"""user-steps core: find what a chat reply asks the USER to do, keep a per-chat ledger, frame it in the reply.

Pure-ish functions + small classes so tests need no Hermes runtime:
  - extract(): aux-model call (injected ``llm``) -> validated steps + resolved ids; regex fallback on failure.
  - Ledger: ~/.hermes/user-steps/<lineage root>.json (atomic writes, fcntl lock), read across compression lineage.
  - frame(): replace the paragraph holding each step's verbatim span with ``::user-step{...}`` (text kept in an attr).
  - process_reply(): the whole transform_llm_output pipeline, with injected llm / ledger / card ops.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

KINDS = ("approve", "do", "choose", "input")
MAX_NEW_PER_REPLY = 5
MAX_NEW_PER_HOUR = 12
ATTR_BUDGET = 1024
DIRECTIVE = "user-step"


def home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


# ---- session lineage (compression rotates the stored id) -----------------------------------------------

def _state_db(h: Optional[Path] = None) -> Path:
    return (h or home()) / "state.db"


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


def _row(db: Path, sid: str) -> Optional[tuple]:
    try:
        c = _ro_connect(db, timeout=2)
        try:
            return c.execute("select id, parent_session_id, end_reason, source from sessions where id=?",
                             (sid,)).fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return None


def lineage(session_id: str, db: Optional[Path] = None) -> list[str]:
    """[session_id, parent, grandparent, ...] following only compression hops (newest first)."""
    db = db or _state_db()
    out = [session_id] if session_id else []
    cur = session_id
    for _ in range(24):
        r = _row(db, cur)
        parent = r[1] if r else None
        if not parent or parent in out:
            break
        pr = _row(db, parent)
        if not pr or "compress" not in str(pr[2] or "").lower():
            break
        out.append(parent)
        cur = parent
    return out


def lineage_root(session_id: str, db: Optional[Path] = None) -> str:
    ids = lineage(session_id, db)
    return ids[-1] if ids else session_id


def session_source(session_id: str, db: Optional[Path] = None) -> str:
    r = _row(db or _state_db(), session_id)
    return str(r[3] or "") if r else ""


# ---- ledger --------------------------------------------------------------------------------------------

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class Ledger:
    """One JSON file per lineage root: {root, steps:[...], card:{id,board}|null, gen, user_msg}."""

    def __init__(self, root_dir: Optional[Path] = None, db: Optional[Path] = None):
        self.dir = Path(root_dir) if root_dir else home() / "user-steps"
        self.db = db

    def root(self, session_id: str) -> str:
        return lineage_root(session_id, self.db or _state_db())

    def path(self, root: str) -> Path:
        return self.dir / (_SAFE.sub("_", root) + ".json")

    @contextmanager
    def edit(self, root: str):
        self.dir.mkdir(parents=True, exist_ok=True)
        lock = open(self.dir / ".lock", "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            d = self._read(root)
            yield d
            d["updated"] = time.time()
            p = self.path(root)
            tmp = p.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
            os.replace(tmp, p)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def _read(self, root: str) -> dict:
        try:
            d = json.loads(self.path(root).read_text())
            if isinstance(d, dict):
                d.setdefault("steps", [])
                return d
        except (OSError, ValueError):
            pass
        return {"root": root, "steps": [], "card": None, "gen": 0}

    def read(self, session_id: str) -> dict:
        return self._read(self.root(session_id))

    def open_steps(self, session_id: str) -> list[dict]:
        return [s for s in self.read(session_id).get("steps", []) if s.get("status") == "open"]

    def find(self, step_id: str, session_id: str = "") -> tuple[Optional[str], Optional[dict]]:
        """(root, step). Uses the session hint first, then scans every ledger (ids are globally unique)."""
        if session_id:
            root = self.root(session_id)
            for s in self._read(root).get("steps", []):
                if s.get("id") == step_id:
                    return root, s
        for p in sorted(self.dir.glob("*.json")) if self.dir.is_dir() else []:
            try:
                d = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            for s in d.get("steps", []):
                if s.get("id") == step_id:
                    return d.get("root") or p.stem, s
        return None, None


def new_step_id() -> str:
    return "us_" + uuid.uuid4().hex[:10]


# ---- noise stripping / sentences (shared with the regex fallback and the guards) -----------------------

_FENCE = re.compile(r"```.*?(```|\Z)", re.S)


def _strip_noise(text: str) -> str:
    text = _FENCE.sub(" ", text or "")
    text = re.sub(r"(?m)^\s*>.*$", " ", text)
    text = re.sub(r"`[^`\n]*`", "X", text)
    return text


def _norm(s: str) -> str:
    s = re.sub(r"[*_`~]", "", s or "")
    s = s.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def sentences(text: str) -> list[str]:
    return [s.strip(" -*•#|") for s in re.split(r"(?<=[.!?])\s+|\n+", _strip_noise(text)) if s.strip(" -*•#|")]


# Asks (regex fallback, carried over from open-asks). Narrow: a false step is noise.
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
    r"\b(if you (ever|later|still|prefer|want|need|would)|unless|in case|whenever you|no action (is )?needed|"
    r"nothing (is )?(waiting|needed|needs?) (on|from|for) you|nothing for you|you (already|have already)|"
    r"you (said|answered|approved|asked)|don'?t need (anything|you)|no (more|further) (input|action)|"
    r"the card asks|it asked|would have asked|next time)\b",
    re.I)
_APPROVE = re.compile(r"\b(want me to|should i|shall i|do you want me to|would you like me to|ok(ay)? (for me )?to|"
                      r"go-ahead|go/no-go|approve|approval)\b", re.I)
_DO = re.compile(r"\b(can you|could you|please) (check|click|tap|open|plug|sign|log|restart|run)\b", re.I)


def regex_steps(text: str) -> list[dict]:
    out: list[dict] = []
    for s in sentences(text or ""):
        if _ASK.search(s) and not _NOT_ASK.search(s) and all(o["verbatim_span"] != s for o in out):
            kind = "approve" if _APPROVE.search(s) else "do" if _DO.search(s) else "input"
            out.append({"text": s[:300], "kind": kind, "verbatim_span": s, "options": []})
    return out


# ---- the aux-model extractor ---------------------------------------------------------------------------

SYSTEM = """You read an AI assistant's final chat reply to its user and list what the reply asks the USER to do \
or decide right now. Also say which previously-open steps are now resolved.

