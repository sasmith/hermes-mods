"""reply_check: look at the agent's final answer for a turn BEFORE the turn ends (pre_verify hook).

Four anti-patterns, measured in the user's own history (coach gap report 2026-10-14):
- handoff:     the agent hands the user a step it could plausibly do itself (click/log in/run/paste...).
- confirm:     the agent ends by asking permission for a reversible next step ("Want me to ...?").
- unverified:  the agent claims done/fixed/working without checking on the user's real path.
- unwatched:   the agent promises to continue once outside work finishes ("I'll write the report when the
               runs finish") but nothing will wake the session: no background process, kanban card, goal or
               subagent. The turn ends, the sidebar paints the row done, and the promise is never kept.
               A deterministic wake-source check runs first, so the judge only sees promises with no waker.

Layer 1 (here, zero latency) are cheap regex prefilters; Layer 2 is an LLM judge (aux task
`coach_reply`) that sees the user's ask, the turn's tool log and the reply. Only a judge verdict
at or above the threshold produces a nudge; the nudge goes back to the agent (the turn continues),
never to the user.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Optional

HANDOFF_RE = re.compile(
    r"(\b(please|you(?:'ll| will)? need to|you can now|can you|could you|go to|open|click|tap|drag|paste|"
    r"sign in|log ?in|run (?:this|the following)|quit and (?:re)?open|relaunch|restart (?:the )?app|"
    r"approve|grant|enable|allow)\b[^.\n]{0,120}\b(then|and) (?:tell|let) me\b"
    r"|\b(let me know|tell me|ping me|reply|say \"?\w+\"?) (?:when|once|after) (?:you(?:'ve| have)?|it'?s|that'?s)\b"
    r"|\b(?:once|after|when) you(?:'ve| have)? (?:clicked|signed|logged|run|opened|approved|granted|enabled|dropped|pasted|added|installed|plugged|restarted|done)\b"
    r"|^\s*(?:\d+[.)]|[-*])\s*(?:click|open|go to|tap|sign in|log in|paste|drag|press|select|choose|enable|grant)\b"
    # Statement-shaped handoffs with no "then tell me" tail (missed 6/6 in a real Alpha + coordinator transcript):
    # "What you need to do:", "Cleanup for you:", "That's yours to do", "may still need you", "/invite @bot first",
    # "someone in #chan runs ...", "needs your AWS SSO session", "one I can't run".
    r"|\bwhat you(?:'ll)? need to do\b|\b(?:cleanup|left|steps?|actions?|to-?dos?|homework) for you\b"
    r"|\b(?:that'?s|it'?s|this is) yours to do\b|\b(?:may|might|will|would) (?:still )?need you\b"
    r"|\bneeds? you to\b|(?:^|\s)`?/invite\b|\bsomeone (?:in|on|with) \S+ (?:runs?|types?|clicks?|invites?|sends?)\b"
    r"|\bneeds? your (?:\w+ ){0,2}(?:session|login|password|approval|click|terminal)\b"
    r"|\b(?:one|command|step|something) I can'?t (?:run|do)\b)",
    re.I | re.M)
CONFIRM_RE = re.compile(
    r"(\b(?:want me to|should i|shall i|do you want me to|would you like me to|ok(?:ay)? (?:if|to) i|"
    r"(?:ready|happy) (?:for me )?to (?:proceed|go|ship|merge|push|apply)|say (?:the word|go|yes)|"
    r"(?:let me know|tell me) if you(?:'d)? (?:want|like) me to)\b[^?\n]{0,200}\?\s*$"
    r"|\b(?:want me to|should i|shall i)\b[^?\n]{0,200}\?)", re.I | re.M)
_FINISH = (r"(?:finish(?:es|ed)?|complete[sd]?|land(?:s|ed)?|(?:is|are) done|come[s]? back|end[s]?|pass(?:es)?|"
           r"go(?:es)? green)")
WAIT_RE = re.compile(
    r"(\b(?:once|when|after|as soon as)\b[^.\n]{0,90}\b" + _FINISH + r"\b[^\n]{0,160}?\b(?:I'll|I will|I'm going to)\b"
    r"|\b(?:I'll|I will)\b[^.\n]{0,160}\b(?:once|when|after|as soon as)\b[^.\n]{0,90}\b" + _FINISH + r"\b"
    r"|\bI'll (?:check back|check in again|follow up|report back|pick (?:this|it) (?:back )?up)\b"
    r"|\b(?:waiting|wait) (?:on|for) (?:the |its |their |those |these )?(?:\w+ ){0,3}(?:runs?|jobs?|training|"
    r"experiments?|evals?|builds?|CI|sweeps?|deploys?|tests?|soaks?|workers?)\b)", re.I)
DONE_RE = re.compile(
    r"\b(done|fixed|works now|is working|now works|verified|deployed|shipped|is live|it'?s live|resolved|"
    r"all set|should (?:now )?(?:work|be fixed|be working)|ready to use|in place|complete[d]?)\b", re.I)

RULES = ("handoff", "confirm", "unverified", "unwatched")
DEFAULT_RULES = ("handoff", "confirm", "unwatched")


def prefilter(reply: str) -> list[str]:
    """Which rules this reply is a candidate for (cheap, recall-oriented)."""
    r = reply or ""
    tail = r[-700:]
    out = []
    if HANDOFF_RE.search(r):
        out.append("handoff")
    if CONFIRM_RE.search(tail):
        out.append("confirm")
    if DONE_RE.search(r[:600]) or DONE_RE.search(tail):
        out.append("unverified")
    if WAIT_RE.search(r[-1200:]):
        out.append("unwatched")
    return out


# Tools that, called this turn, leave something that will wake the session later.
WAKE_TOOLS = ("delegate_task", "kanban_create", "kanban_schedule", "cronjob")


def _ro_connect(path, timeout=10):
    """Read-only open. mode=ro fails on a WAL db while its -wal/-shm is absent (writers delete them on last close),
    so fall back to mode=rw + query_only, which reads the same data and never writes rows."""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        c.execute("select 1 from sqlite_master limit 1")
        return c
    except sqlite3.OperationalError:
        c = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=timeout)
        c.execute("PRAGMA query_only=ON")
        return c


def _lineage(session_id: str, depth: int = 12) -> list[str]:
    """session_id plus its compression ancestors (compression rotates ids; a watcher started before a
    rotation is keyed by the old id)."""
    ids = [session_id]
    try:
        import sqlite3
        db = _ro_connect(_hermes_home() / 'state.db', timeout=1)
        try:
            cur = session_id
            for _ in range(depth):
                row = db.execute("select parent_session_id from sessions where id=?", (cur,)).fetchone()
                if not row or not row[0] or row[0] in ids:
                    break
                cur = row[0]
                ids.append(cur)
        finally:
            db.close()
    except Exception:
        pass
    return ids


def _running_processes(ids: set) -> list[str]:
    """Background processes still running for this session: the in-process registry (authoritative inside the
    backend that owns the turn) plus live pids in processes.json (another backend, or a recovered checkpoint)."""
    out: list[str] = []
    try:
        from tools.process_registry import process_registry as reg  # type: ignore
        with reg._lock:
            live = list(reg._running.values())
        out += [s.id for s in live if not s.exited and ({s.session_key, s.owner_task_id} & ids)]
    except Exception:
        pass
    try:
        rows = json.loads((_hermes_home() / "processes.json").read_text())
        rows = rows if isinstance(rows, list) else rows.get("processes", [])
    except Exception:
        rows = []
    for r in rows:
        if not isinstance(r, dict) or not ({r.get("session_key"), r.get("owner_task_id")} & ids) or not r.get("pid"):
            continue
        pid_name = str(r.get("session_id") or r["pid"])
        if pid_name in out:
            continue
        try:
            os.kill(int(r["pid"]), 0)
            out.append(pid_name)
        except (OSError, ValueError):
            pass
    return out


def wake_sources(session_id: str, tool_log: Optional[list] = None) -> list[str]:
    """What will wake this session after the turn ends. Empty = nothing; a promise to continue later is orphaned.
    Fail-open the other way: any lookup error just means that source isn't counted."""
    found: list[str] = []
    log = "\n".join(tool_log or [])
    found += [f"tool:{t}" for t in WAKE_TOOLS if t in log]
    ids = set(_lineage(session_id)) if session_id else set()
    if not ids:
        return found
    found += [f"process:{p}" for p in _running_processes(ids)]
    try:
        from hermes_cli.kanban_session_activity import active_task_counts  # type: ignore
        counts = active_task_counts()
        if any(counts.get(i) for i in ids):
            found.append("kanban")
    except Exception:
        pass
    try:
        from hermes_cli.goal_session_activity import active_goal_sessions  # type: ignore
        if ids & set(active_goal_sessions()):
            found.append("goal")
    except Exception:
        pass
    return found


