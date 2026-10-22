"""ask-gate core: pure helpers (ask detection, live card state, answered-asks ledger, tool log, capability sheet).

No Hermes runtime needed, so every function here is unit-testable with fakes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    _PT = None

# --------------------------------------------------------------------------------------------------
# ask detection (widened from open-asks): a sentence that asks David to do / decide / provide something,
# or says something is waiting on him.
# --------------------------------------------------------------------------------------------------
_ASK = re.compile(
    r"(^|\s)(want me to|should i|shall i|do you want( me)? to|would you like( me)? to|ok(ay)? (for me )?to|"
    r"which (one|option) do you want|can you|could you|would you|will you|are you able to)\b[^?]*\?"
    r"|\b(needs?|waiting (on|for)|still (needs?|waiting (on|for))|blocked on) (your|you\b|david)\b"
    r"|\bneeds? (you|david) to\b"
    r"|\bif you'?d like me to\b|\bonly open (item|question)\b"
    r"|\b(it'?s|that'?s|this is|is|are) (up to|your call|your decision|on you)\b"
    r"|\byour (call|decision|yes/no|yes or no|go-ahead|go/no-go|approval|sign-?in|password)\b"
    r"|\b(david'?s|his) call\b"
    r"|\bneeds? david\b"
    r"|\b(you|you'll|you will) (need|have) to (sign|log|click|approve|revoke|cancel|add|enable|grant|create|run|"
    r"open|paste|copy|install|decide|pick|choose|confirm|tap)\b"
    r"|\byou can (revoke|cancel|add|enable|grant|do it yourself|sign in)\b"
    r"|\b(reply|answer|say|tell me) (with )?[\"'“]?(yes|no|go)\b"
    r"|\b(let me know|tell me) (if|whether|which|when)\b"
    r"|\bplease (confirm|approve|decide|pick|choose|reply|sign|add|click|revoke|cancel)\b"
    r"|\b(unblock|unblocks) (it|the card|t_[0-9a-f]{8})\b",
    re.I)
# Mentions that are not a live ask: hypotheticals, explicit "nothing for you", past/third person.
_NOT_ASK = re.compile(
    r"\b(if you (ever|later|still|prefer)|unless|in case|whenever you|no action (is )?needed|"
    r"nothing (is )?(waiting|needed|needs?|left) (on|from|for) you|nothing (for|to do for) you|"
    r"nothing in this (work|card) is waiting|doesn'?t need you|don'?t need (anything|you)|"
    r"you (already|have already)|you (said|answered|approved|asked|told)|no (more|further) (input|action)|"
    r"(isn'?t|is not|aren'?t|are not) waiting on you|not waiting on you|the card asks|it asked|"
    r"i'?ll stop asking|nothing from this work is waiting|nothing\b[^.]{0,60}\bneeds? (you|david)|"
    r"no\b[^.]{0,40}\bwaiting on you|(was|were|had been|had) (waiting|blocked) (on|for) (you|david)|never (create|say|tell|ask|put))\b",
    re.I)
# --- shapes added after the week-of-10-12 corrections (imperatives, user-named headings, "I can't" claims) ---
_VERB = (r"(?:open|check|tap|click|press|sign in|log in|go to|change|enable|disable|raise|switch (?:off|on)|"
         r"turn (?:off|on)|install|run|delete)")
# "Open items", "Run log" and the like are nouns, not instructions
_VERB_NOT_NOUN = r"(?!\s+(?:\d|items?|questions?|issues?|prs?|cards?|loops?|logs?|times?|counts?|results?|ids?)\b)"
_IMPERATIVE = re.compile(
    r"^(?:[^.!?:]{1,70}:\s+)?"                                                    # "To confirm: " / "Echo Dot pod: "
    r"(?:(?:in|on|if|once|when|after|then|otherwise|from|at|before|for)\b[^,.!?]{0,80},\s+)?"   # "On the iPhone 8, "
    r"(?:(?:and|then|also|just)\s+)*(?:please\s+)?" + _VERB + r"\b" + _VERB_NOT_NOUN, re.I)
_PLEASE_VERB = re.compile(r"\bplease\s+" + _VERB + r"\b" + _VERB_NOT_NOUN, re.I)
# headings that hand the reader steps: "Your checklist", "What I need from you:", "To confirm:", "@user", "For you"
_USER_HEAD = re.compile(
    r"^\W*(?:your (?:checklist|steps|turn|action items?|to-?dos?)|what i need from you|to confirm|to clear it|"
    r"@user|for you|needs you|on you)\b[^:\n]{0,40}(?::|$)", re.I)
# "I can't" claims: the gap stays with the reader unless this turn's tool output shows the attempt failing
_CANT = re.compile(
    r"\bI (?:can['\u2019]?t|cannot) (?:change|check|do|open|fix|reach)\b"
    r"|^\W*Limitation:|\bwould need\b[^.]*\b(?:doesn['\u2019]?t|don['\u2019]?t) have\b", re.I)
_HOWTO_ASKED = re.compile(
    r"\bhow (?:do|can|would|should|could|to|am i supposed)\b|\bwalk me through\b|\bsteps?\b|\binstructions?\b|"
    r"\bshow me how\b|\bteach me\b|\bwhat should i do\b", re.I)
_CARD_ID = re.compile(r"\bt_[0-9a-f]{8}\b")
# worker handoff leftovers that point at David
_LEFTOVER = re.compile(
    r"\bdavid'?s (call|decision|choice)\b|\bup to david\b|\bdavid (should|must|needs to|will need to|can)\b"
    r"|\bneeds? david\b|\bfor david to\b|\bleft (for|to) david\b|\bask david\b|\bneeds? (your|the user'?s) "
    r"(call|decision|approval)\b|\bthe user (should|must|needs to)\b", re.I)
_DAVID_ACTION = re.compile(
    r"\bneeds? (you|david)\b|\bdavid'?s call\b|\bdavid (must|should|needs? to)\b|\bwaiting on (you|david)\b"
    r"|\bneeds? your\b", re.I)


def _strip_noise(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"(?m)^\s*>.*$", " ", text)
    text = re.sub(r"`[^`\n]*`", "X", text)
    return text


_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_BOLD_SUB = re.compile(r"^\s*(?:\*\*[^*\n]+\*\*|#+\s+.+)\s*:?\s*$")


def _clean(x: str) -> str:
    return x.strip(" -*•#|_").replace("**", "").replace("__", "")


def _units(text: str) -> list[tuple[str, bool]]:
    """(unit, forced) pairs. A heading line ending in ':' that is ask-shaped absorbs the list items under it into one
    unit ("Needs you:" carries what it needs). A heading that names the reader ("Your checklist", "@user") makes
    each item under it its own *forced* unit: it is a step for the reader whatever its wording."""
    lines = _strip_noise(text or "").split("\n")
    units: list[tuple[str, bool]] = []
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        bare = ln.rstrip("*_ ").lstrip("*_# ")
        if bare.endswith(":") and _ASK.search(ln) and i + 1 < len(lines) and _LIST_ITEM.match(lines[i + 1]):
            items = []
            j = i + 1
            while j < len(lines) and _LIST_ITEM.match(lines[j]) and len(items) < 5:
                items.append(_LIST_ITEM.sub("", lines[j]).strip())
                j += 1
            units.append((_clean(ln + " " + " | ".join(items)), False))
            i = j
            continue
        if _USER_HEAD.match(bare):
            tail = re.sub(r"^[^:]*:\s*", "", bare) if ":" in bare else ""
            if len(tail.strip()) >= 15:          # "To confirm: in the Nest app, open Upstairs -> History."
                first, *rest = [x for x in (_clean(y) for y in re.split(r"(?<=[.!?])\s+", ln)) if x]
                units.append((first, True))
                units.extend((x, False) for x in rest)
                i += 1
                continue
            j, n, intro = i + 1, 0, False
            while j < len(lines) and n < 12:
                nxt = lines[j].strip()
                if not nxt:
                    j += 1
                elif _LIST_ITEM.match(lines[j]):
                    units.append((_clean(_LIST_ITEM.sub("", lines[j])), True))
                    n += 1
                    j += 1
                elif _BOLD_SUB.match(nxt):       # a sub-heading inside the checklist
                    j += 1
                elif n == 0 and not intro and len(nxt) < 200:   # one intro line before the first step
                    intro = True
                    j += 1
                else:
                    break
            if n:
                i = j
                continue
        units.extend((x2, False) for x2 in (_clean(x) for x in re.split(r"(?<=[.!?])\s+", ln)) if x2)
        i += 1
    return units


def sentences(text: str) -> list[str]:
    return [u for u, _ in _units(text)]


_COND_LEAD = re.compile(r"^(?:[^.!?:]{1,70}:\s+)?(?:if|in that case|otherwise|then|once|when|after)\b", re.I)
_CHAIN = re.compile(r",\s*(?:and\s+|then\s+)?" + _VERB + r"\b", re.I)


def is_imperative(s: str) -> bool:
    """A sentence that tells the reader to do something. ``please <verb>`` always counts; a bare leading verb
    counts only when it is a fallback/conditional step ("In that case, check ...") or a
    chain of steps ("Click A, open B, then click C"), so plain narration and one-word labels stay out."""
    if _PLEASE_VERB.search(s):
        return True
    if not _IMPERATIVE.match(s):
        return False
    return bool(_COND_LEAD.match(s) or _CHAIN.search(s))


def is_soft_ask(s: str) -> bool:
    """Asks judged with the stricter instruction/claim prompt: any imperative to the reader or "I can't" claim."""
    return bool(_IMPERATIVE.match(s) or _PLEASE_VERB.search(s) or _CANT.search(s))


def asked_how_to(user_msg: str) -> bool:
    return bool(_HOWTO_ASKED.search(user_msg or ""))


def find_asks(text: str, howto: bool = False) -> list[str]:
    """Sentences that hand David something to do, decide or provide. ``howto``: he asked how to do something,
    so imperative steps are the answer, not an ask."""
    out: list[str] = []
    n_imp = 0
    for s, forced in _units(_strip_quotes(text)):
        if _NOT_ASK.search(s) or s in out:
            continue
        hit = bool(_ASK.search(s)) or bool(_CANT.search(s))
        if not hit and forced:
            hit = not howto
        if not hit and not howto and n_imp < 8 and is_imperative(s):
            hit, n_imp = True, n_imp + 1
        if hit:
            out.append(s[:1200] if forced else s[:600])
    return out


def _strip_quotes(text: str) -> str:
    """Quoted phrases are mentions, not asks (\"needs David\" inside an explanation)."""
    return re.sub(r"[\"“][^\"”\n]{1,80}[\"”]", "X", text or "")


def find_leftovers(text: str) -> list[str]:
    """Sentences in a worker handoff that hand a decision/action to David."""
    out: list[str] = []
    for s in sentences(text):
        if (_LEFTOVER.search(s) or _DAVID_ACTION.search(s)) and not _NOT_ASK.search(s) and s not in out:
            out.append(s)
    return out


def card_ids(text: str) -> list[str]:
    return sorted(set(_CARD_ID.findall(text or "")))


def says_david_must_act(text: str) -> bool:
    return bool(_DAVID_ACTION.search(text or "") or _LEFTOVER.search(text or ""))


# --------------------------------------------------------------------------------------------------
# paths / time
# --------------------------------------------------------------------------------------------------
def home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def boards() -> list[Path]:
    root = home()
    paths = [root / "kanban.db"] + sorted((root / "kanban" / "boards").glob("*/kanban.db"))
    return [p for p in paths if p.is_file()]


def fmt_ts(ts: float | int | None) -> str:
    if not ts:
        return "?"
    d = datetime.fromtimestamp(float(ts), _PT) if _PT else datetime.fromtimestamp(float(ts))
    return d.strftime("%m-%d %H:%M %Z").strip()


def log_event(kind: str, **kw: Any) -> None:
    """Append-only gate log (~/.hermes/logs/ask-gate.jsonl): every verdict, hold and gate failure."""
    try:
        p = home() / "logs" / "ask-gate.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, **kw}, default=str) + "\n")
    except OSError:
        pass


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


# --------------------------------------------------------------------------------------------------
# L1: live card state
# --------------------------------------------------------------------------------------------------
def _ro(p: Path) -> sqlite3.Connection:
    return _ro_connect(p, timeout=2)


def linked_cards(session_id: str, limit: int = 15, db_paths: list[Path] | None = None) -> list[dict]:
    """Cards created by this chat or notifying it (kanban_notify_subs), newest first."""
    out: list[dict] = []
    if not session_id:
        return out
    for p in db_paths if db_paths is not None else boards():
        try:
            c = _ro(p)
            rows = c.execute("select id, title, status, assignee, created_at, completed_at from tasks "
                             "where session_id=? order by created_at desc limit ?", (session_id, limit)).fetchall()
            try:  # cards created elsewhere but notifying this chat count too (matches the sidebar count)
                rows += c.execute("select t.id, t.title, t.status, t.assignee, t.created_at, t.completed_at "
                                  "from tasks t where t.id in (select task_id from kanban_notify_subs "
                                  "where chat_id=?) order by t.created_at desc limit ?", (session_id, limit)).fetchall()
            except sqlite3.Error:
                pass
            for r in rows:
                out.append({"id": r[0], "title": r[1], "status": r[2], "assignee": r[3],
                            "created": r[4], "completed": r[5]})
            c.close()
        except sqlite3.Error:
            continue
    seen: set[str] = set()
    out = [x for x in sorted(out, key=lambda x: x.get("created") or 0, reverse=True)
           if not (x["id"] in seen or seen.add(x["id"]))]
    return out[:limit]


def card_status(ids: list[str], db_paths: list[Path] | None = None) -> dict[str, dict]:
    """Live status of arbitrary card ids (mentioned in a reply), any board."""
    out: dict[str, dict] = {}
    if not ids:
        return out
    ph = ",".join("?" * len(ids))
    for p in db_paths if db_paths is not None else boards():
        try:
            c = _ro(p)
            for r in c.execute(f"select id, title, status, completed_at, result from tasks where id in ({ph})", ids):
                out[r[0]] = {"id": r[0], "title": r[1], "status": r[2], "completed": r[3],
                             "result": (r[4] or "")[:300]}
            c.close()
        except sqlite3.Error:
            continue
    return out


def render_cards(cards: list[dict]) -> str:
    lines = []
    for c in cards:
        when = f", completed {fmt_ts(c['completed'])}" if c.get("completed") else ""
        lines.append(f"- {c['id']} [{c['status']}{when}] {str(c.get('title') or '')[:90]}")
    return "\n".join(lines)


def l1_block(session_id: str, ledger: "Ledger | None", db_paths: list[Path] | None = None) -> str | None:
    cards = linked_cards(session_id, db_paths=db_paths)
    answered = ledger.recent(14 * 86400, limit=8) if ledger else []
    if not cards and not answered:
        return None
    parts = ["[ask-gate] Live state, read from the database just now. It outranks anything you remember about "
             "these cards; completion notices reach the chat late."]
    if cards:
        parts.append("Cards linked to this chat:\n" + render_cards(cards))
    if answered:
        parts.append("Asks David has already answered or dismissed (do not ask these again):\n" + "\n".join(
            f"- {a['topic'][:120]} -> {a['answer'][:120]} ({fmt_ts(a['ts'])})" for a in answered))
    parts.append("Before saying anything waits on David, check it is true now (kanban_show / the tool that proves "
                 "the premise). If you can do it yourself (token-broker scopes, ssh, signed-in browser), do it.")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------------------------------
# L4: answered-asks ledger (sqlite under ~/.hermes/state, not synced)
# --------------------------------------------------------------------------------------------------
class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else Path(os.environ.get("ASK_GATE_LEDGER") or home() / "state" / "ask-ledger.sqlite")
        self._lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=5)
        c.execute("create table if not exists answered (id integer primary key, ts real, session text, "
                  "topic text, answer text, card_id text)")
        c.execute("create table if not exists open_asks (session text, ts real, text text)")
        return c

    def _init(self) -> None:
        try:
            self._conn().close()
        except sqlite3.Error:
            pass

    def add(self, topic: str, answer: str, session: str = "", card_id: str = "", ts: float | None = None) -> None:
        with self._lock:
            try:
                c = self._conn()
                c.execute("insert into answered(ts, session, topic, answer, card_id) values (?,?,?,?,?)",
                          (ts or time.time(), session, topic.strip()[:400], answer.strip()[:400], card_id))
                c.commit()
                c.close()
            except sqlite3.Error:
                pass

    def recent(self, window_s: float, limit: int = 20) -> list[dict]:
        try:
            c = self._conn()
            rows = c.execute("select ts, session, topic, answer, card_id from answered where ts>=? "
                             "order by ts desc limit ?", (time.time() - window_s, limit)).fetchall()
            c.close()
        except sqlite3.Error:
            return []
        return [{"ts": r[0], "session": r[1], "topic": r[2], "answer": r[3], "card_id": r[4]} for r in rows]

    def set_open_asks(self, session: str, asks: list[str]) -> None:
        with self._lock:
            try:
                c = self._conn()
                c.execute("delete from open_asks where session=?", (session,))
                if asks:
                    c.execute("insert into open_asks values (?,?,?)", (session, time.time(), json.dumps(asks)))
                c.commit()
                c.close()
            except sqlite3.Error:
                pass

    def open_asks(self, session: str, max_age_s: float = 6 * 3600) -> list[str]:
        try:
            c = self._conn()
            row = c.execute("select ts, text from open_asks where session=?", (session,)).fetchone()
            c.close()
        except sqlite3.Error:
            return []
        if not row or time.time() - row[0] > max_age_s:
            return []
        try:
            return list(json.loads(row[1]))
        except ValueError:
            return []