Return ONLY JSON:
{"steps":[{"text":"...","kind":"approve|do|choose|input","verbatim_span":"...","options":["..."]}],
 "resolved":["<open step id>", ...]}

Kinds:
- approve: the assistant proposes an action and waits for a go/no ("Want me to deploy it?", "OK to delete X?").
- do: the user must perform something outside the chat (click, plug in, sign in, buy, test on a device),
  then tell the assistant it is done.
- choose: the user must pick one of 2-4 named options. Put the options in "options", recommended first, each
  under 40 characters.
- input: the user must type information or an open answer (a name, a value, a free-form decision).

Rules:
- Only LIVE asks addressed to the user in THIS reply. Not: mentions of what the user could do someday,
  hypotheticals ("if you ever want...", "unless you..."), past asks, things already answered, asks to third
  parties, text inside code blocks or quotes, rhetorical questions, or the assistant's own next steps.
- "text": one short imperative line for the user (<= 120 chars), e.g. "Approve deploying build 412 to prod".
- "verbatim_span": an EXACT substring copied from the reply (one sentence or line) where the ask is made.
- One step per distinct ask; at most 5.
- "resolved": ids from OPEN STEPS that the USER'S LAST MESSAGE answered, did, declined or made moot, or that the
  reply says are now done. Leave a step out when unsure.