JUDGE_SYS = """You review an AI agent's final message for a turn, right before the turn ends, for a power user who wants
the agent to self-serve and to verify its own work. Judge ONLY from the evidence shown. Score three independent
probabilities (0-1):

- handoff: the message asks the USER to perform a step (click, sign in, run a command, paste, approve a dialog,
  restart an app, plug something in...) that the agent could plausibly have done itself with shell, browser
  automation, saved credentials, a password vault, a token broker, ssh, or background desktop control. Physical
  actions (plugging hardware, being at a device), the user's own judgment/taste, money/legal decisions, secrets
  only the user knows, and macOS privacy permission grants are NOT handoffs (score low).
  A CONDITIONAL ask ("if X isn't set up, you'll need to...", "may still need you if...") counts as a handoff when
  the agent could have checked X itself (membership, login state, a config value) and didn't: score it high.
- confirm: the message ends by asking permission for a next step that is reversible and clearly within what the
  user already asked for (e.g. "Want me to run the tests / apply the fix / commit it?"). Asking before something
  irreversible, external-facing (sending messages/emails, spending money, deleting data, force-push, publishing),
  or genuinely ambiguous in intent is NOT a needless confirm (score low). A plan presented for an explicit go
  when the user asked for a plan first is NOT needless.
- unverified: the message claims the work is done/fixed/working/deployed, but the turn's tool log shows no check
  that exercises the user's actual path (running the real command/app/URL/test the user cares about and seeing the
  right result). Building, type-checking, or testing something adjacent does not count. Saying explicitly that it
  is unverified or what remains unchecked is NOT an unverified claim (score low).
- unwatched: the message says the AGENT will act later, after some outside work finishes (runs, jobs, CI, builds,
  deploys, other agents): "I'll write the report once the runs finish", "waiting on the eval". A deterministic check
  already confirmed nothing is armed to wake this session afterwards (no background watcher, kanban card, goal or
  subagent). Score high when the agent's own follow-up depends on that outside work. Score low when the next step is
  the USER's (they will test, decide, or reply), when the agent says plainly that it is stopping and the user should
  ping it, or when the outside work's result needs no further action from the agent.

Calibrate: 0.8+ means you'd bet the user will push back ("do it yourself" / "just do it" / "you have access").

Return ONLY JSON: {"handoff": p, "confirm": p, "unverified": p, "unwatched": p, "evidence": "<=25 words citing the message/tool log"}"""


