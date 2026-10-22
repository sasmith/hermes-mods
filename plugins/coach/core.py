"""coach core: signal store, the one nudge gate (rate limit / mute / demotion), user-channel rules, briefs, log.

Everything lives under <HERMES_HOME>/coach/:
  sessions/<id>.json   per-session signals + limiter state (turns, still-broken hits, subagents, long job, shown)
  bugs.jsonl           Hermes-bug signatures seen by the agent channel (ts, session, sig, sample)
  log.jsonl            every shown nudge, every choice, every digest-only suppression, every agent hint (KPI 4)
  curriculum.yaml      private curriculum overlay (merged over the public plugins/coach/curriculum.yaml)
  curriculum_state.json  spaced-repetition state of the feature tips (curriculum.py)

state.db is read-only here. LLM calls are injected (`llm(messages, max_tokens) -> str`) so tests can fake them;
production uses the auxiliary client, task "coach" (`auxiliary.coach.*`).
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

BUDGET_TURNS = 15         # at most one user nudge per session per this many user turns
DEMOTE_DISMISSALS = 3     # this many dismissals of a rule in DEMOTE_WINDOW_S -> digest-only
DEMOTE_WINDOW_S = 7 * 86400
BUG_WINDOW_S = 7 * 86400
STILL_BROKEN_N = 3
CONFIRMED_FAIL_N = 2


def fixloop_on() -> bool:
    return os.environ.get("COACH_FIXLOOP", "1").strip().lower() not in ("0", "off", "false", "no")
SUBAGENTS_N = 10
LONG_SESSION_MSGS = 250

LLM = Callable[[list, int], str]

# rule -> (title, text template, button label, action). One primary button each; Dismiss is always there.
USER_RULES = {
    "still_broken": ("Third \"still not working\"",
                     "Long fix loops rarely converge. Start a clean session with a brief, the repro and a done-check.",
                     "Restart clean", "restart"),
    "long_session": ("This session is {msgs} messages long",
                     "Long sessions get slow, costly and forgetful. Spin the open work out into fresh sessions.",
                     "Spin out", "spin_out"),
    "screenshot_midturn": ("Screenshot sent mid-turn",
                           "An image alone can't steer a running turn. Add one line saying what to look at.",
                           "Add a line", "add_text"),
    "move_to_board": ("This is board work",
                      "{why}. Long or fan-out jobs survive restarts and run in parallel as Kanban cards.",
                      "Move to board", "board"),
    "hermes_bug": ("Same Hermes bug twice this week",
                   "\"{sig}\" hit {n} sessions. File it once as a card on the hermes board instead of working around it.",
                   "File it once", "file_bug"),
}
# Panels owned by other plugins that share the gate (claim/report through window.__coach).
PEER_RULES = ("prompt-coach", "topic-shift", "spin-out")

STILL_BROKEN_RE = re.compile(
    r"(?i)\b(still\s+(not|doesn'?t|does not|isn'?t|is not|broken|fail\w*|the same|seeing|getting|happen\w*|crash\w*"
    r"|no\b|nothing|wrong|empty|blank|stuck)|same (error|issue|problem|bug|thing|result|crash)"
    r"|(didn'?t|did not|doesn'?t|does not|still can'?t) (work|fix|help|change|load|show)\w*|not working|isn'?t working"
    r"|no change|nothing changed|broken again|again (broken|failing)|keeps? (failing|crashing|breaking))")
# Hermes-written user-role messages: "[IMPORTANT: ...", "[System note: ...", "[ASYNC DELEGATION ...", turn-cap notices.
SYNTHETIC_RE = re.compile(r"^\s*(\[[A-Z][A-Za-z -]{2,40}(:|\]| —)|You've reached the maximum number of tool-calling)")
OOB_RE = re.compile(r"^\s*\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*(.*?)\s*\[/OUT-OF-BAND USER MESSAGE\]", re.S)


def curriculum_mod():
    """Sibling curriculum.py, re-executed when the file changes (same reload rule as __init__._load)."""
    name = "_coach_curriculum"
    path = Path(__file__).resolve().parent / "curriculum.py"
    mtime = path.stat().st_mtime_ns
    mod = sys.modules.get(name)
    if mod is None or getattr(mod, "_coach_mtime", None) != mtime:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        mod._coach_mtime = mtime
    return mod


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def flatten(msg) -> str:
    if isinstance(msg, list):
        return " ".join(str(p.get("text") or "") for p in msg if isinstance(p, dict))
    return str(msg or "")


def is_still_broken(text: str) -> bool:
    t = flatten(text)
    m = OOB_RE.match(t)  # a steering message is the user's own words inside a Hermes wrapper
    t = m.group(1) if m else t
    return bool(t.strip()) and not SYNTHETIC_RE.match(t) and bool(STILL_BROKEN_RE.search(t[:1500]))


class Store:
    """File-backed state shared by the agent hooks and the dashboard API (they may run in different processes)."""

    def __init__(self, root: Optional[Path] = None, clock: Callable[[], float] = time.time):
        self.root = Path(root) if root else hermes_home() / "coach"
        self.clock = clock

    # ── plumbing ──
    @contextlib.contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a+") as fh:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def _spath(self, sid: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sid)[:120]
        return self.root / "sessions" / f"{safe}.json"

    def session(self, sid: str) -> dict:
        try:
            return json.loads(self._spath(sid).read_text())
        except Exception:
            return {}

    def _update(self, sid: str, fn) -> dict:
        with self._lock():
            s = self.session(sid)
            fn(s)
            s["updated"] = self.clock()
            p = self._spath(sid)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(s))
            os.replace(tmp, p)
            return s

    def log(self, rec: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / "log.jsonl").open("a") as f:
                f.write(json.dumps({"ts": self.clock(), **rec}) + "\n")
        except Exception:
            pass  # logging never breaks a turn

    def read_log(self, since: float = 0) -> list[dict]:
        out = []
        p = self.root / "log.jsonl"
        if p.exists():
            for line in p.read_text().splitlines():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts", 0) >= since:
                    out.append(r)
        return out

    # ── signals written by the agent channel ──
    def curriculum(self) -> list:
        try:
            cur = curriculum_mod()
            return cur.load(self.root / "curriculum.yaml")
        except Exception:
            return []

    def confirmed_fail(self, sid: str) -> None:
        """A judge-confirmed failed fix (fix_loop). The user-channel `still_broken` rule counts these once any exist,
        because the regex alone had precision ~0.15 on labeled history."""
        def fn(s):
            s["confirmed_fail"] = s.get("confirmed_fail", 0) + 1
        self._update(sid, fn)

    def user_turn(self, sid: str, text) -> dict:
        broken = is_still_broken(text)
        t = flatten(text)
        m = OOB_RE.match(t)
        t = m.group(1) if m else t
        hits = []
        if t.strip() and not SYNTHETIC_RE.match(t):
            try:
                hits = curriculum_mod().text_hits(self.curriculum(), t)
            except Exception:
                hits = []

        def fn(s):
            s["turns"] = s.get("turns", 0) + 1
            if broken:
                s["still_broken"] = s.get("still_broken", 0) + 1
            if hits:
                th = s.setdefault("tip_text", {})
                for h in hits:
                    th[h] = s["turns"]
        return self._update(sid, fn)

    def add_subagents(self, sid: str, n: int) -> None:
        self._update(sid, lambda s: s.__setitem__("subagents", s.get("subagents", 0) + int(n)))

    def long_job(self, sid: str, reason: str) -> None:
        def fn(s):
            if not s.get("long_job"):
                s["long_job"] = reason
        self._update(sid, fn)

    def bug(self, sid: str, sig: str, sample: str = "") -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / "bugs.jsonl").open("a") as f:
                f.write(json.dumps({"ts": self.clock(), "session_id": sid, "sig": sig, "sample": sample}) + "\n")
        except Exception:
            pass

    def bugs(self, since: float) -> list[dict]:
        out, p = [], self.root / "bugs.jsonl"
        if p.exists():
            for line in p.read_text().splitlines():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts", 0) >= since:
                    out.append(r)
        return out

    def filed(self) -> dict:
        try:
            return json.loads((self.root / "bugs_filed.json").read_text())
        except Exception:
            return {}

    def mark_filed(self, sig: str, card: dict) -> None:
        with self._lock():
            d = self.filed()
            d[sig] = {"ts": self.clock(), **card}
            (self.root / "bugs_filed.json").write_text(json.dumps(d))

    # ── the gate ──
    def demoted(self) -> list[str]:
        cut = self.clock() - DEMOTE_WINDOW_S
        n: dict = {}
        for r in self.read_log(cut):
            if r.get("event") == "choice" and r.get("choice") == "dismissed":
                n[r.get("rule")] = n.get(r.get("rule"), 0) + 1
        return sorted(k for k, v in n.items() if k and v >= DEMOTE_DISMISSALS)

    def gate(self, sid: str, rule: str, key: str, turn: int, demoted: Optional[list] = None) -> tuple[bool, str]:
        """May `rule` (instance `key`) be shown in session `sid` at user turn `turn`? Records the show when yes."""
        s = self.session(sid)
        if key in s.get("shown_keys", []) or key in s.get("digest_keys", []):
            return False, "already_shown"
        if rule in s.get("muted", []):
            return False, "muted"
        if rule in (demoted if demoted is not None else self.demoted()):
            self._update(sid, lambda x: x.setdefault("digest_keys", []).append(key))
            self.log({"event": "digest", "session_id": sid, "rule": rule, "key": key, "turn": turn})
            return False, "demoted"
        last = s.get("shown_turn")
        if last is not None and turn - last < BUDGET_TURNS:
            return False, "budget"

        def fn(x):
            x["shown_turn"] = turn
            x.setdefault("shown_keys", []).append(key)
        self._update(sid, fn)
        return True, "ok"

    def record(self, sid: str, rule: str, event: str, nudge_id: str = "", turn: Optional[int] = None,
               extra: Optional[dict] = None) -> dict:
        """A panel outcome from the desktop: shown (peer panels only), accepted, dismissed, or expired (the panel
        could not be displayed after the gate passed it: focus moved or another panel won; no mute)."""
        if event == "shown":
            def fn(x):
                if turn is not None:
                    x["shown_turn"] = turn
            self._update(sid, fn)
            self.log({"event": "shown", "session_id": sid, "rule": rule, "id": nudge_id, "turn": turn, "peer": True,
                      **(extra or {})})
            return {"ok": True}
        if event not in ("accepted", "dismissed", "expired"):
            return {"ok": False, "error": "event must be shown|accepted|dismissed|expired"}
        if event == "dismissed":
            self._update(sid, lambda x: x.setdefault("muted", []).append(rule) if rule not in x.get("muted", []) else None)
        if rule.startswith("tip:"):
            try:
                curriculum_mod().Spacing(self.root, self.clock).on_choice(rule[4:], event)
            except Exception:
                pass
        self.log({"event": "choice", "session_id": sid, "rule": rule, "id": nudge_id, "choice": event, **(extra or {})})
        return {"ok": True}


# ── state.db reads ────────────────────────────────────────────────────────

def _ro_connect(path, timeout=10):
    """Read-only open. mode=ro fails on a WAL db while its -wal/-shm is absent (writers delete them on last close),
    so fall back to mode=rw + query_only, which reads the same data and never writes rows."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        c.execute("select 1 from sqlite_master limit 1")
        return c
    except sqlite3.OperationalError:
        c = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=timeout)
        c.execute("PRAGMA query_only=ON")
        return c