- No asks: {"steps":[],"resolved":[...]}."""


def build_prompt(reply: str, open_steps: list[dict], user_msg: str) -> str:
    opened = "\n".join(f"- {s['id']} ({s.get('kind')}): {s.get('text')}" for s in open_steps) or "(none)"
    return (f"OPEN STEPS:\n{opened}\n\nUSER'S LAST MESSAGE:\n{(user_msg or '(none)')[:1500]}\n\n"
            f"ASSISTANT REPLY:\n{(reply or '')[:12000]}")


def _parse_json(raw: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    d = json.loads(m.group(0))
    if not isinstance(d, dict):
        raise ValueError("model output is not an object")
    return d


def _blocks(text: str) -> list[dict]:
    """Markdown blocks split on blank lines, with code fences kept whole: [{start, end, text, code, quote}]."""
    out: list[dict] = []
    lines = (text or "").split("\n")
    pos = 0
    cur: Optional[dict] = None
    fence = False
    for ln in lines:
        start, end = pos, pos + len(ln)
        pos = end + 1
        is_fence = ln.lstrip().startswith("```")
        if fence:
            cur["end"] = end
            if is_fence:
                fence = False
                out.append(cur)
                cur = None
            continue
        if is_fence:
            if cur:
                out.append(cur)
            cur = {"start": start, "end": end, "code": True}
            fence = True
            continue
        if not ln.strip():
            if cur:
                out.append(cur)
                cur = None
            continue
        if cur is None:
            cur = {"start": start, "end": end, "code": False}
        else:
            cur["end"] = end
    if cur:
        out.append(cur)
    for b in out:
        b["text"] = text[b["start"]:b["end"]]
        b["quote"] = not b["code"] and all(l.lstrip().startswith(">") for l in b["text"].split("\n") if l.strip())
    return out


def locate(reply: str, span: str) -> Optional[dict]:
    """The prose block (not code, not quote) whose text contains ``span`` (markdown/whitespace-insensitive)."""
    ns = _norm(span)
    if len(ns) < 4:
        return None
    for b in _blocks(reply):
        if b["code"] or b["quote"]:
            continue
        nb = _norm(re.sub(r"`[^`\n]*`", lambda m: m.group(0).strip("`"), b["text"]))
        if ns in nb or (len(ns) > 60 and ns[:60] in nb):
            return b
    return None


_HANDOFF: dict[str, Any] = {}


def _handoff():
    """ask-gate's shared hand-off detector (plugins/ask-gate/handoff.py), loaded by file path. None if absent."""
    if "mod" not in _HANDOFF:
        _HANDOFF["mod"] = None
        for base in (Path(__file__).resolve().parent.parent, Path(__file__).parent.parent):
            f = base / "ask-gate" / "handoff.py"
            if f.is_file():
                try:
                    import importlib.util
                    spec = importlib.util.spec_from_file_location("user_steps_handoff", f)
                    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                    spec.loader.exec_module(mod)  # type: ignore[union-attr]
                    _HANDOFF["mod"] = mod
                    break
                except Exception:
                    pass
    return _HANDOFF["mod"]


def is_terminal_handoff(text: str, span: str, reply: str = "") -> bool:
    """A step a shell could do (run/paste a command, add an ssh key, sudo, type a password at a prompt) is never a
    For-you step: the agent must call run_in_user_terminal instead. Physical steps (taps, GUI logins, hardware) stay."""
    h = _handoff()
    if h is None:
        return False
    i = reply.find(span) if span else -1
    ctx = reply[i:i + len(span) + 400] if i >= 0 else span
    return bool(h.find_handoffs(text) or h.find_handoffs(span) or h.find_handoffs(ctx))


