#!/usr/bin/env python3
"""Write one compact, redacted JSON digest per Hermes session (stdlib only).

    session_digest.py --host NAME --out DIR [--db PATH ...] [--since-days 45]

With no --db, it reads $HERMES_HOME/state.db (default ~/.hermes) plus every profiles/*/state.db.
Each top-level session becomes DIR/<host>/<profile>/<session_id>.json. A digest is rewritten only
when the session has changed (more messages or later activity), so a git commit of DIR shows
real deltas. Child (subagent) sessions are folded into their parent as counts and cost.

What goes in: session stats, the user's turns (injected context stripped, secrets redacted,
truncated), tool counts and tool-error counts, pattern signals, and the tail of the last
assistant reply. What never goes in: tool output, system prompts, reasoning, file contents.
"""
import argparse, collections, fnmatch, json, os, re, sqlite3, sys, time
from pathlib import Path

DIGEST_V = 6            # v6: each user turn carries `prev`, the agent reply before it (head+tail clip), for the
                        #     self-heal backstop judge (without it \"why didnt this work\" reads as a new issue)
                        # v5: Kanban notices, cron prompts, compaction/continuation notes are synthetic, not user turns
                        # v4: `features` (content-free feature-use counts for curriculum adoption checks)
MAX_TURNS = 60          # keep the first 10 and the last 50 user turns
TURN_CHARS = 500
LAST_REPLY_CHARS = 800
PREV_HEAD, PREV_TAIL = 150, 450  # `prev` per user turn: a reply's ending (\"Done.\", \"Want me to…?\") is what a correction answers