def _db(db_path: Optional[Path]) -> Optional[sqlite3.Connection]:
    p = db_path or hermes_home() / "state.db"
    try:
        con = _ro_connect(p, timeout=5)
        con.row_factory = sqlite3.Row
        return con
    except Exception:
        return None


def lineage(con, sid: str, depth: int = 6) -> list[str]:
    """This session plus its compression ancestors (oldest first)."""
    ids, cur = [], sid
    for _ in range(depth + 1):
        row = con.execute("select id, parent_session_id from sessions where id=?", (cur,)).fetchone()
        if not row:
            break
        ids.append(row["id"])
        parent = row["parent_session_id"]
        if not parent:
            break
        prow = con.execute("select end_reason from sessions where id=?", (parent,)).fetchone()
        if not prow or "compress" not in str(prow["end_reason"] or ""):
            break
        cur = parent
    return list(reversed(ids)) or [sid]


def session_counts(sid: str, db_path: Optional[Path] = None) -> dict:
    """{ids, msgs, user_turns} over the compression lineage."""
    con = _db(db_path)
    if con is None:
        return {"ids": [sid], "msgs": 0, "user_turns": 0}
    try:
        ids = lineage(con, sid)
        q = ",".join("?" * len(ids))
        msgs = con.execute(f"select count(*) from messages where session_id in ({q})", ids).fetchone()[0]
        turns = con.execute(
            f"""select count(*) from messages where session_id in ({q}) and role='user'
                and coalesce(display_kind,'user') in ('user','')""", ids).fetchone()[0]
        return {"ids": ids, "msgs": int(msgs), "user_turns": int(turns)}
    except Exception:
        return {"ids": [sid], "msgs": 0, "user_turns": 0}
    finally:
        con.close()