def validate(raw_steps: Any, reply: str, open_steps: list[dict], dropped: Optional[list] = None) -> list[dict]:
    out: list[dict] = []
    seen = {_norm(s.get("text", "")) for s in open_steps}
    for s in raw_steps if isinstance(raw_steps, list) else []:
        if not isinstance(s, dict):
            continue
        text = re.sub(r"\s+", " ", str(s.get("text") or "")).strip()[:300]
        span = str(s.get("verbatim_span") or "").strip()
        kind = str(s.get("kind") or "").strip().lower()
        opts = [re.sub(r"\s+", " ", str(o)).strip()[:60] for o in (s.get("options") or []) if str(o).strip()][:4]
        if not text or not span:
            continue
        if kind not in KINDS:
            kind = "input"
        if kind == "choose" and len(opts) < 2:
            kind = "input"
        if kind != "choose":
            opts = []
        if _NOT_ASK.search(span):
            continue  # hypothetical / mention-only, whatever the model said
        if locate(reply, span) is None:
            continue  # not in the reply's prose (hallucinated, or inside code/quote)
        if _norm(text) in seen:
            continue
        if is_terminal_handoff(text, span, reply):
            if dropped is not None:
                dropped.append(text)
            continue  # terminal-doable: the agent runs it via run_in_user_terminal, no For-you card
        seen.add(_norm(text))
        out.append({"text": text, "kind": kind, "verbatim_span": span, "options": opts})
    return out[:MAX_NEW_PER_REPLY]


LLM = Callable[[str, str], str]


def extract(reply: str, open_steps: list[dict], user_msg: str, llm: Optional[LLM]) -> dict:
    """{steps, resolved, source: 'model'|'regex', error?}."""
    ids = {s["id"] for s in open_steps}
    dropped: list[str] = []
    if llm is not None:
        try:
            d = _parse_json(llm(SYSTEM, build_prompt(reply, open_steps, user_msg)))
            resolved = [str(x) for x in (d.get("resolved") or []) if str(x) in ids]
            return {"steps": validate(d.get("steps"), reply, open_steps, dropped), "resolved": resolved,
                    "source": "model", "dropped": dropped}
        except Exception as exc:  # fall back to the regex detector; resolution then only comes from buttons
            err = f"{type(exc).__name__}: {exc}"[:200]
            dropped.clear()
            return {"steps": validate(regex_steps(reply), reply, open_steps, dropped), "resolved": [], "source": "regex",
                    "error": err, "dropped": dropped}
    return {"steps": validate(regex_steps(reply), reply, open_steps, dropped), "resolved": [], "source": "regex",
            "dropped": dropped}


# ---- in-message frame ----------------------------------------------------------------------------------

def enc(s: str) -> str:
    """Attr-safe text: the directive grammar forbids braces and double quotes inside attrs."""
    return (s.replace("\\", "\\\\").replace("\n", "\\n").replace('"', "\\q").replace("{", "\\(")
            .replace("}", "\\)"))


def dec(s: str) -> str:
    out, i = [], 0
    m = {"\\": "\\", "n": "\n", "q": '"', "(": "{", ")": "}"}
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in m:
            out.append(m[s[i + 1]])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def directive(ids: list[str], session_id: str, text: str = "") -> str:
    head = f'::{DIRECTIVE}{{id="{",".join(ids)}" s="{_SAFE.sub("", session_id)}"'
    if text:
        head += f' t="{enc(text)}"'
    return head + "}"


def frame(reply: str, steps: list[dict], session_id: str) -> str:
    """Replace each step's paragraph with a directive that carries the paragraph text. A paragraph too long for
    the attr budget falls back to its single line; a line still too long stays as text and the directive goes
    right after it (empty ``t``). Text is never dropped."""
    by_block: dict[tuple, dict] = {}
    for s in steps:
        b = locate(reply, s["verbatim_span"])
        if b is None:
            continue
        key = (b["start"], b["end"])
        by_block.setdefault(key, {"block": b, "ids": []})["ids"].append(s["id"])
    edits: list[tuple[int, int, str]] = []
    for (start, end), g in by_block.items():
        ids = g["ids"]
        btext = g["block"]["text"]
        base = len(directive(ids, session_id, "x")) - 1
        if base + len(enc(btext)) <= ATTR_BUDGET:
            edits.append((start, end, directive(ids, session_id, btext)))
            continue
        # one line of the block
        span = next(s["verbatim_span"] for s in steps if s["id"] == ids[0])
        off = start
        placed = False
        for ln in btext.split("\n"):
            if _norm(span)[:60] in _norm(ln):
                if base + len(enc(ln)) <= ATTR_BUDGET:
                    edits.append((off, off + len(ln), directive(ids, session_id, ln)))
                else:
                    edits.append((off + len(ln), off + len(ln), "\n\n" + directive(ids, session_id)))
                placed = True
                break
            off += len(ln) + 1
        if not placed:
            edits.append((end, end, "\n\n" + directive(ids, session_id)))
    out = reply
    for start, end, rep in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + rep + out[end:]
    return out


