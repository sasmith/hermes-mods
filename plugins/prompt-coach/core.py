"""prompt-coach core: after a message is sent, decide whether it is a task request that would
benefit from a more ambitious framing (Felix Rieseberg's Altitude / Depth / Range lenses), and if so
write a rewritten request the user can launch in a fresh session.

Two stages, each one LLM call (injected as `llm(stage, messages, max_tokens)` so tests can fake it):
  1. classify (cheap; aux task "prompt_coach")        -> is it a task request with room to grow?
  2. rewrite  (strong; aux task "prompt_coach_rewrite") -> lens, critique, rewritten prompt.
Reads state.db read-only. Appends one JSON line per check/choice to <HERMES_HOME>/prompt-coach/log.jsonl.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

MIN_WORDS = 8
# Calibrated on a 236-request backtest of real history: the cheap classifier's gain clusters
# near 0.3 for anything with upside, so it is a loose pre-filter; the strong model's confidence is
# the real gate (>=0.8 fired on 8.5% of requests, all useful; the 0.7 band added generic boilerplate).
GAIN_THRESHOLD = 0.3        # stage-1 gate
CONFIDENCE_THRESHOLD = 0.8  # stage-2 gate
PRIOR_USER_WINDOW = 6
LENSES = ("altitude", "depth", "range")
# System One (Jev) screen in front of stage 1. Backtest 2026-10-19 (659 logged checks): every shown coaching had
# Jev >= 0.71, so a 0.6 screen skips 49% of stage-1 calls with 0/34 shown and 0/17 accepted lost. Jev is too generous
# to REPLACE stage 1 (a lossless gate would send ~3x more messages to the strong rewrite model), so it only screens.
JEV_DEFAULT_MODE = "on"      # SYSTEM_ONE_MODE_PROMPT_COACH=on|shadow|off
JEV_GATE = 0.6               # on: below this Jev p(worth coaching), stop before any LLM call

LLM = Callable[[str, list, int], str]  # (stage, messages, max_tokens) -> text


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def log_path() -> Path:
    return hermes_home() / "prompt-coach" / "log.jsonl"


def append_log(rec: dict, path: Optional[Path] = None) -> None:
    p = path or log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({"ts": time.time(), **rec}) + "\n")
    except Exception:
        pass  # logging must never break a check


_INJECTED = re.compile(r"<memory-context>.*?(</memory-context>|$)", re.S)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", _INJECTED.sub("", s or "")).strip()


def _parse_json(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


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


def session_context(session_id: str, text: str, db_path: Optional[Path] = None) -> dict:
    """Title/cwd plus user/assistant messages strictly before the just-sent `text`."""
    if not session_id:
        return {"title": "", "cwd": "", "prior_user": [], "last_assistant": ""}
    p = db_path or hermes_home() / "state.db"
    try:
        con = _ro_connect(p, timeout=5)
        con.row_factory = sqlite3.Row
    except Exception:
        return {"title": "", "cwd": "", "prior_user": [], "last_assistant": ""}
    try:
        row = con.execute("select * from sessions where id=?", (session_id,)).fetchone()
        info: dict = {"title": (row["title"] if row else "") or "", "cwd": (row["cwd"] if row and "cwd" in row.keys() else "") or ""}
        msgs = [dict(m) for m in con.execute(
            """select role, content from messages where session_id=? and role in ('user','assistant')
               and coalesce(display_kind,'user') in ('user','') and coalesce(content,'')<>'' order by id""",
            (session_id,)).fetchall()]
    except Exception:
        info, msgs = {"title": "", "cwd": ""}, []
    finally:
        con.close()
    key = _norm(text)[:200]
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i]["role"] == "user" and _norm(msgs[i]["content"])[:200] == key:
            msgs = msgs[:i]
            break
    info["prior_user"] = [_norm(m["content"]) for m in msgs if m["role"] == "user"]
    info["last_assistant"] = next((_norm(m["content"])[:600] for m in reversed(msgs) if m["role"] == "assistant"), "")
    return info


def skip_reason(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return "empty"
    if t.startswith("/"):
        return "slash"
    if len(t.split()) < MIN_WORDS:
        return "short"
    if t.lower().startswith(("context carried over from @session", "[prompt-coach]")):
        return "seeded"  # our own / topic-shift's launched messages
    if "[prompt-coach] Rewritten from" in t:
        return "seeded"  # a rewrite we launched: don't coach it again
    return None


CLASSIFY_SYSTEM = (
    "You screen chat messages sent to an AI agent that has a computer, tools and the user's data. Decide "
    "whether the NEWEST message is a fresh TASK REQUEST whose framing leaves a lot on the table, so that a "
    "rewritten request would get a materially better outcome. NOT worth coaching: follow-ups, answers to "
    "the agent's questions, corrections, approvals ('ok', 'do it'), bug reports on what was just built, "
    "quick factual questions, chit-chat, and requests that are already outcome-level, specific and "
    "well-scoped. WORTH coaching: the user asks for a single step when the real goal is an ongoing outcome "
    "(altitude); asks for a shallow/digested version of something an expert would do from raw data with "
    "real tools and outside validation (depth); or frames it narrowly where other fields' methods would "
    "change the result (range). Reply with ONLY JSON: "
    '{"task_request": bool, "followup": bool, "gain": 0..1, "lens": "altitude|depth|range", '
    '"why": "<=20 words"}'
)

REWRITE_SYSTEM = """You coach a user to ask their AI agent (which has a computer, can install software, run code, browse, and reach the user's files and connected apps) for more ambitious work. Use three lenses:

ALTITUDE - ask for the outcome, not the step. "What do you want to happen next? What's the goal beyond the goal?" Instead of "fix this crash", "keep the app crash-free" (and give the agent the access it needs to do that independently: logs, machines, data). Best when there is a real end outcome; weaker for purely creative work.
DEPTH - have it done the way a field-leading expert would: start from the rawest material, not a digest; insist on the instruments experts actually use (databases, statistical models, profilers, simulations - not markdown tables); set unreasonably high targets and say they are reasonable (200ms, not 5s); adversarially test assumptions against raw data; produce new knowledge; and require an OUTSIDE judge (a test, a metric, a held-out dataset, a tool the agent can run) - never the model grading itself.
RANGE - restate nouns as verbs (a QBR isn't "a deck", it's "getting busy people to believe a cause-and-effect story and decide under uncertainty in 30 minutes"), then pull in methods from other fields (a journalist's, a forensic accountant's, a short seller's research process). Ask the agent to DO what those people do, never to role-play them.

Given the user's message and the session context, pick the ONE lens with the most upside and rewrite the request. The rewrite:
- is written in the user's voice, as a prompt they can send as-is to a fresh agent session;
- keeps every concrete fact, path, name and constraint from the original;
- states the real outcome, the raw material to start from, the methods/tools, a stretch target, and the outside check that proves it worked - as far as they apply;
- stays realistic about what the agent can access; if access must be set up first, make that the first step;
- is at most ~150 words. No preamble, no lens names inside it.

If the original is already ambitious and well-scoped, set confidence low. Reply with ONLY JSON:
{"lens": "altitude|depth|range", "critique": "<=25 words, second person, what the original leaves on the table", "rewrite": "...", "confidence": 0..1}"""


def _context_block(info: dict, text: str) -> str:
    recent = info.get("prior_user", [])[-PRIOR_USER_WINDOW:]
    return (
        f"Session title: {info.get('title') or '(new session)'}\n"
        f"Working dir: {info.get('cwd') or '?'}\n"
        f"Earlier user messages in this session: {len(info.get('prior_user', []))}\n"
        + ("Recent user messages (oldest first):\n" + "\n".join(f"- {u[:300]}" for u in recent) + "\n" if recent else "")
        + (f"Last assistant reply (truncated): {info.get('last_assistant')}\n" if info.get("last_assistant") else "")
        + f"\nNEWEST message:\n{_norm(text)[:2000]}"
    )


def _system_one():
    """plugins/system-one/client.py next to this plugin (symlinks resolved), or None."""
    try:
        import importlib.util
        import sys
        path = Path(__file__).resolve().parent.parent / "system-one" / "client.py"
        if not path.exists():
            return None
        mod = sys.modules.get("_system_one_client")
        if mod is None:
            spec = importlib.util.spec_from_file_location("_system_one_client", path)
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            mod = sys.modules.setdefault("_system_one_client", mod)  # register only once fully loaded (threads)
        return mod
    except Exception:
        return None


def jev_mode() -> str:
    """SYSTEM_ONE_MODE_PROMPT_COACH: on (Jev screens out clear no's before the stage-1 LLM) | shadow (both run, the LLM
    decides) | off."""
    c = _system_one()
    return c.mode("prompt_coach", default=JEV_DEFAULT_MODE) if c else "off"


JEV_QUESTION = {"worth_coaching": {
    "type": "noul",
    "instructions": ("Is `newest_message` a fresh TASK REQUEST to an AI agent (which has a computer, tools and the "
                     "user's data) whose framing leaves a lot on the table, so that a rewritten request would get a "
                     "materially better outcome?"),
    "criteria": {
        "true": ("It asks for a single step when the real goal is an ongoing outcome; or a shallow version of something "
                 "an expert would do from raw data with real tools and outside validation; or frames it narrowly where "
                 "other fields' methods would change the result."),
        "false": ("A follow-up, an answer to the agent's question, a correction, an approval ('ok', 'do it'), a bug "
                  "report on what was just built, a quick factual question, chit-chat, or a request that is already "
                  "outcome-level, specific and well-scoped."),
    }}}


def jev_state(info: dict, text: str) -> dict:
    return {"session_title": info.get("title") or "(new session)", "working_dir": info.get("cwd") or "?",
            "earlier_user_message_count": len(info.get("prior_user", [])),
            "recent_user_messages": [u[:300] for u in info.get("prior_user", [])[-PRIOR_USER_WINDOW:]],
            "last_assistant_reply": info.get("last_assistant") or "", "newest_message": _norm(text)[:2000]}


def jev_worth(state: dict) -> Optional[float]:
    c = _system_one()
    if not c:
        return None
    return c.p(c.decide("prompt_coach", state, JEV_QUESTION), "worth_coaching")


def _jev_log(rec: dict) -> None:
    c = _system_one()
    if c:
        c.log(rec)


def coach(session_id: str, text: str, llm: LLM, db_path: Optional[Path] = None,
          log: Optional[Path] = None, write_log: bool = True,
          jev: Optional[Callable[[dict], Optional[float]]] = None, mode: Optional[str] = None) -> dict:
    """`jev(state) -> p | None` is the System One stage-1 judge (production: jev_worth); `mode` defaults to
    jev_mode(). on: below JEV_GATE no LLM runs; otherwise both stages run unchanged."""
    cid = uuid.uuid4().hex[:12]
    base = {"id": cid, "coach": False}
    if (why := skip_reason(text)):
        return {**base, "skipped": why}
    info = session_context(session_id, text, db_path)
    ctx = _context_block(info, text)
    mode = (mode or jev_mode()) if jev else "off"
    pj = None
    if mode in ("on", "shadow"):
        try:
            pj = jev(jev_state(info, text))
        except Exception:
            pj = None
    if mode == "on" and pj is not None and pj < JEV_GATE:
        _jev_log({"name": "prompt_coach", "jev": pj, "skipped_llm": True})
        if write_log:
            append_log({"event": "check", "id": cid, "session_id": session_id, "text": _norm(text)[:2000],
                        "stage1": {"judge": "jev", "p": pj}, "coach": False}, log)
        return {**base, "skipped": "not_worth_it", "gain": pj, "why": "system-one screen"}
    try:
        s1 = _parse_json(llm("classify", [{"role": "system", "content": CLASSIFY_SYSTEM},
                                          {"role": "user", "content": ctx}], 200))
    except Exception as exc:  # fail open: never block the user
        return {**base, "skipped": f"llm_error: {type(exc).__name__}"}
    if pj is not None:
        g = float(s1.get("gain") or 0)
        _jev_log({"name": "prompt_coach", "jev": pj, "skipped_llm": False, "mode": mode,
                  "llm_pass": bool(s1.get("task_request") and not s1.get("followup") and g >= GAIN_THRESHOLD)})
    gain = float(s1.get("gain") or 0)
    rec = {"id": cid, "session_id": session_id, "text": _norm(text)[:2000], "stage1": s1}
    if not s1.get("task_request") or s1.get("followup") or gain < GAIN_THRESHOLD:
        if write_log:
            append_log({"event": "check", **rec, "coach": False}, log)
        return {**base, "skipped": "not_worth_it", "gain": gain, "why": str(s1.get("why") or "")[:160]}
    try:
        s2 = _parse_json(llm("rewrite", [{"role": "system", "content": REWRITE_SYSTEM},
                                         {"role": "user", "content": ctx}], 900))
    except Exception as exc:
        return {**base, "skipped": f"llm_error: {type(exc).__name__}"}
    conf = float(s2.get("confidence") or 0)
    rewrite = str(s2.get("rewrite") or "").strip()
    lens = str(s2.get("lens") or s1.get("lens") or "").lower()
    lens = lens if lens in LENSES else "altitude"
    ok = bool(rewrite) and conf >= CONFIDENCE_THRESHOLD and _norm(rewrite) != _norm(text)
    out = {**base, "coach": ok, "lens": lens, "critique": str(s2.get("critique") or "")[:220],
           "rewrite": rewrite[:3000], "confidence": conf, "gain": gain}
    if not ok:
        out["skipped"] = "low_confidence"
    if write_log:
        append_log({"event": "check", **rec, **{k: out[k] for k in ("coach", "lens", "critique", "rewrite", "confidence")}}, log)
    return out


CHOICES = ("new_session", "replace", "keep", "mute", "dismissed")


def record_choice(cid: str, choice: str, edited: bool = False, log: Optional[Path] = None) -> dict:
    if choice not in CHOICES:
        return {"ok": False, "error": f"choice must be one of {CHOICES}"}
    append_log({"event": "choice", "id": cid, "choice": choice, "edited": bool(edited)}, log)
    return {"ok": True}


def seeded_message(session_id: str, profile: str, rewrite: str, original: str) -> str:
    """First message of the launched session: the rewrite, plus a pointer to where it came from."""
    ref = f"@session:{profile or 'default'}/{session_id}" if session_id else "another session"
    return (f"{rewrite.strip()}\n\n---\n[prompt-coach] Rewritten from my request in {ref}, which is still running "
            f"separately. My original wording: \"{_norm(original)[:600]}\"")


def stats(log: Optional[Path] = None, since_ts: float = 0) -> dict:
    """Aggregate the log for the weekly digest."""
    p = log or log_path()
    checks, choices = {}, {}
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ts", 0) < since_ts:
                continue
            if r.get("event") == "check":
                checks[r["id"]] = r
            elif r.get("event") == "choice":
                choices[r["id"]] = r
    shown = [c for c in checks.values() if c.get("coach")]
    by_lens: dict = {}
    for c in shown:
        by_lens[c.get("lens")] = by_lens.get(c.get("lens"), 0) + 1
    picked: dict = {}
    for ch in choices.values():
        picked[ch["choice"]] = picked.get(ch["choice"], 0) + 1
    return {"checked": len(checks), "shown": len(shown), "by_lens": by_lens, "choices": picked,
            "examples": [{k: c.get(k) for k in ("lens", "text", "critique", "rewrite")} | {"choice": choices.get(c["id"], {}).get("choice")}
                         for c in shown[-20:]]}


def _aux(task: str, messages: list, max_tokens: int) -> str:
    from agent.auxiliary_client import call_llm  # type: ignore
    resp = call_llm(task=task, messages=messages, max_tokens=max_tokens, temperature=0, timeout=60)
    return (getattr(resp.choices[0].message, "content", None) or "").strip()


def aux_llm(stage: str, messages: list, max_tokens: int) -> str:
    """classify -> aux task "prompt_coach"; rewrite -> "prompt_coach_rewrite". Pick models with
    auxiliary.prompt_coach.{provider,model} / auxiliary.prompt_coach_rewrite.{provider,model}."""
    return _aux("prompt_coach" if stage == "classify" else "prompt_coach_rewrite", messages, max_tokens)