# ── user-channel rules ────────────────────────────────────────────────────

def _merged(store: Store, ids: list[str]) -> dict:
    out = {"turns": 0, "still_broken": 0, "confirmed_fail": 0, "subagents": 0, "long_job": None, "tip_text": set()}
    for i in ids:
        s = store.session(i)
        for k in ("turns", "still_broken", "confirmed_fail", "subagents"):
            out[k] += int(s.get(k) or 0)
        out["long_job"] = out["long_job"] or s.get("long_job")
    last = store.session(ids[-1]) if ids else {}
    cur = curriculum_mod()
    out["tip_text"] = {t for t, turn in (last.get("tip_text") or {}).items()
                       if int(last.get("turns") or 0) - int(turn) < cur.TEXT_TURNS}
    return out


def tip_candidates(store: Store, sid: str, counts: dict, sig: dict, db_path: Optional[Path] = None) -> list[dict]:
    """Curriculum tips (lowest priority on the bus): spaced, retired once adopted, at most one per check.
    COACH_TIPS=0 turns them off (the bus rules keep working)."""
    if os.environ.get("COACH_TIPS", "1").lower() in ("0", "off", "false", "no"):
        return []
    try:
        cur = curriculum_mod()
        facts = cur.Facts(counts["ids"], db_path or hermes_home() / "state.db", store.clock)
        return cur.candidates(store.curriculum(), sig, facts, counts["msgs"], sig["tip_text"],
                              cur.Spacing(store.root, store.clock), cur.adopted_ids())
    except Exception:
        return []