# ---- rolling "waiting on you" card ---------------------------------------------------------------------

def card_title(open_steps: list[dict]) -> str:
    first = open_steps[0]["text"] if open_steps else ""
    more = f" (+{len(open_steps) - 1} more)" if len(open_steps) > 1 else ""
    t = f"For you: {first}"
    return (t if len(t) <= 100 else t[:99].rstrip() + "…") + more


def card_body(session_ref: str, open_steps: list[dict]) -> str:
    lines = "\n".join(f"{i}. {s['text']}  ({s.get('kind')}, `{s['id']}`)" for i, s in enumerate(open_steps, 1))
    return (f"Chat {session_ref} is waiting on you for these steps. Filed and kept up to date by the "
            "`user-steps` plugin; it archives this card when the last step is resolved.\n\n"
            f"## Steps\n{lines}\n\n"
            "## How this closes\n"
            "- Click the buttons in the chat's **For you** strip (or in the framed box in the message), or just "
            "answer in the chat.\n"
            "- Archiving this card by hand does not resolve the steps; unblocking it sends no answer.\n")


class Cards:
    """Interface for the rolling card. Default implementation lives in the plugin (__init__/plugin_api)."""

    def create(self, session_id: str, title: str, body: str, key: str) -> Optional[dict]:  # -> {id, board}
        raise NotImplementedError

    def update(self, card: dict, title: str, body: str) -> None:
        raise NotImplementedError

    def archive(self, card: dict) -> None:
        raise NotImplementedError


def sync_card(ledger: Ledger, root: str, session_id: str, session_ref: str, cards: Optional[Cards]) -> Optional[dict]:
    """Make the rolling card match the ledger: create while steps are open, update its body, archive at zero."""
    if cards is None:
        return None
    d = ledger._read(root)
    opened = [s for s in d.get("steps", []) if s.get("status") == "open"]
    card = d.get("card")
    sig = hashlib.sha1(json.dumps([s["id"] for s in opened]).encode()).hexdigest()[:12]
    if not opened:
        if card:
            if cards.archive(card) is False:
                return card  # archive failed: keep the pointer so the next sync retries
            with ledger.edit(root) as e:
                e["card"] = None
                e.setdefault("archived_cards", []).append(card.get("id"))
        return None
    title, body = card_title(opened), card_body(session_ref, opened)
    if not card:
        gen = int(d.get("gen", 0)) + 1
        made = cards.create(session_id, title, body, f"user-steps:{root}:{gen}")
        if made and made.get("id"):
            with ledger.edit(root) as e:
                e["card"] = {**made, "sig": sig}
                e["gen"] = gen
            return made
        return None
    if card.get("sig") != sig:
        cards.update(card, title, body)
        with ledger.edit(root) as e:
            if e.get("card"):
                e["card"]["sig"] = sig
    return card


# ---- REST payloads (here, not in plugin_api.py, so edits apply without a backend restart) ----------------

_PUBLIC = ("id", "text", "kind", "options", "status", "created", "session", "paragraph", "resolution", "resolved_at")


def public_step(step: dict) -> dict:
    return {k: step.get(k) for k in _PUBLIC if k in step}


def steps_payload(ledger: "Ledger", session: str) -> dict:
    """GET /steps: open steps oldest first, the rolling card, and every rolling card id this chat ever had."""
    if not session:
        return {"root": None, "steps": [], "card": None, "own_cards": []}
    root = ledger.root(session)
    d = ledger._read(root)
    opened = sorted((s for s in d.get("steps", []) if s.get("status") == "open"), key=lambda s: s.get("created", 0))
    own = [c for c in [(d.get("card") or {}).get("id"), *d.get("archived_cards", [])] if c]
    return {"root": root, "steps": [public_step(s) for s in opened], "card": d.get("card"), "own_cards": own[-50:]}


