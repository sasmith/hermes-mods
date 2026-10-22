"""open-asks: every question a chat reply leaves for the user is tracked as a blocked Kanban card.

Why: a coordinator chat ended a turn with "Want me to set up X?" and no card. The sidebar's "waiting on
you" state and the blocked-cards digest only read Kanban cards, so the ask was invisible and sat unanswered
behind later notices. A rule in a skill didn't help: the skill wasn't loaded in that session. See README.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from . import core
except ImportError:  # pytest/spec loading imports the hyphenated plugin dir without a parent package
    import importlib.util as _ilu
    import sys as _sys
    _spec = _ilu.spec_from_file_location("open_asks_core", Path(__file__).with_name("core.py"))
    core = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(core)  # type: ignore[union-attr]
    core = _sys.modules.setdefault("open_asks_core", core)

MAX_CARDS_PER_HOUR = 4
SKIP_PLATFORMS = {"cron", "kanban", "webhook", "subagent"}


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _state():
    return core.State(_home() / "cache" / "open-asks" / "state.json")


def _boards() -> list[Path]:
    root = _home()
    paths = [root / "kanban.db"]
    paths += sorted((root / "kanban" / "boards").glob("*/kanban.db"))
    return [p for p in paths if p.is_file()]


def _profile() -> str:
    return os.environ.get("HERMES_SESSION_PROFILE") or os.environ.get("HERMES_PROFILE") or "default"


def _eligible(session_id: str, platform: str = "", parent_session_id: str = "") -> bool:
    """Interactive chats only: Kanban workers block their own card, cron/subagents have no user present."""
    if not session_id or parent_session_id:
        return False
    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_OPEN_ASKS_OFF"):
        return False
    return (platform or "").lower() not in SKIP_PLATFORMS


# ---- hooks ---------------------------------------------------------------------------------------------

def pre_llm_call(session_id: str = "", platform: str = "", parent_session_id: str = "", **_) -> dict | None:
    if not _eligible(session_id, platform, parent_session_id):
        return None
    st = _state()
    st.turn_started(session_id)
    cards = st.cards(session_id)
    if not cards:
        return None
    live = core.statuses(_boards(), [c["id"] for c in cards])
    closed = {c["id"] for c in cards if live.get(c["id"]) not in ("blocked",)}
    if closed:
        st.drop_cards(session_id, closed)
    still = [c for c in cards if c["id"] not in closed]
    return {"context": core.reminder(still)} if still else None


def transform_tool_result(tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "", **_):
    """Remember that this turn already filed/blocked a card, so a reply pointing at it isn't double-filed."""
    try:
        a = args if isinstance(args, dict) else json.loads(args or "{}")
    except (TypeError, ValueError):
        a = {}
    if session_id and (tool_name == "kanban_block"
                       or (tool_name == "kanban_create" and str(a.get("initial_status")) == "blocked")):
        _state().mark_tracked(session_id)
    return None


def _create_card(session_id: str, asks: list[str], reply: str) -> str | None:
    ref = f"@session:{_profile()}/{session_id}"
    when = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    args = {"title": core.card_title(asks), "assignee": "default", "initial_status": "blocked",
            "body": core.card_body(ref, asks, reply, when), "idempotency_key": core.ask_key(session_id, asks)}
    # In-process tool call first: it links the card to this chat exactly like the agent's own kanban_create.
    try:
        from tools.registry import registry
        out = registry.dispatch("kanban_create", args)
        d = out if isinstance(out, dict) else json.loads(out)
        if d.get("task_id"):
            _fix_subscription(d["task_id"], session_id)
            return d["task_id"]
    except Exception:
        pass
    return _create_via_cli(args, session_id)


def _fix_subscription(task_id: str, session_id: str) -> None:
    """Link the card to the chat that asked, and only to it. kanban_create resolves its chat from the process's
    session env, which can name a different chat (e.g. a `hermes chat` started from another session's terminal)."""
    subs = core.subscriptions(_boards(), task_id)
    if ("tui", session_id) not in subs:
        _subscribe(task_id, session_id)
    for platform, chat in subs:
        if platform == "tui" and chat != session_id:
            _run(["kanban", "notify-unsubscribe", task_id, "--platform", "tui", "--chat-id", chat])


def _hermes() -> str | None:
    return shutil.which("hermes") or next((p for p in (str(Path.home() / ".local/bin/hermes"),) if Path(p).exists()), None)


def _create_via_cli(args: dict, session_id: str) -> str | None:
    exe = _hermes()
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "kanban", "create", args["title"], "--body-file", "-", "--assignee", "default",
                            "--initial-status", "blocked", "--idempotency-key", args["idempotency_key"], "--json"],
                           input=args["body"], capture_output=True, text=True, timeout=30)
        tid = (json.loads(p.stdout or "{}") or {}).get("id") or (json.loads(p.stdout or "{}") or {}).get("task_id")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if tid:
        _subscribe(tid, session_id)
    return tid


def _subscribe(task_id: str, session_id: str) -> None:
    _run(["kanban", "notify-subscribe", task_id, "--platform", "tui", "--chat-id", session_id])


def _run(argv: list[str]) -> None:
    exe = _hermes()
    if exe:
        try:
            subprocess.run([exe, *argv], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass


def _gate_refused(session_id: str) -> bool:
    """The ask-gate plugin (if installed) judged this turn's asks unverified/stale: never file a card for them.
    No record = ask-gate absent or down, so behave as before."""
    try:
        v = json.loads((_home() / "cache" / "ask-gate" / "verdicts.json").read_text()).get(session_id)
    except (OSError, ValueError):
        return False
    return bool(v) and time.time() - v.get("t", 0) <= 300 and v.get("verdict") in ("held", "unverified")


def transform_llm_output(response_text: str = "", session_id: str = "", platform: str = "", **_):
    if not isinstance(response_text, str) or not _eligible(session_id, platform):
        return None
    asks = core.find_asks(response_text)
    if not asks:
        return None
    if _gate_refused(session_id):
        return None  # ask-gate could not verify the ask; a blocked card would just publish the unverified premise
    st = _state()
    if st.tracked_since(session_id, st.turn_start(session_id) or time.time() - 600):
        return None  # this turn already filed/blocked a card for it
    if core.blocked_ids(_boards(), core.card_ids(" ".join(asks))):
        return None  # the ask points at a card that is already blocked on the user
    if st.recent_count(session_id) >= MAX_CARDS_PER_HOUR:
        return None
    tid = _create_card(session_id, asks, response_text)
    if not tid:
        return None
    st.add_card(session_id, tid, asks)
    st.mark_tracked(session_id)
    return response_text.rstrip() + f"\n\n_Tracked as blocked card {tid} so this shows as waiting on you._"


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("transform_llm_output", transform_llm_output)