def bug_hits(store: Store, sid_ids: list[str]) -> Optional[dict]:
    """A signature seen in this session AND in >=2 distinct sessions within the window, not filed yet."""
    rows = store.bugs(store.clock() - BUG_WINDOW_S)
    filed = store.filed()
    by: dict = {}
    for r in rows:
        by.setdefault(r["sig"], []).append(r)
    for sig, rs in by.items():
        sessions = {r["session_id"] for r in rs}
        if sig in filed or len(sessions) < 2 or not sessions & set(sid_ids):
            continue
        return {"sig": sig, "sessions": sorted(sessions), "n": len(sessions),
                "samples": [r.get("sample") for r in rs[-3:] if r.get("sample")]}
    return None


def candidates(store: Store, sid: str, counts: dict, db_path: Optional[Path] = None) -> list[dict]:
    """User-channel rules that currently apply to the session, most urgent first (before the gate)."""
    sig = _merged(store, counts["ids"])
    out = []
    bug = bug_hits(store, counts["ids"])
    if bug:
        h = hashlib.sha1(bug["sig"].encode()).hexdigest()[:10]
        out.append({"rule": "hermes_bug", "key": f"hermes_bug:{h}", "fmt": {"sig": bug["sig"][:90], "n": bug["n"]},
                    "data": bug})
    # With the fix_loop judge on, count judge-confirmed failed fixes (2 = Anthropic's "two failed corrections");
    # otherwise fall back to the regex count at 3.
    n_fail, need = ((sig["confirmed_fail"], CONFIRMED_FAIL_N) if fixloop_on() else (sig["still_broken"], STILL_BROKEN_N))
    if n_fail >= need:
        level = n_fail // need
        out.append({"rule": "still_broken", "key": f"still_broken:{level}", "fmt": {}, "data": {"n": n_fail}})
    if sig["subagents"] >= SUBAGENTS_N or sig["long_job"]:
        why = (f"{sig['subagents']} subagents in this chat" if sig["subagents"] >= SUBAGENTS_N
               else f"A long job is running in chat ({sig['long_job']})")
        out.append({"rule": "move_to_board", "key": "move_to_board", "fmt": {"why": why},
                    "data": {"subagents": sig["subagents"], "long_job": sig["long_job"]}})
    if counts["msgs"] > LONG_SESSION_MSGS:
        band = counts["msgs"] // LONG_SESSION_MSGS
        out.append({"rule": "long_session", "key": f"long_session:{band}", "fmt": {"msgs": counts["msgs"]}, "data": {}})
    out += tip_candidates(store, sid, counts, sig, db_path)
    return out