# ---- resolve / pipeline --------------------------------------------------------------------------------

def resolve(ledger: Ledger, root: str, ids: list[str], resolution: str, by: str) -> list[str]:
    done: list[str] = []
    with ledger.edit(root) as d:
        for s in d.get("steps", []):
            if s.get("id") in ids and s.get("status") == "open":
                s.update(status="resolved", resolution=(resolution or "")[:200], resolved_by=by,
                         resolved_at=time.time())
                done.append(s["id"])
    return done


def process_reply(reply: str, session_id: str, *, user_msg: str = "", llm: Optional[LLM] = None,
                  ledger: Ledger, cards: Optional[Cards] = None, framed: bool = True,
                  session_ref: str = "", now: Optional[float] = None) -> dict:
    """Run one final reply through the pipeline. Returns {text (None = unchanged), steps, resolved, source}."""
    now = now or time.time()
    root = ledger.root(session_id)
    opened = ledger.open_steps(session_id)
    if not opened and not _worth_asking(reply):
        return {"text": None, "steps": [], "resolved": [], "source": "skip"}
    res = extract(reply, opened, user_msg, llm)
    resolved = resolve(ledger, root, res["resolved"], "answered in chat", "model") if res["resolved"] else []
    recent = sum(1 for s in ledger._read(root).get("steps", []) if now - s.get("created", 0) < 3600)
    new = res["steps"][: max(0, MAX_NEW_PER_HOUR - recent)]
    for s in new:
        s.update(id=new_step_id(), status="open", created=now, session=session_id)
        b = locate(reply, s["verbatim_span"])
        s["paragraph"] = b["text"] if b else s["verbatim_span"]
    if new:
        with ledger.edit(root) as d:
            d["root"] = root
            d["steps"].extend(new)
    text = frame(reply, new, session_id) if (new and framed) else None
    if new or resolved:
        sync_card(ledger, root, session_id, session_ref or f"@session:default/{session_id}", cards)
    return {"text": text if text != reply else None, "steps": new, "resolved": resolved, "source": res["source"],
            "dropped": res.get("dropped") or [], **({"error": res["error"]} if res.get("error") else {})}


_WORTH = re.compile(
    r"\?|\byou(r|'ll|'d)?\b|\bplease\b|\b(let|tell) me\b|\bneeds? (your|you)\b"
    # Imperative verbs also count after a comma or "or/and/then": "To get it, tap the bar, or go to Settings"
    # names no "you" and starts no sentence with the verb, and a skip here never reaches the model.
    r"|(^|[.!:,;]\s+|\b(?:or|and|then)\s+|\n\s*(?:[-*•]|\d+[.)])?\s*)(plug|open|click|tap|sign|log|run|check|install|restart|reboot|reply|"
    r"send|grant|allow|approve|confirm|pick|choose|decide|test|try|buy|enter|paste|scan|unlock|turn|press|hold|"
    r"connect|charge|go to|update|add|remove|delete|upload|share|accept)\b", re.I | re.M)


def _worth_asking(reply: str) -> bool:
    """Cheap pre-filter so a plain report with no open steps never costs a model call."""
    t = _strip_noise(reply or "")
    return len(t.strip()) >= 15 and bool(_WORTH.search(t))


def reminder(open_steps: list[dict]) -> str:
    lines = ["[user-steps] Steps still waiting on the user in this chat (shown in the Desktop 'For you' strip "
             "with buttons; a click arrives as a short user message like \"✓ Done: …\", \"Go: …\", \"Chose: …\"):"]
    for s in open_steps[:10]:
        opts = f" options: {' | '.join(s.get('options') or [])}" if s.get("options") else ""
        lines.append(f"- {s['id']} ({s.get('kind')}): {s.get('text')}{opts}")
    lines.append("Act on any the user's message answers. Don't re-ask an open step verbatim; the strip already "
                 "shows it. Steps are marked resolved automatically from the user's reply.")
    return "\n".join(lines)