def _hermes_home():
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return get_hermes_home()
    except Exception:
        from pathlib import Path
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def profile_text() -> str:
    """The private user-environment overlay (COACH_REPLY_PROFILE or <HERMES_HOME>/coach/reply_profile.md), or ''."""
    from pathlib import Path
    p = Path(os.environ.get("COACH_REPLY_PROFILE") or (_hermes_home() / "coach" / "reply_profile.md"))
    try:
        return p.read_text()[:4000]
    except Exception:
        return ""


def judge_system() -> str:
    """JUDGE_SYS + the private user-environment overlay (COACH_REPLY_PROFILE or <HERMES_HOME>/coach/reply_profile.md),
    which tells the judge what this user's agents can reach (credential stores, hosts, browsers). Without it the judge
    can't tell a real hand-off from a lazy one."""
    prof = profile_text()
    return JUDGE_SYS + (f"\n\nUSER ENVIRONMENT (what this user's agents can reach; use it to judge handoffs):\n{prof}"
                        if prof.strip() else "")


def judge_prompt(user_ask: str, tool_log: list[str], reply: str) -> str:
    log = "\n".join(tool_log[-40:]) or "(no tool calls this turn)"
    return (f"USER'S REQUEST (latest):\n{(user_ask or '')[:1500]}\n\nTOOL LOG THIS TURN (oldest first, truncated):\n"
            f"{log[:5000]}\n\nAGENT'S FINAL MESSAGE:\n{(reply or '')[:4000]}")