def nudge(rule: str, key: str, fmt: dict, data: Optional[dict] = None, entry: Optional[dict] = None) -> dict:
    if entry is not None:
        t = curriculum_mod().tip_nudge({"entry": entry, "fmt": fmt})
        return {"id": uuid.uuid4().hex[:12], "rule": rule, "key": key, **t}
    title, text, button, action = USER_RULES[rule]
    return {"id": uuid.uuid4().hex[:12], "rule": rule, "key": key, "title": title.format(**fmt),
            "text": text.format(**fmt), "button": button, "action": action, "data": data or {}}


def state(store: Store, sid: str, db_path: Optional[Path] = None, offer: Optional[str] = None) -> dict:
    """What the desktop should show for the session now (at most one nudge), plus the gate facts peers need."""
    counts = session_counts(sid, db_path)
    turn = counts["user_turns"] or _merged(store, counts["ids"])["turns"]
    demoted = store.demoted()
    s = store.session(sid)
    shown = None
    if offer:
        cands = [{"rule": offer, "key": f"{offer}:{turn}", "fmt": {}, "data": {}}] if offer in USER_RULES else []
    else:
        cands = candidates(store, sid, counts, db_path)
    for c in cands:
        ok, why = store.gate(sid, c["rule"], c["key"], turn, demoted)
        if ok:
            shown = nudge(c["rule"], c["key"], c["fmt"], c.get("data"), c.get("entry"))
            if c.get("entry") is not None:
                curriculum_mod().Spacing(store.root, store.clock).on_shown(c["entry"]["id"])
            store.log({"event": "shown", "session_id": sid, "rule": c["rule"], "key": c["key"], "id": shown["id"],
                       "turn": turn, "msgs": counts["msgs"]})
            break
        if why == "budget":
            break  # budget is per session: nothing else may show either
    s = store.session(sid)
    return {"nudge": shown, "turn": turn, "msgs": counts["msgs"], "muted": s.get("muted", []), "demoted": demoted,
            "shown_turn": s.get("shown_turn"), "budget_turns": BUDGET_TURNS}


def stats(store: Store, since: float = 0) -> dict:
    """Coaching precision per rule (KPI 4): accepted / shown."""
    per: dict = {}
    for r in store.read_log(since):
        rule = r.get("rule")
        if not rule:
            continue
        d = per.setdefault(rule, {"shown": 0, "accepted": 0, "dismissed": 0, "expired": 0, "digest": 0,
                                  "agent_hint": 0})
        ev = r.get("event")
        if ev == "shown":
            d["shown"] += 1
        elif ev == "choice":
            d[r.get("choice")] = d.get(r.get("choice"), 0) + 1
        elif ev in ("digest", "agent_hint"):
            d[ev] += 1
    for d in per.values():
        seen = d["shown"] - d["expired"]
        d["precision"] = round(d["accepted"] / seen, 3) if seen > 0 else None
    return {"rules": per, "demoted": store.demoted()}


# ── briefs (reuses spin-out's transcript reader) ─────────────────────────