# Cheap pre-check before spending an aux call on classifying the user's message.
_ANSWERISH = re.compile(
    r"\b(done|did it|already|handled|completed?|revoked|cancel+ed|i keep|stop asking|don'?t ask|why (are|do) you "
    r"(keep )?ask|do it yourself|you can do it|you do it|go ahead|yes|yep|yeah|no|nope|skip|not now|never ?mind|"
    r"fine|ok(ay)?|sure|do it|ship it|sounds good|approved?|keep asking|just do)\b", re.I)
_STRONG_DISMISS = re.compile(
    r"\b(stop asking|don'?t ask|why (are|do) you (keep )?ask\w*|already (did|done|handled|revoked|cancel+ed)|"
    r"i keep (completing|telling|saying)|do it yourself|you can do it|have been revoked|was handled)\b", re.I)


def looks_like_answer(msg: str) -> bool:
    return bool(_ANSWERISH.search(msg or ""))


def is_strong_dismissal(msg: str) -> bool:
    return bool(_STRONG_DISMISS.search(msg or ""))


# --------------------------------------------------------------------------------------------------
# tool log (this turn)
# --------------------------------------------------------------------------------------------------
_NON_EVIDENCE = {"kanban_create", "kanban_block", "kanban_complete", "kanban_comment", "kanban_heartbeat",
                 "kanban_link", "kanban_request_review", "kanban_request_changes", "clarify", "todo_list",
                 "send_message"}


