"""branch-pin-guard: keep pinned checkouts (sync mirrors, deploy checkouts) on their branch.

- ``pre_tool_call`` blocks terminal / execute_code / process calls whose `git checkout|switch` would move a pinned
  checkout off its branch, and points the agent at `git worktree add`.
- ``transform_tool_result`` is the safety net: after a call that ran checkout/switch, if any pinned checkout is off
  its branch anyway (cwd the hook couldn't see, a script), it appends how to put it back.
Pins: ``$HERMES_HOME/branch-pins.json`` ({"~/code/repo": "branch"}). ``claude_hook.py`` = the Claude Code version.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

try:
    from . import guard
except ImportError:  # loaded without a parent package (tests, hyphenated dir)
    import importlib.util
    from pathlib import Path

    _spec = importlib.util.spec_from_file_location("branch_pin_guard_guard", Path(__file__).with_name("guard.py"))
    guard = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(guard)

logger = logging.getLogger(__name__)

SHELL_TOOLS = {"terminal", "process_manage", "process"}
PY_TOOLS = {"execute_code"}


def _cwd(args: dict) -> Optional[str]:
    return args.get("workdir") or os.environ.get("TERMINAL_CWD") or None


def classify(tool_name: str, args: Any) -> Optional[str]:
    args = args if isinstance(args, dict) else {}
    if tool_name in SHELL_TOOLS:
        cmd = " ; ".join(str(args.get(k) or "") for k in ("command", "input", "data"))
        return guard.check_shell(cmd, _cwd(args))
    if tool_name in PY_TOOLS:
        return guard.check_python(str(args.get("code") or ""), _cwd(args))
    if tool_name in {"tool_call", "mcp__tool_call"}:
        for call in args.get("calls") or []:
            if isinstance(call, dict):
                why = classify(str(call.get("name") or ""), call.get("arguments"))
                if why:
                    return why
    return None


def _pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict]:
    try:
        if not guard.enabled():
            return None
        why = classify(tool_name, args)
    except Exception:  # never let this plugin break a tool call
        logger.debug("branch-pin-guard classify failed", exc_info=True)
        return None
    if not why:
        return None
    logger.info("branch-pin-guard blocked %s: %s", tool_name, why)
    return {"action": "block", "message": guard.message(why)}


def _mentions_switch(args: Any) -> bool:
    text = " ".join(str(v) for v in (args or {}).values()) if isinstance(args, dict) else str(args or "")
    return "checkout" in text or "switch" in text


def _transform_tool_result(tool_name: str = "", args: Any = None, result: Any = None, **_: Any):
    try:
        if (not guard.enabled() or tool_name not in SHELL_TOOLS | PY_TOOLS or not isinstance(result, str)
                or "[branch-pin-guard]" in result or not _mentions_switch(args)):
            return None
        drift = guard.off_pin()
        return result + guard.drift_note(drift) if drift else None
    except Exception:
        logger.debug("branch-pin-guard drift check failed", exc_info=True)
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