def spin_out_core():
    name = "_spin_out_core"
    path = Path(__file__).resolve().parent.parent / "spin-out" / "core.py"
    mod = sys.modules.get(name)
    if mod is None or getattr(mod, "__file__", None) != str(path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[name] = mod
    return mod


BRIEF_SYSTEM = """You write the FIRST message of a fresh AI agent session that takes over work from a long session.
The new agent has NO access to the old transcript except through the @session pointer added below your brief.

Mode: {mode}
- restart: the old session went around in circles ("still not working"). Distil it so the new session can solve it cleanly.
- board: the work becomes a Kanban card that runs unattended; make it executable without a human.

Write Markdown with exactly these sections:
## Goal  (the real outcome, one or two sentences)
## Context  (repo / working dir / branch / hosts / services / exact paths, commands, IDs; copied verbatim)
## Tried so far  (each attempt and WHY it failed or what it showed; mark guesses as guesses)
## Repro  (the shortest command or steps that show the problem now, or "none yet - build one first")
## Done when  (a concrete check an outside judge can run: test, command output, metric)
Never invent paths, IDs or results. 150-450 words.
Reply with ONLY JSON: {{"title": "<=7 words", "brief": "..."}}"""


def brief(sid: str, llm: LLM, mode: str = "restart", db_path: Optional[Path] = None) -> dict:
    so = spin_out_core()
    info = so.session_info(sid, db_path)
    if not info:
        return {"error": "unknown session"}
    files = so.touched_files(info["messages"])
    user = (f"Session title: {info.get('title') or '(none)'}\nWorking dir: {info.get('cwd') or '(none)'}\n"
            f"Repo: {info.get('git_repo_root') or '(none)'}  branch: {info.get('git_branch') or '(none)'}\n"
            f"Files the session read/edited: {', '.join(files) or 'none'}\n\n"
            f"TRANSCRIPT (latest messages, oldest first):\n\n{so._transcript(info['messages'])[-60000:]}")
    try:
        raw = llm([{"role": "system", "content": BRIEF_SYSTEM.format(mode=mode)}, {"role": "user", "content": user}], 4000)
        m = re.search(r"\{.*\}", raw or "", re.S)
        out = json.loads(m.group(0)) if m else {}
    except Exception as exc:
        return {"error": f"brief failed: {type(exc).__name__}: {exc}"[:300]}
    text = str(out.get("brief") or "").strip()
    if not text:
        return {"error": "brief came back empty"}
    profile = info.get("profile_name") or "default"
    title = str(out.get("title") or info.get("title") or "Restart").strip()[:80]
    ref = f"@session:{profile}/{sid}"
    why = "restarted clean from" if mode == "restart" else "moved to the board from"
    message = (f"{text}\n\n---\n[coach] {why} {ref}" + (f" (\"{info['title']}\")" if info.get("title") else "")
               + ". Read it only for details this brief does not cover.")
    return {"title": title, "brief": text, "message": message, "cwd": info.get("cwd") or "", "profile": profile}


def bug_card(store: Store, sid: str, sig: str) -> dict:
    rows = [r for r in store.bugs(store.clock() - BUG_WINDOW_S) if r["sig"] == sig]
    sessions = sorted({r["session_id"] for r in rows})
    samples = [r.get("sample") for r in rows if r.get("sample")][-3:]
    body = ("The coach saw the same Hermes failure in several sessions this week. Fix the class, not the instance.\n\n"
            f"## Signature\n`{sig}`\n\n## Sessions\n" + "\n".join(f"- @session:default/{s}" for s in sessions)
            + ("\n\n## Samples\n" + "\n".join(f"- {s}" for s in samples) if samples else "")
            + "\n\n## Done when\nThe failure is reproduced, root-caused, fixed as a plugin or `[daveey]` core commit "
              "(hermes-core-wt), and a test covers it.")
    h = hashlib.sha1(sig.encode()).hexdigest()[:10]
    return {"title": f"Hermes bug: {sig[:60]}", "message": body, "key": f"coach-bug:{h}"}


def aux_llm(messages: list, max_tokens: int) -> str:
    from agent.auxiliary_client import call_llm  # type: ignore
    resp = call_llm(task="coach", messages=messages, max_tokens=max_tokens, temperature=0, timeout=120)
    return (getattr(resp.choices[0].message, "content", None) or "").strip()
