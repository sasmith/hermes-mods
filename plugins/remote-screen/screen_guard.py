"""Guard: agents must not open macOS Screen Sharing (vnc://) on this Mac on their own.

A Screen Sharing window in control mode takes key focus, hides the local cursor and captures Cmd-Tab, which
locks the user out of their own desktop. A ``pre_tool_call`` hook refuses terminal / execute_code commands that
launch it. Allowed only when the current user turn of an interactive chat explicitly asks for it. Kanban
workers, cron runs and delegated subagents are always refused.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Optional

SHELL_TOOLS = {"terminal", "process_manage", "process", "mcp__terminal", "mcp__process_manage"}
PY_TOOLS = {"execute_code", "mcp__execute_code"}
WRAP_TOOLS = {"tool_call", "mcp__tool_call"}

# [\\\s]* also matches shell-escaped spaces: /System/Applications/Utilities/Screen\ Sharing.app
_TARGET = r"(?:vnc://|screen[\\\s]*sharing|com\.apple\.screensharing)"
_SHELL_RE = re.compile(r"\bopen\b[^\n;|&]*?" + _TARGET, re.I)
_OSA_RE = re.compile(r"\bosascript\b.*" + _TARGET, re.I | re.S)
# any Python URL/app opener: subprocess 'open' argv, webbrowser.open*/obj.open(, os.startfile, NSWorkspace openURL*
_PY_OPEN_RE = re.compile(
    r"""(?:["'\s,\[]open["'\s,\]]|\.open\w*\s*\(|\bstartfile\b|\bopenURL\w*|\blaunchApplication\w*)""", re.I)
_PY_TARGET_RE = re.compile(_TARGET, re.I)

# explicit request: an imperative / polite opener right before "open ... screen sharing", no negation anywhere
_ASK_RE = re.compile(
    r"(?:^|[.!?\n,;]\s*|\b(?:please|pls|can you|could you|would you|will you|go ahead and|and|then|just|now|"
    r"i want you to|i need you to|want to|need to|let'?s|lets)\s+)"
    r"(?:open|launch|start|connect)\b[^.!?\n]{0,60}?(?:screen\s*shar\w*|screenshare|vnc)", re.I)
_NEG_RE = re.compile(r"\b(?:don'?t|do not|never|stop|without|why|did|didn'?t|shouldn'?t|must not)\b", re.I)

TURNS: dict[str, dict] = {}  # session_id -> {"allow": bool, "blocked": bool}
_LOCK = threading.Lock()


def enabled() -> bool:
    return os.environ.get("HERMES_SCREEN_SHARE_GUARD", "1").strip().lower() not in {"0", "off", "false", "no"}


def explicit_request(text: str) -> bool:
    text = text or ""
    return bool(_ASK_RE.search(text)) and not _NEG_RE.search(text)


def pre_llm_call(session_id: str = "", user_message: Any = "", platform: str = "", parent_session_id: str = "",
                 **_: Any) -> None:
    sid = session_id or ""
    msg = user_message if isinstance(user_message, str) else ""
    blocked = (platform or "").lower() in {"cron", "kanban", "webhook", "api_server"} or bool(parent_session_id)
    with _LOCK:
        TURNS[sid] = {"allow": explicit_request(msg), "blocked": blocked}
        if len(TURNS) > 500:
            for k in list(TURNS)[:250]:
                TURNS.pop(k, None)
    return None


def _env_blocked() -> bool:
    return any(os.environ.get(k) for k in ("HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_CRON_SESSION"))


def launches_screen_sharing(tool_name: str, args: Any) -> bool:
    args = args if isinstance(args, dict) else {}
    if tool_name in SHELL_TOOLS:
        text = " ".join(str(args.get(k) or "") for k in ("command", "input", "data"))
        return bool(_SHELL_RE.search(text) or _OSA_RE.search(text))
    if tool_name in PY_TOOLS:
        code = str(args.get("code") or "")
        return bool((_PY_OPEN_RE.search(code) and _PY_TARGET_RE.search(code)) or _OSA_RE.search(code))
    if tool_name in WRAP_TOOLS:
        return any(isinstance(c, dict) and launches_screen_sharing(str(c.get("name") or ""), c.get("arguments"))
                   for c in args.get("calls") or [])
    return False


MESSAGE = (
    "Blocked: agents must not open Screen Sharing (vnc://) on the user's own Mac. A control-mode Screen Sharing "
    "window takes key focus, hides the cursor and captures Cmd-Tab, locking the user out of their desktop. "
    "To act on a remote Mac use remote_screenshot, remote_input or remote_approve_dialog (no window opens). "
    "If the user needs to look at it, put a ::screenshare{host=\"<verified host or ip>\" label=\"<computer name>\"} "
    "button in your reply or card summary so they can click it themselves. Only an interactive chat turn that "
    "explicitly asks you to open Screen Sharing is allowed."
)


def pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> Optional[dict]:
    try:
        if not enabled() or not launches_screen_sharing(tool_name, args):
            return None
        st = TURNS.get(session_id or "") or {}
        if not _env_blocked() and st.get("allow") and not st.get("blocked"):
            return None
    except Exception:  # never let the guard break a tool call
        return None
    return {"action": "block", "message": MESSAGE}
