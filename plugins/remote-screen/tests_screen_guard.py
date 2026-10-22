"""Tests for the local Screen Sharing guard (pre_tool_call). Run:
uv run --with pytest --with cryptography --with pillow python -m pytest -q -p no:cacheprovider tests_screen_guard.py"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("remote_screen_plugin_guardtest", HERE / "__init__.py")
rs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rs
_spec.loader.exec_module(rs)

CMDS = [
    "open -a \"Screen Sharing\" 'vnc://you@192.0.2.10'",
    "open vnc://you@192.0.2.10",
    "open -b com.apple.ScreenSharing",
    "open -a 'Screen Sharing.app'",
    "cd /tmp && open -g 'vnc://192.0.2.10'",
    "open -n -a /System/Applications/Utilities/Screen\\ Sharing.app",
    "open -a Screen\\ Sharing --args vnc://192.0.2.10",
]


@pytest.mark.parametrize("code", [
    "import webbrowser\nwebbrowser.open('vnc://you@192.0.2.10')",
    "import webbrowser; webbrowser.open_new_tab(\"vnc://192.0.2.10\")",
    "import os; os.startfile('vnc://192.0.2.10')",
    "from AppKit import NSWorkspace, NSURL\nNSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_('vnc://192.0.2.10'))",
])
def test_execute_code_python_openers_blocked(code):
    r = _call("execute_code", {"code": code})
    assert r and r["action"] == "block"


def test_execute_code_printing_vnc_allowed():
    assert _call("execute_code", {"code": "print('host: vnc://192.0.2.10')"}) is None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_CRON_SESSION"):
        monkeypatch.delenv(k, raising=False)
    rs.TURNS.clear()


def _call(tool, args, session="s1"):
    return rs.pre_tool_call(tool_name=tool, args=args, session_id=session)


def _chat(session, text, platform="cli", **kw):
    rs.pre_llm_call(session_id=session, user_message=text, platform=platform, **kw)


@pytest.mark.parametrize("cmd", CMDS)
def test_terminal_blocked(cmd):
    r = _call("terminal", {"command": cmd})
    assert r and r["action"] == "block"
    assert "remote_screenshot" in r["message"] and "::screenshare" in r["message"]


def test_execute_code_blocked():
    code = "import subprocess\nsubprocess.run(['open','-a','Screen Sharing','vnc://you@192.0.2.10'])"
    r = _call("execute_code", {"code": code})
    assert r and r["action"] == "block"
    r = _call("mcp__execute_code", {"code": "import os; os.system('open vnc://192.0.2.10')"})
    assert r and r["action"] == "block"


def test_browser_exec_and_tool_call_wrapped_blocked():
    r = _call("mcp__tool_call", {"calls": [{"name": "terminal", "arguments": {"command": CMDS[1]}}]})
    assert r and r["action"] == "block"


def test_unrelated_commands_allowed():
    assert _call("terminal", {"command": "open https://example.com"}) is None
    assert _call("terminal", {"command": "open -a Safari"}) is None
    assert _call("terminal", {"command": "echo vnc is nice"}) is None
    assert _call("terminal", {"command": "grep -n 'vnc://' notes.md"}) is None


def test_blocked_without_explicit_request():
    _chat("s1", "please check the zoom room for me")
    assert _call("terminal", {"command": CMDS[0]})["action"] == "block"


def test_allowed_when_user_explicitly_asks_in_chat():
    _chat("s1", "Open Screen Sharing to the main room mac please")
    assert _call("terminal", {"command": CMDS[0]}) is None
    assert _call("execute_code", {"code": "import os; os.system('open vnc://192.0.2.10')"}) is None


def test_request_is_per_turn():
    _chat("s1", "open screen sharing to main")
    _chat("s1", "thanks, now check disk space")
    assert _call("terminal", {"command": CMDS[0]})["action"] == "block"


def test_mere_mention_is_not_a_request():
    _chat("s1", "why did a worker open screen sharing earlier?")
    assert _call("terminal", {"command": CMDS[0]})["action"] == "block"


def test_kanban_worker_always_blocked(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    _chat("s1", "open screen sharing to main")
    assert _call("terminal", {"command": CMDS[0]})["action"] == "block"


def test_cron_and_delegate_always_blocked(monkeypatch):
    _chat("c1", "open screen sharing to main", platform="cron")
    assert _call("terminal", {"command": CMDS[0]}, session="c1")["action"] == "block"
    _chat("d1", "open screen sharing to main", parent_session_id="p1")
    assert _call("terminal", {"command": CMDS[0]}, session="d1")["action"] == "block"
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    _chat("e1", "open screen sharing to main")
    assert _call("terminal", {"command": CMDS[0]}, session="e1")["action"] == "block"
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert _call("terminal", {"command": CMDS[0]}, session="e1")["action"] == "block"


def test_hooks_registered_even_without_deps(monkeypatch):
    seen = {}

    class Ctx:
        def register_hook(self, name, fn): seen[name] = fn
        def register_tool(self, **kw): pass

    monkeypatch.setattr(rs, "_deps_ok", lambda: False)
    rs.register(Ctx())
    assert {"pre_tool_call", "pre_llm_call"} <= set(seen)
