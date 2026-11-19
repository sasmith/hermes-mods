#!/usr/bin/env python3
"""Deliver an unaddressable PR review/CI notification into the Desktop chat that owns the PR.

Usage:
  ~/.hermes/scripts/notify-pr-chat.py <owner/repo>#<n> <branch> <message>
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

HERMES_HOME = Path(os.path.expanduser("~/.hermes"))
STATE_DB = HERMES_HOME / "state.db"
HERMES_BIN = next(
    (
        p
        for p in (
            str(HERMES_HOME / "hermes-agent/.hermes/bin/hermes"),
            os.path.expanduser("~/.local/bin/hermes"),
        )
        if os.path.exists(p)
    ),
    "hermes",
)


def find_owning_chat(pr_key: str, branch: str) -> str | None:
    """Return the session_id of the Desktop chat that worked on this PR, or the most recent active Desktop chat.

    Matching is strict on purpose: only the full `owner/repo#n`, the PR URL, or the branch name count
    (bare `#n` matched unrelated numbers such as `#119570`). Messages that are themselves PR-attention or
    kanban notifications are ignored, so a chat that merely *received* a misrouted ping doesn't become the
    owner. The chat with the most matching messages wins (ties: the one that mentioned it first)."""
    if not STATE_DB.exists():
        return None
    repo, _, num = pr_key.partition("#")
    regexes = [re.compile(re.escape(pr_key) + r"(?!\d)")]
    if repo and num:
        regexes.append(re.compile(re.escape(f"{repo}/pull/{num}") + r"(?!\d)"))
    if branch:
        regexes.append(re.compile(r"(?<![\w/-])" + re.escape(branch) + r"(?![\w-])"))
    like = [f"%{pr_key}%"] + ([f"%{repo}/pull/{num}%"] if repo and num else []) + ([f"%{branch}%"] if branch else [])

    conn = sqlite3.connect(STATE_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        where_like = " OR ".join("m.content LIKE ?" for _ in like)
        rows = conn.execute(
            f"""
            SELECT s.id, m.content, m.timestamp
            FROM sessions s
            JOIN messages m ON m.session_id = s.id
            WHERE s.source IN ('local', 'desktop')
              AND COALESCE(s.title, '') != 'Bot Chat'
              AND ({where_like})
            """,
            like,
        ).fetchall()
        score: dict[str, list] = {}
        for sid, content, ts in rows:
            text = content or ""
            if "[kanban] Task" in text or "PR attention needed on" in text:
                continue
            if not any(r.search(text) for r in regexes):
                continue
            s = score.setdefault(str(sid), [0, ts or 0])
            s[0] += 1
            s[1] = min(s[1], ts or 0)
        if score:
            return max(score.items(), key=lambda kv: (kv[1][0], -kv[1][1]))[0]

        fallback = conn.execute(
            """
            SELECT s.id
            FROM sessions s
            WHERE s.source IN ('local', 'desktop')
              AND COALESCE(s.title, '') != 'Bot Chat'
            ORDER BY s.last_activity_at DESC
            LIMIT 1
            """
        ).fetchone()
        return str(fallback[0]) if fallback and fallback[0] else None
    finally:
        conn.close()


def run_hermes(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([HERMES_BIN, *args], capture_output=True, text=True, timeout=30)


def main() -> int:
    if len(sys.argv) < 4:
        print("Usage: notify-pr-chat.py <owner/repo>#<n> <branch> <message>", file=sys.stderr)
        return 2
    pr_key = sys.argv[1].strip()
    branch = sys.argv[2].strip()
    message = " ".join(sys.argv[3:]).strip()

    chat_id = find_owning_chat(pr_key, branch)
    idem_key = f"pr-followup-attention:{pr_key}"
    title = f"PR attention needed on {pr_key} ({branch})"

    created = run_hermes(
        "kanban",
        "create",
        title[:120],
        "--idempotency-key",
        idem_key,
        "--body",
        message,
        "--json",
    )
    tid = None
    try:
        payload = json.loads(created.stdout)
        tid = payload.get("id") or payload.get("task_id") or (payload.get("task") or {}).get("id")
    except Exception:
        tid = None

    if not tid:
        print(f"notify-pr-chat: failed to create/find card: {created.stderr or created.stdout}", file=sys.stderr)
        return 1

    # Sticky owner: a reused card that already notifies a Desktop chat keeps that chat only.
    existing = run_hermes("kanban", "notify-list", tid, "--json")
    try:
        subs = json.loads(existing.stdout) or []
        owned = [s.get("chat_id") for s in subs if s.get("platform") == "local" and s.get("chat_id")]
    except Exception:
        owned = []
    if owned:
        chat_id = owned[0]
    elif chat_id:
        run_hermes(
            "kanban",
            "notify-subscribe",
            tid,
            "--platform",
            "local",
            "--chat-id",
            chat_id,
            "--delivery-mode",
            "notify+wake",
        )
        run_hermes(
            "kanban",
            "notify-subscribe",
            tid,
            "--platform",
            "tui",
            "--chat-id",
            f"agent:main:local:dm:{chat_id}",
        )

    # Ensure a fresh `blocked` event is emitted after subscription so the notifier fires even if reused.
    run_hermes("kanban", "unblock", tid)
    blocked = run_hermes("kanban", "block", tid, "--kind", "needs_input", "--", message)
    if blocked.returncode != 0:
        print(f"notify-pr-chat: block failed: {blocked.stderr or blocked.stdout}", file=sys.stderr)
        return 1

    print(f"Notified chat {chat_id or 'none'} via card {tid} for {pr_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
