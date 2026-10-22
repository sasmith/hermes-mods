"""follow-through: keep "when card X finishes, do Y" promises alive outside the conversation.

Pure logic, no Hermes imports, so tests drive it with throwaway SQLite files and fake messaging tools.

Three failure points it watches (all seen in one real incident, see README):
1. A parent keeps a delivery for itself when it spawns/comments on a child card ("do NOT send it yourself, the
   parent session sends it"). Remembering it in chat context is not durable: notices can be lost and compaction
   tells the model not to act on summarized requests. -> steer to a dependent follow-up card (``parents=[child]``),
   which the dispatcher runs by itself when the child completes.
2. A child the session subscribed to completed with an explicitly undelivered handoff ("minted but NOT sent")
   and nothing has delivered it yet -> a per-turn reminder quoting the recorded authorization, until a follow-up
   card exists or a ``follow-through: SENT`` / ``DELIVERED`` marker comment is recorded on the child.
   Also covers a completion notice that never reached this chat at all.
3. A send tool returned success but the readback shows nothing -> the result says delivery is NOT verified.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

# --- 1. obligation kept by the parent -------------------------------------------------------------------------
_KEEP = re.compile(
    r"(?:\bdo\s+not|\bdon'?t|\bnever)\s+(?:send|message|deliver|post|forward|share|whatsapp|text|email)\b[^.\n]{0,60}"
    r"\byourself\b"
    r"|\b(?:parent|coordinator|calling|this)\s+(?:session|chat|agent)\b[^.\n]{0,40}"
    r"\b(?:will\s+|'ll\s+)?(?:sends?|delivers?|messages?|posts?|forwards?|shares?)\b"
    r"|\bdo\s+not\s+send\s+it\b",
    re.I)
_TASK_ID = re.compile(r"\bt_[0-9a-f]{8}\b")


def kept_obligation(text: str) -> bool:
    """True when a card body/comment says the PARENT (not the worker) will deliver something later."""
    return bool(text and _KEEP.search(text))


# --- 2. undelivered handoff ---------------------------------------------------------------------------------
_UNDELIVERED = re.compile(
    r"\b(?:not|never)\s+(?:yet\s+)?(?:been\s+)?(?:sent|delivered|messaged|posted|forwarded|shared)\b"
    r"|\bminted\s+but\s+not\b|\bparent\s+session\s+(?:sends|delivers|will)\b|\bdid\s+not\s+send\b",
    re.I)
_RESOLVED_MARKER = re.compile(r"follow-through:\s*(?:SENT|DELIVERED|DONE|CANCELLED)\b", re.I)
FOLLOWUP_KEY_PREFIX = "followup:"


def undelivered_handoff(summary: str) -> bool:
    return bool(summary and _UNDELIVERED.search(summary))


def kanban_dbs(home: Path) -> list[Path]:
    out = [home / "kanban.db"] + sorted((home / "kanban" / "boards").glob("*/kanban.db"))
    return [p for p in out if p.is_file()]


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


def _ro(path: Path) -> sqlite3.Connection:
    conn = _ro_connect(path, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _table_cols(conn, table) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def session_keys(state_db: Optional[Path], session_id: str) -> set[str]:
    """The ids a chat's kanban subscriptions may carry: its own id, its stored session_key and its ancestors
    (compression can rotate the agent session id under one chat)."""
    keys = {session_id} if session_id else set()
    if not state_db or not state_db.is_file() or not session_id:
        return keys
    try:
        with _ro(state_db) as conn:
            cols = _table_cols(conn, "sessions")
            cur, seen = session_id, 0
            while cur and seen < 20:
                seen += 1
                row = conn.execute("SELECT * FROM sessions WHERE id=?", (cur,)).fetchone()
                if not row:
                    break
                if "session_key" in cols and row["session_key"]:
                    keys.add(row["session_key"])
                cur = row["parent_session_id"] if "parent_session_id" in cols else None
                if cur:
                    keys.add(cur)
    except sqlite3.Error:
        pass
    return keys


def _seen_in_chat(state_db: Optional[Path], keys: Iterable[str], needle: str) -> bool:
    if not state_db or not state_db.is_file():
        return True  # can't tell: don't claim a notice was missed
    keys = [k for k in keys if k]
    if not keys:
        return True
    try:
        with _ro(state_db) as conn:
            q = "SELECT 1 FROM messages WHERE session_id IN (%s) AND content LIKE ? LIMIT 1" % ",".join("?" * len(keys))
            return conn.execute(q, (*keys, f"%{needle}%")).fetchone() is not None
    except sqlite3.Error:
        return True


def open_followups(home: Path, state_db: Optional[Path], session_id: str, now: Optional[float] = None,
                   max_age_s: float = 7 * 86400) -> list[dict]:
    """Completed child cards this chat subscribed to whose handoff is explicitly undelivered and that nothing
    has delivered yet, plus completions whose notice never reached this chat. Newest first."""
    now = time.time() if now is None else now
    keys = session_keys(state_db, session_id)
    found: list[dict] = []
    for db in kanban_dbs(home):
        try:
            conn = _ro(db)
        except sqlite3.Error:
            continue
        with conn:
            try:
                subs = conn.execute(
                    "SELECT task_id, last_event_id FROM kanban_notify_subs WHERE lower(platform)='tui' AND chat_id IN (%s)"
                    % ",".join("?" * len(keys)), tuple(keys)).fetchall() if keys else []
            except sqlite3.Error:
                continue
            for sub in subs:
                tid = sub["task_id"]
                task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                if not task or task["status"] != "done":
                    continue
                ev = conn.execute("SELECT id, payload, created_at FROM task_events WHERE task_id=? AND kind='completed' "
                                  "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
                if not ev or now - (ev["created_at"] or 0) > max_age_s:
                    continue
                try:
                    payload = json.loads(ev["payload"] or "{}") or {}
                except ValueError:
                    payload = {}
                run = conn.execute("SELECT summary FROM task_runs WHERE task_id=? AND outcome='completed' "
                                   "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
                summary = (run["summary"] if run and run["summary"] else None) or payload.get("summary") or \
                    (task["result"] if "result" in task.keys() else "") or ""
                comments = [r["body"] for r in conn.execute(
                    "SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (tid,))]
                resolved = any(_RESOLVED_MARKER.search(c or "") for c in comments)
                followup = conn.execute(
                    "SELECT t.id, t.status FROM task_links l JOIN tasks t ON t.id=l.child_id WHERE l.parent_id=? "
                    "AND (t.idempotency_key LIKE ? OR lower(t.title) LIKE 'follow-up%')",
                    (tid, FOLLOWUP_KEY_PREFIX + "%")).fetchone() if "idempotency_key" in task.keys() else None
                undelivered = undelivered_handoff(summary) or kept_obligation(task["body"] or "") or \
                    any(kept_obligation(c or "") for c in comments)
                missed = not _seen_in_chat(state_db, keys, f"Kanban {tid} done")
                if resolved or followup or not (undelivered or missed):
                    continue
                authority = [s.strip() for s in [task["body"] or "", *comments] if kept_obligation(s or "")]
                found.append(dict(task_id=tid, title=task["title"], completed_at=ev["created_at"], event_id=ev["id"],
                                  summary=summary, undelivered=undelivered, missed_notice=missed,
                                  authority=[_excerpt(a) for a in authority[-2:]], db=str(db)))
    found.sort(key=lambda r: -(r["completed_at"] or 0))
    return found


def _excerpt(text: str, limit: int = 600) -> str:
    """The sentences around the kept-obligation match (the recorded instruction), trimmed."""
    m = _KEEP.search(text)
    if not m:
        return text[:limit]
    start = max(0, text.rfind("\n", 0, max(0, m.start() - 300)) + 1)
    return re.sub(r"\s+", " ", text[start:start + limit]).strip()


def _redact_capabilities(text: str) -> str:
    """Never echo capability URLs into the prompt-injected note; the agent reads them from the card itself."""
    return re.sub(r"https?://\S+/(?:person|invite|pair|join)/\S+", "<link in the card handoff>", text)


def render_followups(items: list[dict]) -> str:
    lines = ["[follow-through] Unfinished follow-ups from child cards this chat is responsible for:"]
    for it in items:
        why = []
        if it["missed_notice"]:
            why.append("its completion notice never reached this chat")
        if it["undelivered"]:
            why.append("its handoff is explicitly NOT delivered yet")
        lines.append(f"- {it['task_id']} ({it['title'][:90]}) completed: " + "; ".join(why) + ".")
        lines.append("  Handoff: " + _redact_capabilities(re.sub(r"\s+", " ", it["summary"] or "")[:500]))
        for a in it["authority"]:
            lines.append("  Recorded instruction: " + _redact_capabilities(a))
    if not any(it["undelivered"] for it in items):
        lines.append("Read each handoff (kanban_show) and act on anything this chat still owes for it; if nothing "
                     "is owed, just take note.")
        return "\n".join(lines)
    lines.append(
        "For the undelivered ones, do it now, without waiting to be asked: read the card (kanban_show) for the exact "
        "artifact, check it is "
        "still valid, and perform exactly the recorded action (no wider than recorded; if no instruction is "
        "recorded, ask). Before sending, check the card's comments and the channel's history so you never send "
        "twice. After the action, record kanban_comment(task_id=<card>, body='follow-through: SENT via <channel> "
        "at <time>; delivery verified|not verified') and tell the user. A send tool's success with an empty "
        "readback is 'sent, delivery not verified', never 'delivered'.")
    return "\n".join(lines)


# --- question about a missed action ---------------------------------------------------------------------------
_MISSED_Q = re.compile(
    r"\b(?:did|have|has)\s+(?:you|u|it|bb|anyone)\b[^?\n]{0,60}\b(?:send|sent|deliver(?:ed)?|messag(?:e|ed)|post(?:ed)?|"
    r"forward(?:ed)?|share(?:d)?|do|done|did|finish(?:ed)?|text(?:ed)?|email(?:ed)?|tell|told|reply|replied)\b"
    r"|\bhow\s+am\s+i\s+supposed\s+to\b|\bwhy\s+(?:didn'?t|haven'?t|hasn'?t)\s+you\b|\byou\s+(?:forgot|never)\b",
    re.I)

MISSED_ACTION_NOTE = (
    "[follow-through] The user is asking about an action that may not have happened. If it is something you or a "
    "card were already asked/authorized to do and it is still undone, do it now (check current state first, stay "
    "within what was authorized, avoid duplicates), verify it, and then answer with what you did. An admission "
    "without the action is not an answer.")


def is_missed_action_question(message: str) -> bool:
    return bool(message and _MISSED_Q.search(message[:500]))


# --- obligation steer at kanban_create / kanban_comment ----------------------------------------------------------
def obligation_hint(tool_name: str, args: dict, result_text: str) -> Optional[str]:
    """Hint appended to a kanban_create/kanban_comment result that records a delivery the parent keeps."""
    args = args or {}
    try:
        parsed = json.loads(result_text) if result_text else {}
    except (TypeError, ValueError):
        parsed = {}
    if not isinstance(parsed, dict) or parsed.get("error") or parsed.get("ok") is False:
        return None  # nothing was recorded, so nothing to schedule
    if tool_name == "kanban_create":
        text = args.get("body") or ""
        try:
            child = (json.loads(result_text) or {}).get("task_id") or ""
        except (TypeError, ValueError):
            child = ""
    elif tool_name == "kanban_comment":
        text, child = args.get("body") or "", args.get("task_id") or ""
    else:
        return None
    if not child or not kept_obligation(text):
        return None
    if str(args.get("idempotency_key") or "").startswith(FOLLOWUP_KEY_PREFIX):
        return None  # this IS the follow-up card
    return (
        f"[follow-through] You are keeping a delivery for after {child} finishes. Chat memory is not durable "
        f"(completion notices can be lost; compaction says not to act on summarized requests). Schedule it now as a "
        f"dependent card: kanban_create(title='Follow-up: <action> after {child}', assignee=<your profile>, "
        f"parents=['{child}'], idempotency_key='{FOLLOWUP_KEY_PREFIX}{child}:<action-slug>', body=<the user's exact "
        f"instruction quoted, the recipient, where the artifact comes from (the parent's handoff), and: check this "
        f"card's comments and the channel history before sending so it never sends twice; after sending, "
        f"kanban_comment(task_id='{child}', body='follow-through: SENT via <channel> at <time>; delivery verified|"
        f"not verified'); empty readback = not verified>). The dispatcher runs it by itself when {child} completes, "
        f"even if this chat never sees the notice.")


# --- 3. send success + empty readback ---------------------------------------------------------------------------
_SEND_TOOL = re.compile(r"(?:^|__)(?:send_message|send_file|send_audio_message|send_text|send_sms|send_email)$")
_READ_TOOL = re.compile(r"(?:^|__)(?:list_messages|get_chat|get_last_interaction|get_message_context|read_messages|"
                        r"list_chats|search_messages)$")
_EMPTY = re.compile(r"no messages to display|\"messages\"\s*:\s*\[\s*\]|^\s*(?:\[\s*\]|\{\s*\}|null|)\s*$", re.I)
_OK = re.compile(r"\\?\"success\\?\"\s*:\s*true|\bmessage sent\b", re.I)

READBACK_NOTE = (
    "\n[follow-through] Readback is empty, so this send is NOT verified as delivered. Say 'sent (the send tool "
    "returned success); delivery not verified', not 'delivered'. Don't resend because of this alone: an empty "
    "readback is not a failed send.")


class SendTracker:
    """Per-session: last successful send time, so a following empty readback is flagged."""

    def __init__(self, window_s: float = 1800.0, clock: Callable[[], float] = time.time):
        self.window_s, self.clock, self.sent = window_s, clock, {}

    def observe(self, session_id: str, tool_name: str, result_text: str) -> Optional[str]:
        name = tool_name or ""
        if _SEND_TOOL.search(name):
            if _OK.search(result_text or ""):
                self.sent[session_id] = self.clock()
            return None
        if _READ_TOOL.search(name):
            at = self.sent.get(session_id)
            if at and self.clock() - at <= self.window_s and _EMPTY.search(_strip_wrapper(result_text or "")):
                if READBACK_NOTE.strip() not in (result_text or ""):
                    return READBACK_NOTE
        return None


def _strip_wrapper(text: str) -> str:
    """MCP results arrive wrapped in an untrusted-content envelope and/or a {"result": "..."} JSON object."""
    inner = re.sub(r"(?s)^.*?<untrusted_tool_result[^>]*>.*?(\{.*\}|\[.*\]).*?</untrusted_tool_result>.*$", r"\1", text)
    try:
        obj = json.loads(inner)
        if isinstance(obj, dict) and "result" in obj:
            return str(obj["result"])
    except (TypeError, ValueError):
        pass
    return inner


# --- throttle -------------------------------------------------------------------------------------------------
class Throttle:
    """Remind about each (session, card) at most once per ``every_s`` and at most ``max_times`` in all (state on
    disk, so a backend restart neither forgets nor re-floods)."""

    def __init__(self, path: Path, every_s: float = 1800.0, max_times: int = 6, clock: Callable[[], float] = time.time):
        self.path, self.every_s, self.max_times, self.clock = path, every_s, max_times, clock

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def due(self, session_id: str, task_ids: list[str]) -> list[str]:
        state, now = self._load(), self.clock()
        due = []
        for t in task_ids:
            last, count = state.get(f"{session_id}|{t}", [None, 0])
            if count < self.max_times and (last is None or now - float(last) >= self.every_s):
                due.append(t)
                state[f"{session_id}|{t}"] = [now, count + 1]
        if due:
            state = {k: v for k, v in state.items() if now - float(v[0]) < 14 * 86400}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(self.path)
        return due