class TurnLog:
    """Per-session record of this turn's tool calls, fed by post_tool_call and reset at pre_llm_call."""

    def __init__(self) -> None:
        self._d: dict[str, list[dict]] = {}
        self._users: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def reset(self, session: str) -> None:
        with self._lock:
            self._d[session] = []

    def add(self, session: str, tool: str, args: Any, result: Any) -> None:
        with self._lock:
            lst = self._d.setdefault(session, [])
            lst.append({"tool": tool, "args": _short(args, 400), "result": _short(result, 400), "t": time.time()})
            del lst[:-60]

    def get(self, session: str) -> list[dict]:
        with self._lock:
            return list(self._d.get(session, []))

    def set_users(self, session: str, msgs: list[str]) -> None:
        with self._lock:
            self._users[session] = msgs[-30:]

    def users(self, session: str) -> list[str]:
        with self._lock:
            return list(self._users.get(session, []))


def _short(v: Any, n: int) -> str:
    if isinstance(v, str):
        s = v
    else:
        try:
            s = json.dumps(v, default=str)
        except (TypeError, ValueError):
            s = str(v)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def tools_from_messages(turn_messages: list[Any]) -> list[dict]:
    """Tool calls + results out of an OpenAI-format message list (dicts or attr objects)."""
    def g(o: Any, k: str, d: Any = None) -> Any:
        return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)
    calls: dict[str, dict] = {}
    order: list[dict] = []
    for m in turn_messages or []:
        role = g(m, "role")
        if role == "assistant":
            for tc in g(m, "tool_calls") or []:
                fn = g(tc, "function") or {}
                ent = {"tool": g(fn, "name") or g(tc, "name") or "?",
                       "args": _short(g(fn, "arguments") or g(tc, "arguments") or "", 400), "result": ""}
                calls[str(g(tc, "id"))] = ent
                order.append(ent)
        elif role == "tool":
            ent = calls.get(str(g(m, "tool_call_id")))
            if ent is not None:
                ent["result"] = _short(g(m, "content") or "", 400)
    return order