# Blocks Hermes injects into user messages; they are not the user's words.
INJECTED = [
    re.compile(r"<memory-context>.*?</memory-context>", re.S),
    re.compile(r"<system-reminder>.*?</system-reminder>", re.S),
    re.compile(r"<pasted_content[^>]*>.*?</pasted_content>", re.S),
    re.compile(r"\[System note:[^\]]*\]", re.S),
    re.compile(r"\[OUT-OF-BAND USER MESSAGE[^\]]*\]|\[/OUT-OF-BAND USER MESSAGE\]"),
    re.compile(r"\[Context from the interrupted assistant response\].*?(?=\n\n|\Z)", re.S),
]
SECRETS = [
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,}|sk-ant-[A-Za-z0-9_\-]{16,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\b(xox[abprs]-[A-Za-z0-9\-]{10,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"),
    re.compile(r"(?i)\b(bearer|token|password|passwd|secret|api[_-]?key)(\s*[:=]\s*|\s+)((?=[^\s\"']*\d)[^\s\"']{12,})"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b[A-Za-z0-9+_\-]{48,}={0,2}"),   # long opaque blobs (keys, tokens, base64)
]
SIGNALS = {
    "screenshot": re.compile(r"Screenshot|\[Image attached|@image:|\[screenshot\]"),
    "session_mention": re.compile(r"@session:"),
    "repeat_fix": re.compile(r"(?i)\b(still|again|didn'?t work|didnt work|not working|broken|same (error|issue|problem))\b"),
    "stuck": re.compile(r"(?i)\b(stuck|hang|hanging|hung|frozen|idle|not responding)\b"),
    "resume": re.compile(r"(?i)^\s*(resume|continue)\s*$"),
    "plan": re.compile(r"(?i)\bplan\b"),
    "automation_want": re.compile(r"(?i)\b(every (day|morning|night|hour|week)|whenever|automatically|each time)\b"),
    "hermes_meta": re.compile(r"(?i)\bhermes\b"),
    "frustration": re.compile(r"(?i)\b(wtf|why (is|isn'?t|does|doesn'?t|did)|ugh|come on|seriously)\b"),
}
# Feature use read from tool-call args (counts only; no argument text leaves the host). The curriculum's
# "adopted" tests (plugins/coach/curriculum.yaml, evaluated by coach_mine.py --write-adoption) key off these names.
# `hermes` as a command word (not a ~/.hermes path), then its options up to the subcommand.
HERMES_CMD = r"(?<![\w./-])hermes\s+(?:[^\s;&|]+\s+)*?"
CMD_FEATURES = [
    ("kanban_board_create", re.compile(r"\bkanban\s+boards\s+(create|new)\b")),
    ("kanban_cli_create", re.compile(HERMES_CMD + r"kanban\s+create\b")),
    ("kanban_swarm", re.compile(r"\bkanban\s+swarm\b")),
    ("kanban_specify", re.compile(r"\bkanban\s+specify\b")),
    ("kanban_decompose", re.compile(r"\bkanban\s+decompose\b")),
    ("kanban_notify", re.compile(r"\bkanban\s+notify-subscribe\b")),
    ("kanban_tail", re.compile(r"\bkanban\s+(tail|watch)\b")),
    ("kanban_request_review", re.compile(r"\bkanban\s+request-review\b")),
    ("kanban_request_changes", re.compile(r"\bkanban\s+(request-changes|reopen-review)\b")),
    ("profile_create", re.compile(HERMES_CMD + r"profile\s+create\b")),
    ("cron_create", re.compile(HERMES_CMD + r"cron\s+create\b")),
    ("git_worktree", re.compile(r"\bgit\s+worktree\s+add\b")),
    ("hermes_worktree", re.compile(HERMES_CMD + r"(-w|--worktree)(?=\s|$)")),
]


def _args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def call_features(name, args):
    """Feature names one tool call counts toward (a tool_call wrapper is unwrapped by the caller)."""
    out = []
    if name == "kanban_create":
        out.append("kanban_create")
        if args.get("max_runtime_seconds"):
            out.append("kanban_create:max_runtime")
        if args.get("parents"):
            out.append("kanban_create:parents")
        if args.get("workspace_kind") == "worktree":
            out.append("kanban_create:worktree")
        if args.get("goal_mode"):
            out.append("kanban_create:goal_mode")
        if args.get("triage"):
            out.append("kanban_create:triage")
        if args.get("skills"):
            out.append("kanban_create:skills")
        if str(args.get("assignee") or "default") not in ("default", ""):
            out.append("kanban_create:specialist")
    elif name in ("todo", "todo_list", "todo_write"):
        out.append("todo")
    elif name in ("cronjob", "cronjob_manage", "cron"):
        if str(args.get("action") or "create") in ("create", "add"):
            out.append("cron_create")
    elif name in ("cross_session", "kanban_request_review", "kanban_request_changes"):
        out.append(name)
    elif name == "terminal":
        cmd = str(args.get("command") or "")
        out += [k for k, rx in CMD_FEATURES if rx.search(cmd)]
        if "kanban_cli_create" in out:  # the CLI spelling of the same card options
            for flag, k in (("--max-runtime", "max_runtime"), ("--parent", "parents"), ("--goal", "goal_mode"),
                            ("--triage", "triage"), ("--skill", "skills")):
                if re.search(re.escape(flag) + r"\b", cmd):
                    out.append(f"kanban_create:{k}")
    return out


TOOL_ERR = re.compile(r'"exit_code":\s*[1-9]|"success":\s*false|"error":\s*"[^"]|^Error\b|Traceback \(most recent', re.M)


def redact(s: str) -> str:
    for rx in SECRETS:
        s = rx.sub(lambda m: (m.group(1) + m.group(2) + "[REDACTED]") if rx.groups == 3 else "[REDACTED]", s)
    return s


# User-role messages Hermes writes itself (not typed by the user): counted, not kept as turns.
SYNTHETIC = re.compile(
    r"^\s*(\[(IMPORTANT:|STILL IN PROGRESS|Note: model was|Recent Summary|CONTEXT COMPACTION|Background task|Continuing toward your"
    r" standing goal|ASYNC DELEGATION|Message from another Hermes session|From the [^\]]{0,80}session|System: "
    r"|Cronjob \"|Continuing toward this kanban task|Your active task list was preserved"
    r"|Current user objective preserved|Turn stopped by Desktop)"
    # Kanban gateway notices: "<glyph> [board] @assignee Kanban t_…" (tui_gateway/session_notifications.py)
    r"|[✔✖✘⏸⏱🔄]\ufe0f?\s+(\[[^\]\n]{1,80}\]\s+)?(@\S+\s+)?Kanban\s+t_"
    r"|You've reached the maximum number of tool-calling)", re.I)
SYNTH_KIND = [("cron", "[Cronjob"), ("kanban", " Kanban t_"), ("kanban_continue", "kanban task"),
              ("todo_preserved", "task list was preserved"), ("objective_preserved", "objective preserved"),
              ("turn_stopped", "Turn stopped by Desktop"),
              ("bg_notify", "Background"), ("goal_continue", "standing goal"), ("delegation", "ASYNC DELEGATION"),
              ("cross_session", "session"), ("turn_cap", "maximum number of tool-calling"), ("system", "System:")]


def flatten(content: str) -> str:
    """Multimodal content is stored as a JSON list of parts; keep the text, mark images."""
    c = content.lstrip()
    if c.startswith("json:"):
        c = c[5:].lstrip()
    if c.startswith("[{") or c.startswith('{"type"'):
        try:
            parts = json.loads(c)
            parts = parts if isinstance(parts, list) else [parts]
            out = []
            for p in parts:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    out.append(p.get("text") or "")
                elif "image" in str(p.get("type")):
                    out.append("[screenshot]")
            return "\n".join(out)
        except Exception:
            pass
    return re.sub(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", "[binary]", content)


def clean_user(text: str) -> str:
    text = flatten(text)
    for rx in INJECTED:
        text = rx.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"@image:`[^`]*`", "[screenshot]", text)
    # Desktop stores "@image:`path`\n[screenshot]" for one attachment; don't count it twice.
    text = re.sub(r"\[screenshot\](\s*\[screenshot\])+", "[screenshot]", text)
    text = re.sub(r"/Users/[^/\s]+/", "~/", text)
    return redact(re.sub(r"\n{3,}", "\n\n", text).strip())


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 15] + " …[truncated]"


def clip_ends(s: str, head: int = PREV_HEAD, tail: int = PREV_TAIL) -> str:
    s = (s or "").strip()
    return s if len(s) <= head + tail + 20 else s[:head] + " …[clipped]… " + s[-tail:]


def discover_dbs(home: Path):
    out = []
    if (home / "state.db").exists():
        out.append(("default", home / "state.db"))
    for p in sorted((home / "profiles").glob("*/state.db")):
        if not p.parent.name.startswith("."):
            out.append((p.parent.name, p))
    return out


def cols(con, table):
    return {r[1] for r in con.execute(f"pragma table_info({table})")}


def transcript(con, sid, mcols):
    """One row per real message, in order.

    state.db is append-only across compaction: every compression pass re-inserts the live tail as new
    rows (same message_uid) and flips the old copies to active=0, compacted=1. Queued prompts (typed
    while a turn runs) also get a placeholder row (active=0, compacted=0, display_metadata
    _queued_prompt) that is re-written on each pass; the real send lands later as its own row. Read
    raw, one Desktop send shows up many times. Keep live + compacted history only (drop rewound and
    queued placeholders), drop display-only `hidden` rows, and collapse copies by message_uid.
    """
    where, order = "session_id=?", "timestamp, id"
    if "active" in mcols and "compacted" in mcols:
        where += " and (active=1 or compacted=1)"
    if "display_kind" in mcols:
        where += " and coalesce(display_kind, '') <> 'hidden'"
    uid = "message_uid" if "message_uid" in mcols else "NULL"
    active = "active" if "active" in mcols else "1"
    rows = con.execute(f"""select role, content, tool_name, tool_calls, timestamp, {uid} as uid, {active} as live
                           from messages where {where} order by {order}""", (sid,)).fetchall()
    # Prefer the live copy of a uid, else its first compacted copy; keep the first position seen.
    best, order_keys = {}, []
    for r in rows:
        k = r["uid"] or (r["role"], r["timestamp"], r["content"], r["tool_calls"])
        if k not in best:
            order_keys.append(k)
            best[k] = r
        elif r["live"] and not best[k]["live"]:
            best[k] = r
    return [best[k] for k in order_keys]


def digest_db(profile, db, host, since):
    con = sqlite3.connect(f"file:{db}", uri=True, timeout=30)
    con.execute("pragma query_only = 1")
    con.row_factory = sqlite3.Row
    scols = cols(con, "sessions")
    opt = lambda c, alias=None: (c if c in scols else f"NULL") + f" as {alias or c}"
    act = "coalesce(last_activity_at, ended_at, started_at)" if "last_activity_at" in scols else "coalesce(ended_at, started_at)"
    rows = con.execute(f"""
        select id, source, model, parent_session_id, started_at, ended_at, end_reason, message_count,
               tool_call_count, api_call_count, input_tokens, output_tokens, cache_read_tokens,
               coalesce(actual_cost_usd, estimated_cost_usd) as cost, title,
               {opt('cwd')}, {opt('git_repo_root')}, {opt('git_branch')}, {opt('archived')},
               {opt('profile_name')}, {act} as activity
        from sessions where {act} >= ?""", (since,)).fetchall()
    mcols = cols(con, "messages")

    # Fold subagent sessions into their top-level ancestor.
    kids = collections.defaultdict(lambda: {"n": 0, "cost": 0.0, "msgs": 0, "tool_calls": 0})
    parent_of = {r["id"]: r["parent_session_id"] for r in con.execute("select id, parent_session_id from sessions")}
    def root(i):
        seen = 0
        while parent_of.get(i) and seen < 50:
            i, seen = parent_of[i], seen + 1
        return i
    for r in rows:
        if r["parent_session_id"]:
            k = kids[root(r["id"])]
            k["n"] += 1; k["cost"] += r["cost"] or 0; k["msgs"] += r["message_count"] or 0
            k["tool_calls"] += r["tool_call_count"] or 0

    for r in rows:
        if r["parent_session_id"]:
            continue
        sid = r["id"]
        msgs = transcript(con, sid, mcols)
        users, tools, terr = [], collections.Counter(), collections.Counter()
        sig = collections.Counter()
        feats = collections.Counter()
        last_reply, n_real = "", 0
        for m in msgs:
            role, content = m["role"], m["content"] or ""
            if role == "user":
                text = clean_user(content)
                if not text:
                    continue
                if SYNTHETIC.match(text):
                    head = text[:80]
                    sig["synthetic_" + next((k for k, w in SYNTH_KIND if w in head), "summary")] += 1
                    continue
                for k, rx in SIGNALS.items():
                    if rx.search(text):
                        sig[k] += 1
                n_real += 1
                if users and users[-1]["text"] == trunc(text, TURN_CHARS):
                    continue  # the same turn persisted twice (retry / steer echo)
                turn = {"t": round(m["timestamp"]), "text": trunc(text, TURN_CHARS)}
                if last_reply.strip():
                    turn["prev"] = clip_ends(redact(_home_rel(last_reply) or ""))
                users.append(turn)
            elif role == "assistant":
                if content.strip():
                    last_reply = content
                for tc, targs, inner in _tool_calls(m["tool_calls"], with_args=True):
                    if not inner:
                        tools[tc] += 1
                    feats.update(call_features(tc, targs))
            elif role == "tool":
                if m["tool_name"] and TOOL_ERR.search(content[:4000]):
                    terr[m["tool_name"]] += 1
        if len(users) > MAX_TURNS:
            users = users[:10] + [{"t": None, "text": f"…[{len(users) - MAX_TURNS} turns omitted]"}] + users[-(MAX_TURNS - 10):]
        k = kids.get(sid, {"n": 0, "cost": 0.0, "msgs": 0, "tool_calls": 0})
        yield {
            "digest_v": DIGEST_V, "host": host, "profile": r["profile_name"] or profile, "id": sid,
            "source": r["source"], "title": r["title"], "model": r["model"],
            "started_at": r["started_at"], "ended_at": r["ended_at"], "last_activity_at": r["activity"],
            "end_reason": r["end_reason"], "archived": bool(r["archived"]),
            "cwd": _home_rel(r["cwd"]), "repo": _home_rel(r["git_repo_root"]), "branch": r["git_branch"],
            "msgs": len(msgs), "tool_calls": sum(tools.values()), "api_calls": r["api_call_count"],
            "tokens": {"in": r["input_tokens"], "out": r["output_tokens"], "cache_read": r["cache_read_tokens"]},
            "cost_usd": round(r["cost"] or 0, 3),
            "children": {"n": k["n"], "cost_usd": round(k["cost"], 3), "msgs": k["msgs"], "tool_calls": k["tool_calls"]},
            "user_turns_total": n_real,
            "user_turns": users,
            "signals": dict(sig),
            "tools": dict(tools.most_common(25)),
            "tool_errors": dict(terr.most_common(15)),
            "features": dict(sorted(feats.items())),
            "last_reply": trunc(redact(_home_rel(last_reply) or ""), LAST_REPLY_CHARS),
        }
    con.close()


def _tool_calls(raw, with_args=False):
    """Tool names in an assistant message. with_args: (name, args, inner) triples, where deferred tools invoked
    through the `tool_call` wrapper also yield each inner call (inner=True), so `todo_list` via tool_call counts
    as todo use without inflating the tool counts."""
    if not raw:
        return []
    try:
        calls = json.loads(raw)
    except Exception:
        return []
    out = []
    for c in calls if isinstance(calls, list) else []:
        fn = (c.get("function") or {}) if isinstance(c, dict) else {}
        name = fn.get("name") or (c.get("name") if isinstance(c, dict) else None)
        if not name:
            continue
        if not with_args:
            out.append(name)
            continue
        args = _args(fn.get("arguments") if fn else c.get("arguments"))
        out.append((name, args, False))
        if name == "tool_call":
            for inner in args.get("calls") or []:
                if isinstance(inner, dict) and inner.get("name"):
                    out.append((inner["name"], _args(inner.get("arguments")), True))
    return out


def _home_rel(s):
    return re.sub(r"/Users/[^/\s]+/|/home/[^/\s]+/", "~/", s) if isinstance(s, str) else s


def excluded_profile(profile, patterns):
    """fnmatch, so 'alpha*' also drops 'alpha-follow' (a plain 'alpha' missed it and leaked Slack chat)."""
    return any(fnmatch.fnmatchcase(profile, p) for p in patterns)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--db", action="append", help="profile=path or path (repeatable)")
    ap.add_argument("--home", default=os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
    ap.add_argument("--since-days", type=float, default=45)
    ap.add_argument("--exclude-profile", action="append", default=[],
                    help="skip a discovered profile; glob ok, e.g. 'alpha*' (shared team bots holding other people's chats)")
    a = ap.parse_args()

    dbs = []
    for d in a.db or []:
        prof, _, path = d.rpartition("=")
        dbs.append((prof or Path(path).parent.name, Path(path)))
    dbs = dbs or discover_dbs(Path(a.home).resolve())
    dbs = [(p, d) for p, d in dbs if not excluded_profile(p, a.exclude_profile)]
    since = time.time() - a.since_days * 86400
    out = Path(a.out) / a.host
    written = unchanged = 0
    for profile, db in dbs:
        try:
            for d in digest_db(profile, db, a.host, since):
                p = out / (d["profile"] or profile) / f"{d['id']}.json"
                body = json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True)
                if p.exists() and p.read_text() == body:
                    unchanged += 1
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp"); tmp.write_text(body); tmp.replace(p)
                written += 1
        except sqlite3.Error as e:
            print(f"warn: {profile} {db}: {e}", file=sys.stderr)
    print(json.dumps({"host": a.host, "dbs": len(dbs), "written": written, "unchanged": unchanged}))


if __name__ == "__main__":
    main()