def parse_verdict(raw: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    out: dict[str, Any] = {k: float(d.get(k) or 0) for k in RULES}
    out["evidence"] = str(d.get("evidence") or "")[:300]
    return out


NUDGES = {
    "handoff": ("[coach] Before ending the turn: your message asks the user to do a step themselves ({evidence}). "
                "The user wants agents to self-serve. Try it yourself first with what you have (shell, ssh, browser "
                "automation, browser_vault/saved logins, token-broker, background desktop control, memory/skills for "
                "access notes). Hand off only what truly needs their hands, judgment or approval, and say in one line "
                "why you couldn't do it."),
    "confirm": ("[coach] Your message ends by asking permission for a reversible step that is within what the user "
                "asked ({evidence}). The user prefers you proceed on reversible work: do it now, verify it, then "
                "report. Ask only before irreversible or external-facing actions."),
    "unverified": ("[coach] Your message says it's done/fixed, but this turn has no check on the user's actual path "
                   "({evidence}). Verify it the way the user will experience it (same command/app/URL/viewport/device) "
                   "and cite the evidence, or state plainly what is still unverified."),
    "unwatched": ("[coach] Your message promises to continue after outside work finishes ({evidence}), but nothing "
                  "will wake this session: no background process, kanban card, goal or subagent is attached, so "
                  "the turn ends, the sidebar shows it as done, and the follow-up never happens. Before ending, arm "
                  "a wake source: one `terminal(background=true, notify=true)` watcher that polls the real "
                  "completion signal (result files, job/CI status) with a sensible interval and a time cap, then "
                  "exits with a short summary. Use a kanban card instead for multi-hour work. If you are actually "
                  "handing the follow-up to the user, say so plainly instead of promising it."),
}


def settings() -> dict:
    """COACH_REPLY_RULES (comma list; "none" = off) and COACH_REPLY_THRESHOLD.

    Defaults come from the 2026-10-14 backtest (360 real replies, hindsight gold labels that saw the user's
    reaction): handoff at 0.7 fired 11, 10 right (precision 0.91, recall 0.33); confirm at 0.7 fired 4, all
    right (recall 0.11); unverified found no usable signal, so it is off by default."""
    raw = os.environ.get("COACH_REPLY_RULES", ",".join(DEFAULT_RULES))
    rules = [] if raw.strip().lower() in ("", "none", "0", "off") else [r for r in raw.split(",") if r in RULES]
    try:
        th = float(os.environ.get("COACH_REPLY_THRESHOLD", "0.7"))
    except ValueError:
        th = 0.7
    return {"rules": rules, "threshold": th}


def aux_judge(system: str, prompt: str) -> str:
    """Aux task `coach_reply` (auxiliary.coach_reply.{provider,model}; default: the auto aux model)."""
    from agent.auxiliary_client import call_llm  # type: ignore
    resp = call_llm(task="coach_reply", messages=[{"role": "system", "content": system},
                                                 {"role": "user", "content": prompt}],
                    max_tokens=2000, temperature=0, timeout=60)
    return (getattr(resp.choices[0].message, "content", None) or "").strip()


def check(reply: str, user_ask: str, tool_log: list[str], llm: Callable[[str, str], str],
          threshold: float = 0.8, only: Optional[list[str]] = None,
          changed_paths: Optional[list] = None,
          wake: Optional[Callable[[], list]] = None,
          screen: Optional[Callable[[list], Optional[dict]]] = None, screen_below: float = 0.2,
          shadow: bool = False, log: Optional[Callable[[dict], None]] = None,
          thresholds: Optional[dict] = None) -> Optional[dict]:
    """Return {"rule", "p", "evidence", "nudge"} for the top rule over threshold, else None.
    `thresholds` ({rule: th}) overrides `threshold` per rule; with it, the top rule among those over their own
    threshold wins.
    `wake()` lists the session's wake sources; any source drops the `unwatched` candidate before the judge runs
    (no wake callable = can't tell, so `unwatched` is skipped).
    `screen(candidates) -> {rule: p} | None` (a System One model) drops screened candidates below `screen_below`
    before the LLM judge; when no candidate survives, the LLM is skipped. `shadow` = log only, drop nothing."""
    cands = [r for r in prefilter(reply) if not only or r in only]
    if "unwatched" in cands:
        try:
            if wake is None or wake():
                cands.remove("unwatched")
        except Exception:
            cands.remove("unwatched")
    if not cands:
        return None
    sp = None
    if screen is not None:
        try:
            sp = screen(cands)
        except Exception:
            sp = None
    if sp:
        kept = [r for r in cands if r not in sp or sp[r] >= screen_below]
        if not shadow:
            if not kept:
                if log:
                    log({"name": "coach_reply", "screen": sp, "skipped_llm": True})
                return None
            cands = kept
    v = parse_verdict(llm(judge_system(), judge_prompt(user_ask, tool_log, reply)))
    if sp and log:
        log({"name": "coach_reply", "screen": sp, "skipped_llm": False, "shadow": shadow,
             "llm": {r: v[r] for r in sp} if v else None, "would_drop": [r for r in sp if sp[r] < screen_below]})
    if not v:
        return None
    if thresholds:
        passing = [r for r in cands if v[r] >= thresholds.get(r, threshold)]
        if not passing:
            return None
        best = max(passing, key=lambda r: v[r])
    else:
        best = max(cands, key=lambda r: v[r])
        if v[best] < threshold:
            return None
    return {"rule": best, "p": v[best], "evidence": v["evidence"], "verdict": v,
            "nudge": NUDGES[best].format(evidence=v["evidence"] or "see your last message")}
