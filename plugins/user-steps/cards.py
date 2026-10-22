"""Rolling 'waiting on you' card ops for user-steps.

create(): the agent's own kanban_create tool in-process (links the card to the chat, like the agent calling it),
falling back to `hermes kanban create` + notify-subscribe. update()/archive(): the hermes CLI, so the dashboard
backend (a different process with no tool registry) can archive the card when a button resolves the last step.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def boards(h: Optional[Path] = None) -> list[tuple[str, Path]]:
    root = h or _home()
    out = [("default", root / "kanban.db")]
    out += [(p.parent.name, p) for p in sorted((root / "kanban" / "boards").glob("*/kanban.db"))]
    return [(b, p) for b, p in out if p.is_file()]


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


def board_of(task_id: str, h: Optional[Path] = None) -> Optional[str]:
    for slug, p in boards(h):
        try:
            c = _ro_connect(p, timeout=2)
            try:
                if c.execute("select 1 from tasks where id=?", (task_id,)).fetchone():
                    return slug
            finally:
                c.close()
        except sqlite3.Error:
            continue
    return None


def subscriptions(task_id: str, h: Optional[Path] = None) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for _, p in boards(h):
        try:
            c = _ro_connect(p, timeout=2)
            try:
                out |= {(r[0], r[1]) for r in c.execute(
                    "select platform, chat_id from kanban_notify_subs where task_id=?", (task_id,))}
            finally:
                c.close()
        except sqlite3.Error:
            continue
    return out


def _hermes() -> Optional[str]:
    return shutil.which("hermes") or next(
        (p for p in (str(Path.home() / ".local/bin/hermes"),) if Path(p).exists()), None)


def _run(argv: list[str], stdin: Optional[str] = None) -> Optional[subprocess.CompletedProcess]:
    exe = _hermes()
    if not exe:
        return None
    try:
        return subprocess.run([exe, *argv], input=stdin, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def _board_args(card: dict) -> list[str]:
    b = card.get("board")
    return ["--board", b] if b and b != "default" else []


class HermesCards:
    """Real implementation of core.Cards."""

    def __init__(self, in_process: bool = True):
        self.in_process = in_process

    def create(self, session_id: str, title: str, body: str, key: str) -> Optional[dict]:
        args = {"title": title, "assignee": "default", "initial_status": "blocked", "body": body,
                "idempotency_key": key}
        tid = None
        if self.in_process:
            try:
                from tools.registry import registry  # type: ignore

                out = registry.dispatch("kanban_create", args)
                d = out if isinstance(out, dict) else json.loads(out)
                tid = d.get("task_id")
            except Exception:
                tid = None
        if not tid:
            p = _run(["kanban", "create", title, "--body-file", "-", "--assignee", "default", "--initial-status",
                      "blocked", "--idempotency-key", key, "--json"], stdin=body)
            try:
                d = json.loads(p.stdout or "{}") if p else {}
                tid = d.get("id") or d.get("task_id")
            except ValueError:
                tid = None
        if not tid:
            return None
        self._fix_subscription(tid, session_id)
        return {"id": tid, "board": board_of(tid) or "default"}

    def _fix_subscription(self, task_id: str, session_id: str) -> None:
        """Link the card to exactly this chat (the process env may name another chat)."""
        subs = subscriptions(task_id)
        if ("tui", session_id) not in subs:
            _run(["kanban", "notify-subscribe", task_id, "--platform", "tui", "--chat-id", session_id])
        for platform, chat in subs:
            if platform == "tui" and chat != session_id:
                _run(["kanban", "notify-unsubscribe", task_id, "--platform", "tui", "--chat-id", chat])

    def update(self, card: dict, title: str, body: str) -> None:
        _run(["kanban", *_board_args(card), "edit", card["id"], "--title", title, "--body", body])

    def archive(self, card: dict) -> bool:
        p = _run(["kanban", *_board_args(card), "archive", card["id"]])
        return bool(p and p.returncode == 0)