def merge_tool_logs(*logs: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for lg in logs:
        for e in lg:
            k = (e["tool"], e["args"][:120])
            if k in seen:
                continue
            seen.add(k)
            out.append(e)
    return out


def render_tools(tools: list[dict], limit: int = 40) -> str:
    if not tools:
        return "(no tool calls this turn)"
    return "\n".join(f"- {t['tool']}({t['args'][:220]}) -> {t['result'][:220]}" for t in tools[-limit:])


# --------------------------------------------------------------------------------------------------
# L3: `Checked:` line must name a tool call made this turn
# --------------------------------------------------------------------------------------------------
_CHECKED = re.compile(r"(?im)^\W*checked\W*\s*:\s*(.+)$")
_STOP = {"this", "that", "with", "from", "have", "output", "result", "status", "command", "tool", "called",
         "ran", "run", "shows", "showed", "show", "returned", "returns", "turn", "line"}


def checked_line(text: str) -> str | None:
    m = _CHECKED.search(text or "")
    return m.group(1).strip() if m and len(m.group(1).strip()) >= 8 else None


def checked_matches(claim: str, tools: list[dict]) -> bool:
    """True if some evidence-producing tool call of this turn is what the claim names.

    A claim matches a call if the call's tool name appears in it, or at least two distinctive claim tokens
    appear in the call's arguments or result. ``kanban_create``/``kanban_block`` and other writes never count.
    """
    low = claim.lower()
    toks = {t for t in re.findall(r"[a-z0-9_@./:-]{4,}", low) if t not in _STOP}
    for t in tools:
        if t["tool"] in _NON_EVIDENCE:
            continue
        if t["tool"].lower() in low:
            return True
        hay = (t["args"] + " " + t["result"]).lower()
        if sum(1 for tok in toks if tok in hay) >= 2:
            return True
    return False


def needs_checked(tool_name: str, args: dict, is_worker: bool) -> tuple[bool, str]:
    """(does this tool call wait on David / need the Checked gate, the text to scan)."""
    if not isinstance(args, dict):
        return False, ""
    if tool_name == "kanban_block":
        return True, str(args.get("reason") or "")
    if tool_name == "kanban_create":
        body = f"{args.get('title') or ''}\n{args.get('body') or ''}"
        if str(args.get("initial_status")) == "blocked" or says_david_must_act(body):
            return True, body
    return False, ""


# --------------------------------------------------------------------------------------------------
# capability sheet (generated at runtime, never committed)
# --------------------------------------------------------------------------------------------------
_CAP_CACHE: dict[str, Any] = {"t": 0.0, "text": ""}


def _run(argv: list[str], timeout: float = 8) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def broker_scopes() -> list[str]:
    exe = shutil.which("token-broker") or str(Path.home() / ".local/bin/token-broker")
    if not Path(exe).exists() and not shutil.which("token-broker"):
        return []
    raw = _run([exe, "scopes"])
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    names = []
    for s in data if isinstance(data, list) else []:
        n = s.get("name") if isinstance(s, dict) else None
        if n:
            names.append(f"{n} ({s.get('tier', '?')})")
    return names


def ssh_hosts(cfg: Path | None = None) -> list[str]:
    p = cfg or Path.home() / ".ssh" / "config"
    try:
        text = p.read_text()
    except OSError:
        return []
    hosts: list[str] = []
    for m in re.finditer(r"(?im)^\s*host\s+(.+)$", text):
        for h in m.group(1).split():
            if "*" not in h and "?" not in h and h not in hosts:
                hosts.append(h)
    return hosts


def chrome_accounts() -> list[str]:
    """Signed-in Chrome profile emails (names only) from Chrome's Local State, if cheaply readable."""
    p = Path.home() / "Library/Application Support/Google/Chrome/Local State"
    try:
        info = json.loads(p.read_text()).get("profile", {}).get("info_cache", {})
    except (OSError, ValueError):
        return []
    return sorted({v.get("user_name") for v in info.values() if isinstance(v, dict) and v.get("user_name")})


def vault_entries() -> list[str]:
    try:
        from tools.registry import registry  # type: ignore
        out = registry.dispatch("browser_vault", {"action": "list"})
        d = out if isinstance(out, (dict, list)) else json.loads(out)
        items = d.get("entries") if isinstance(d, dict) else d
        return [str(i.get("name") or i.get("site") or i)[:60] for i in (items or [])][:30]
    except Exception:
        return []


def capability_sheet(force: bool = False, ttl_s: float = 600) -> str:
    if not force and _CAP_CACHE["text"] and time.time() - _CAP_CACHE["t"] < ttl_s:
        return _CAP_CACHE["text"]
    scopes, hosts, chrome, vault = broker_scopes(), ssh_hosts(), chrome_accounts(), vault_entries()
    lines = ["Things the agent can do itself (generated from local config just now):"]
    lines.append("- token-broker scopes (names only; a scope named like *-minter can mint narrower tokens): "
                 + (", ".join(scopes) if scopes else "none readable"))
    lines.append("- ssh hosts: " + (", ".join(hosts) if hosts else "none"))
    lines.append("- Chrome profiles signed in (agent can drive them): " + (", ".join(chrome) if chrome else "unknown"))
    lines.append("- browser vault entries: " + (", ".join(vault) if vault else "none/unknown"))
    text = "\n".join(lines)
    _CAP_CACHE.update(t=time.time(), text=text)
    return text


def text_hash(text: str) -> str:
    return hashlib.sha1(re.sub(r"\W+", " ", text.lower()).strip().encode()).hexdigest()[:12]
