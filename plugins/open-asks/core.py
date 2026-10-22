"""open-asks: a chat reply that asks the user for a decision or action becomes a blocked Kanban card.

Pure functions (detection, card text, state) live here so tests need no Hermes runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

# A sentence that asks the user to decide or do something. Kept narrow: a false card is noise on the board.
_ASK = re.compile(
    r"(^|\s)(want me to|should i|shall i|do you want( me)? to|would you like( me)? to|ok(ay)? (for me )?to|"
    r"which (one|option) do you want|can you (confirm|approve|decide|pick|choose|check|reply|tell me)|"
    r"could you (confirm|approve|decide|pick|choose|check|reply|tell me))\b[^?]*\?"
    r"|\b(needs?|waiting (on|for)|still needs?) (your|you to)\b[^.?!]{0,60}\b"
    r"(yes|no|yes/no|yes or no|go|go-ahead|ok|okay|approval|decision|call|answer|password|sign-?in|input|pick|choice|"
    r"confirm|approve|decide|click|tap|reply)\b"
    r"|\b(reply|answer|say|tell me) (with )?[\"'“]?(yes|no|go)\b"
    r"|\byour (yes/no|yes or no|go-ahead|go/no-go)\b"
    r"|\b(let me know|tell me) (if|whether|which)\b"
    r"|\bplease (confirm|approve|decide|pick|choose|reply)\b",
    re.I)
# Mentions that are not a live ask: hypotheticals, explicit "nothing for you", past/third person.
_NOT_ASK = re.compile(
    r"\b(if you (ever|later|still|prefer)|unless|in case|whenever you|no action (is )?needed|nothing (is )?"
    r"(waiting|needed|needs?) (on|from|for) you|nothing for you|you (already|have already)|you (said|answered|"
    r"approved|asked)|don'?t need (anything|you)|no (more|further) (input|action)|the card asks|it asked)\b",
    re.I)
_CARD_ID = re.compile(r"\bt_[0-9a-f]{8}\b")


def _strip_noise(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)  # code blocks
    text = re.sub(r"(?m)^\s*>.*$", " ", text)            # quotes
    text = re.sub(r"`[^`\n]*`", "X", text)                # inline code
    return text


def sentences(text: str) -> list[str]:
    return [s.strip(" -*•#|") for s in re.split(r"(?<=[.!?])\s+|\n+", _strip_noise(text)) if s.strip(" -*•#|")]


def find_asks(text: str) -> list[str]:
    """Sentences of a reply that ask the user for a decision/action (in order, deduplicated)."""
    out: list[str] = []
    for s in sentences(text or ""):
        if _ASK.search(s) and not _NOT_ASK.search(s) and s not in out:
            out.append(s)
    return out


def card_ids(text: str) -> list[str]:
    return sorted(set(_CARD_ID.findall(text or "")))


def ask_key(session_id: str, asks: list[str]) -> str:
    norm = re.sub(r"\W+", " ", " ".join(asks).lower()).strip()
    return "open-ask:%s:%s" % (session_id, hashlib.sha1(norm.encode()).hexdigest()[:12])


def card_title(asks: list[str]) -> str:
    first = re.sub(r"\s+", " ", asks[0]).strip()
    return "ASK: " + (first if len(first) <= 110 else first[:107].rstrip() + "…")


def card_body(session_ref: str, asks: list[str], reply: str, when: str) -> str:
    quoted = "\n".join("> " + a for a in asks)
    excerpt = reply.strip()
    if len(excerpt) > 3000:
        excerpt = excerpt[:3000].rstrip() + " …"
    return (
        f"Open question for David from chat {session_ref}, asked {when}. Filed automatically by the `open-asks` "
        "plugin: the reply asked him for a decision or action without a tracked card, and a question that only "
        "lives in chat text never shows as waiting on him (sidebar, blocked digest).\n\n"
        f"## The ask\n{quoted}\n\n"
        "## How this card closes\n"
        "- **David answers in the chat:** the chat agent acts on the answer, then archives this card.\n"
        "- **David unblocks it without answering:** treat it as \"yes, go with the recommended option\". The worker "
        f"reads the chat (`cross_session` action=read target={session_ref}), does exactly the action the ask "
        "proposed, verifies it, and completes with the evidence. If the ask offered no single recommended action, "
        "comment that and complete without acting.\n"
        "- **David archives it:** the answer is no; nothing runs.\n\n"
        f"## Reply excerpt\n{excerpt}\n")


# ---- state (per session: open ask cards; per turn: whether the turn already filed/blocked a card) ----

class State:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.path)

    def mark_tracked(self, session_id: str, now: float | None = None) -> None:
        with self._lock:
            d = self._load()
            d.setdefault("tracked", {})[session_id] = now or time.time()
            self._save(d)

    def tracked_since(self, session_id: str, since: float) -> bool:
        return self._load().get("tracked", {}).get(session_id, 0) >= since

    def turn_started(self, session_id: str, now: float | None = None) -> None:
        with self._lock:
            d = self._load()
            d.setdefault("turn", {})[session_id] = now or time.time()
            # prune sessions idle for a week
            cutoff = time.time() - 7 * 86400
            for k in ("turn", "tracked"):
                d[k] = {s: t for s, t in d.get(k, {}).items() if t >= cutoff}
            self._save(d)

    def turn_start(self, session_id: str) -> float:
        return self._load().get("turn", {}).get(session_id, 0)

    def add_card(self, session_id: str, task_id: str, asks: list[str]) -> None:
        with self._lock:
            d = self._load()
            d.setdefault("cards", {}).setdefault(session_id, []).append(
                {"id": task_id, "asks": asks, "t": time.time()})
            self._save(d)

    def cards(self, session_id: str) -> list[dict]:
        return list(self._load().get("cards", {}).get(session_id, []))

    def drop_cards(self, session_id: str, ids: set[str]) -> None:
        with self._lock:
            d = self._load()
            rows = [c for c in d.get("cards", {}).get(session_id, []) if c["id"] not in ids]
            d.setdefault("cards", {})[session_id] = rows
            if not rows:
                d["cards"].pop(session_id, None)
            self._save(d)

    def recent_count(self, session_id: str, window_s: float = 3600) -> int:
        now = time.time()
        return sum(1 for c in self.cards(session_id) if now - c.get("t", 0) < window_s)


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


def blocked_ids(db_paths: list[Path], ids: list[str]) -> set[str]:
    """Which of ``ids`` are currently ``blocked`` on any board (an already-tracked ask)."""
    found: set[str] = set()
    if not ids:
        return found
    ph = ",".join("?" * len(ids))
    for p in db_paths:
        try:
            c = _ro_connect(p, timeout=2)
            found |= {r[0] for r in c.execute(f"select id from tasks where status='blocked' and id in ({ph})", ids)}
            c.close()
        except sqlite3.Error:
            continue
    return found


def subscriptions(db_paths: list[Path], task_id: str) -> set[tuple[str, str]]:
    """``(platform, chat_id)`` pairs subscribed to ``task_id`` on any board."""
    out: set[tuple[str, str]] = set()
    for p in db_paths:
        try:
            c = _ro_connect(p, timeout=2)
            out |= {(r[0], r[1]) for r in c.execute(
                "select platform, chat_id from kanban_notify_subs where task_id=?", (task_id,))}
            c.close()
        except sqlite3.Error:
            continue
    return out


def statuses(db_paths: list[Path], ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not ids:
        return out
    ph = ",".join("?" * len(ids))
    for p in db_paths:
        try:
            c = _ro_connect(p, timeout=2)
            out.update({r[0]: r[1] for r in c.execute(f"select id, status from tasks where id in ({ph})", ids)})
            c.close()
        except sqlite3.Error:
            continue
    return out


def reminder(open_cards: list[dict]) -> str:
    lines = ["[open-asks] Your previous reply left these questions for the user tracked as blocked Kanban cards:"]
    for c in open_cards:
        lines.append(f"- {c['id']}: " + " / ".join(c.get("asks", []))[:300])
    lines.append("If the user's message answers one, act on the answer, then archive that card "
                 "(terminal: `hermes kanban archive <id>`) and say so in one line. If it doesn't, leave the card open.")
    return "\n".join(lines)
