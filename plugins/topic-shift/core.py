"""topic-shift core: detect a topic change in a just-sent message, and build a
context brief for carrying a session's state into a new session.

Pure logic + state.db reads (read-only). The LLM call is injected (`llm`) so
tests can fake it; production uses Hermes' auxiliary client (task
"topic_shift" → `auxiliary.topic_shift.*` config, else auto/cheap model).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Callable, Optional

MIN_WORDS = 3
MIN_PRIOR_USER_MSGS = 1  # check from the session's second message on
CONFIDENCE_THRESHOLD = 0.6  # on p_new, the probability the message starts a new task
PRIOR_USER_WINDOW = 8
JEV_DEFAULT_MODE = "shadow"  # System One (Jev) detection; flip with SYSTEM_ONE_MODE_TOPIC_SHIFT=on
BRIEF_MSG_WINDOW = 60

LLM = Callable[[list, int], str]  # (messages, max_tokens) -> text


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


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


def _db(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or hermes_home() / "state.db"
    con = _ro_connect(p, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _lineage(con, session_id: str, depth: int = 3) -> list[str]:
    """This session plus compression ancestors (oldest first)."""
    ids, cur = [], session_id
    for _ in range(depth + 1):
        row = con.execute("select id, parent_session_id, end_reason from sessions where id=?", (cur,)).fetchone()
        if not row:
            break
        ids.append(row["id"])
        parent = row["parent_session_id"]
        if not parent:
            break
        prow = con.execute("select end_reason from sessions where id=?", (parent,)).fetchone()
        # Only follow compression continuations, not subagent parents.
        if not prow or "compress" not in str(prow["end_reason"] or ""):
            break
        cur = parent
    return list(reversed(ids))


def session_info(session_id: str, db_path: Optional[Path] = None) -> dict:
    con = _db(db_path)
    try:
        row = con.execute("select * from sessions where id=?", (session_id,)).fetchone()
        if not row:
            return {}
        info = {k: row[k] for k in ("id", "title", "cwd", "git_branch", "git_repo_root", "model", "profile_name")
                if k in row.keys()}
        ids = _lineage(con, session_id)
        q = ",".join("?" * len(ids))
        msgs = con.execute(
            f"""select role, content, tool_calls, display_kind from messages
                where session_id in ({q}) and role in ('user','assistant')
                and coalesce(display_kind,'user') in ('user','') order by id""", ids).fetchall()
        info["messages"] = [dict(m) for m in msgs]
        return info
    finally:
        con.close()


def skip_reason(text: str, prior_user: list[str]) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return "empty"
    if t.startswith("/"):
        return "slash"
    if len(t.split()) < MIN_WORDS:
        return "short"
    if len(prior_user) < MIN_PRIOR_USER_MSGS:
        return "young_session"
    return None


def _before(messages: list[dict], text: str) -> list[dict]:
    """Messages strictly before the last occurrence of the just-sent `text` (it, and any reply
    already streaming after it, must not count as session context)."""
    msgs = [m for m in messages if m.get("content")]
    key = _norm(text)[:200]
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i]["role"] == "user" and _norm(msgs[i]["content"])[:200] == key:
            return msgs[:i]
    return msgs


def _prior_user(info: dict, text: str) -> list[str]:
    return [_norm(m["content"]) for m in _before(info.get("messages", []), text) if m["role"] == "user"]


def _parse_json(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    return json.loads(m.group(0)) if m else {}


CLASSIFY_SYSTEM = (
    "You decide whether a user's newest chat message starts a NEW task that would be better handled in a "
    "fresh session, or continues the session's current work. This user often switches tasks mid-session "
    "without saying so, so judge the GOAL of the message, not its overlap in vocabulary.\n"
    "CONTINUATION: approvals and answers to the assistant's questions ('do it', 'yes', '1 no 2 yes'), status "
    "checks, follow-ups, refinements, corrections, bug reports about what was just built, and small additions "
    "that only make sense on top of the current change.\n"
    "NEW TASK: a different goal that needs its own investigation or design and does not depend on finishing the "
    "recent work, EVEN on the same machine, project, repo or product. Examples: the session tuned a backup "
    "script on a server and the user now asks why that server keeps running out of memory; the session deployed "
    "a web app and the user now asks to change how a different service handles sign-in; the session fixed a "
    "login bug and the user asks for a recommendations feature in the same app. Sharing a host, repo or keyword is NOT enough "
    "to call it a continuation.\n"
    "Reply with ONLY JSON: "
    '{"p_new": <probability 0..1 that it is a NEW TASK>, "label": "<=6 word name of the NEWEST message\'s task", '
    '"reason": "<=15 words"}'
)


def _p_new(out: dict) -> float:
    """p_new from the reply; also accepts the older {new_topic, confidence} shape."""
    if out.get("p_new") is not None:
        return float(out["p_new"])
    conf = float(out.get("confidence") or 0)
    return conf if out.get("new_topic") else 1 - conf if "new_topic" in out else 0.0


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
    """SYSTEM_ONE_MODE_TOPIC_SHIFT: on (Jev decides; the LLM only names a detected shift) | shadow (both run, the LLM
    decides, both logged) | off."""
    c = _system_one()
    return c.mode("topic_shift", default=JEV_DEFAULT_MODE) if c else "off"


JEV_QUESTION = {"new_task": {
    "type": "noul",
    "instructions": ("Does `newest_message` start a NEW task with a different goal from the session's current work "
                     "(shown by `session_title`, `recent_user_messages` and `last_assistant_reply`)? Judge the goal of "
                     "the message, not shared words, host, repo or product."),
    "criteria": {
        "true": ("A different goal that needs its own investigation or design and does not depend on finishing the "
                 "recent work, even on the same machine, project, repo or product."),
        "false": ("Continues the current work: an approval or answer to the assistant's question ('do it', 'yes'), a "
                  "status check, follow-up, refinement, correction, bug report about what was just built, or a small "
                  "addition that only makes sense on top of the current change."),
    }}}


def jev_p_new(state: dict) -> Optional[float]:
    c = _system_one()
    if not c:
        return None
    return c.p(c.decide("topic_shift", state, JEV_QUESTION), "new_task")


def _jev_log(rec: dict) -> None:
    c = _system_one()
    if c:
        c.log(rec)


def classify(session_id: str, text: str, llm: LLM, db_path: Optional[Path] = None,
             jev: Optional[Callable[[dict], Optional[float]]] = None, mode: Optional[str] = None) -> dict:
    """`jev(state) -> p_new | None` is the System One judge (production: jev_p_new); `mode` defaults to jev_mode()."""
    info = session_info(session_id, db_path)
    if not info:
        return {"shift": False, "skipped": "unknown_session"}
    prior = _prior_user(info, text)
    if (why := skip_reason(text, prior)):
        return {"shift": False, "skipped": why}
    recent = prior[-PRIOR_USER_WINDOW:]
    last_asst = next((_norm(m["content"])[:600] for m in reversed(_before(info["messages"], text))
                      if m["role"] == "assistant" and m["content"]), "")
    mode = (mode or jev_mode()) if jev else "off"
    pj = None
    if mode in ("on", "shadow"):
        try:
            pj = jev({"session_title": info.get("title") or "(none)", "working_dir": info.get("cwd") or "?",
                      "recent_user_messages": [u[:300] for u in recent], "last_assistant_reply": last_asst,
                      "newest_message": _norm(text)[:1500]})
        except Exception:
            pj = None
    if mode == "on" and pj is not None and pj < CONFIDENCE_THRESHOLD:
        _jev_log({"name": "topic_shift", "session_id": session_id, "jev": pj, "skipped_llm": True})
        return {"shift": False, "confidence": pj, "label": "", "reason": "", "title": info.get("title") or "",
                "judge": "jev"}
    user = (
        f"Session title: {info.get('title') or '(none)'}\n"
        f"Working dir: {info.get('cwd') or '?'}\n"
        "Recent user messages (oldest first):\n" + "\n".join(f"- {u[:300]}" for u in recent) +
        f"\nLast assistant reply (truncated): {last_asst}\n\nNEWEST message:\n{_norm(text)[:1500]}"
    )
    try:
        out = _parse_json(llm([{"role": "system", "content": CLASSIFY_SYSTEM},
                               {"role": "user", "content": user}], 150))
    except Exception as exc:  # fail open: never block the user
        return {"shift": False, "skipped": f"llm_error: {type(exc).__name__}"}
    try:
        p = max(0.0, min(1.0, _p_new(out)))
    except (TypeError, ValueError):
        p = 0.0
    if pj is not None:
        _jev_log({"name": "topic_shift", "session_id": session_id, "jev": pj, "llm": p, "skipped_llm": False,
                  "mode": mode})  # id only (for hindsight labelling against state.db), never message text
    if mode == "on" and pj is not None:  # Jev decided "shift"; the LLM supplied the label
        return {"shift": True, "confidence": pj, "label": str(out.get("label") or "")[:60],
                "reason": str(out.get("reason") or "")[:140], "title": info.get("title") or "", "judge": "jev",
                "llm_confidence": p}
    return {"shift": p >= CONFIDENCE_THRESHOLD, "confidence": p, "label": str(out.get("label") or "")[:60],
            "reason": str(out.get("reason") or "")[:140], "title": info.get("title") or ""}


def _touched_files(messages: list[dict], limit: int = 15) -> list[str]:
    seen: list[str] = []
    for m in messages:
        for call in json.loads(m.get("tool_calls") or "[]") if m.get("tool_calls") else []:
            fn = (call.get("function") or {})
            if fn.get("name") not in ("write_file", "patch", "read_file"):
                continue
            try:
                path = json.loads(fn.get("arguments") or "{}").get("path")
            except Exception:
                path = None
            if path and path not in seen:
                seen.append(path)
    return seen[-limit:]


BRIEF_SYSTEM = (
    "You write a compact hand-off brief so a NEW agent session can continue work without the old transcript. "
    "Use only facts present in the transcript. Markdown, <=220 words, sections: **Was working on**, "
    "**Environment** (repo/dir/branch/hosts), **Key files**, **Decisions & facts**, **Open issues**, and "
    "**Relevant to the new request** (what from the old session matters for the NEW message; say 'nothing "
    "specific' if none). Omit empty sections."
)


def brief(session_id: str, new_text: str, llm: LLM, db_path: Optional[Path] = None) -> dict:
    info = session_info(session_id, db_path)
    if not info:
        return {"brief": "", "title": ""}
    msgs = [m for m in info["messages"] if m["content"]]
    # The just-sent message may still be in the transcript (with a partial reply after it):
    # cut from its last occurrence onward so it only appears as the NEW REQUEST.
    key = _norm(new_text)[:200]
    for i in range(len(msgs) - 1, max(len(msgs) - 6, -1), -1):
        if msgs[i]["role"] == "user" and _norm(msgs[i]["content"])[:200] == key:
            msgs = msgs[:i]
            break
    window = msgs[-BRIEF_MSG_WINDOW:]
    transcript = "\n".join(f"{m['role'].upper()}: {_norm(m['content'])[:700]}" for m in window)
    files = _touched_files(info["messages"])
    user = (
        f"Title: {info.get('title')}\nCwd: {info.get('cwd')}\nRepo: {info.get('git_repo_root')} "
        f"branch {info.get('git_branch')}\nFiles touched: {', '.join(files) or 'none'}\n\n"
        f"TRANSCRIPT (latest {len(window)} messages):\n{transcript[-24000:]}\n\nNEW REQUEST:\n{_norm(new_text)[:1500]}"
    )
    try:
        text = llm([{"role": "system", "content": BRIEF_SYSTEM}, {"role": "user", "content": user}], 600).strip()
    except Exception as exc:
        text = f"(brief unavailable: {type(exc).__name__}) Files touched: {', '.join(files) or 'none'}"
    return {"brief": text, "title": info.get("title") or "", "cwd": info.get("cwd") or "",
            "profile": info.get("profile_name") or "default"}


def seeded_message(session_id: str, profile: str, title: str, brief_md: str, text: str) -> str:
    ref = f"@session:{profile or 'default'}/{session_id}"
    head = f'Context carried over from {ref} ("{title}"):' if title else f"Context carried over from {ref}:"
    return f"{head}\n\n{brief_md}\n\n---\n\n{text}"


def aux_llm(messages: list, max_tokens: int) -> str:
    from agent.auxiliary_client import call_llm  # type: ignore
    resp = call_llm(task="topic_shift", messages=messages, max_tokens=max_tokens, temperature=0, timeout=30)
    choice = resp.choices[0]
    return (getattr(choice.message, "content", None) or "").strip()
