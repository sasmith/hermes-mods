#!/usr/bin/env python3
"""Hands-on digest: every Kanban card blocked on a human, as one ordered checklist.

Reads the Hermes Kanban boards read-only (default `<HERMES_HOME>/kanban.db` plus
`<HERMES_HOME>/kanban/boards/*/kanban.db`), keeps cards with status=blocked whose latest
block is a human block (kind needs_input / capability, or created blocked for human ops),
and turns their block reasons into one checklist:

  * grouped by device/location (config `groups`, keyword-scored),
  * shared steps deduped (config `shared_steps`, e.g. one "install the latest build"
    covering several cards),
  * ordered: hard deadlines first (soonest), then priority, then oldest block,
  * each item: one-line action, the card(s) it unblocks, how completion is detected.

Auto-checks (`--run-checks`): a check is a shell command from config `checks`. Exit 0 =
satisfied, 1 = not yet, anything else = error; its first stdout line is shown. A check
bound to a card (via `CHECK:` in the reason, a config `rules` regex, or `cards`) with
`"unblock": true` unblocks that card (`hermes kanban [--board B] unblock --reason ...`)
once it passes. A check bound to a shared step marks the step done.

Structured block reasons (what workers should write; anything else falls back to heuristics):

  ACTION: <one imperative line the human can do>
  DEVICE: <group name, e.g. iPhone>
  UNBLOCKS-WHEN: <observable completion condition>
  CHECK: <check name> [args]          (optional; a name from config `checks`)
  DEADLINE: 2026-10-18T19:55-07:00    (optional; hard deadline)

Keys may sit on separate lines or run together in one line; ACTION may repeat.

stdlib only. `python3 hands_digest.py --help`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

HUMAN_KINDS = {"needs_input", "capability"}
KEYS = ("ACTION", "DEVICE", "UNBLOCKS-WHEN", "CHECK", "DEADLINE")
KEY_RE = re.compile(r"(?:(?<=^)|(?<=[\s;|/]))(" + "|".join(re.escape(k) for k in KEYS) + r")\s*:\s*", re.M)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
CLAUSE_SPLIT = re.compile(r",\s*then\s+|;\s+|,\s+and then\s+|:\s+(?=[a-z])", re.I)
ACTION_CUES = re.compile(
    r"\b(you|your|needs?|needed|please|open|turn|install|unlock|plug|tap|run|say|swipe|enable|"
    r"confirm|walk|ask|answer|tell|approve|log ?in|sign|scan|press|hold|charge|lower|leave|keep|OK)\b", re.I)
STATUS_ONLY = re.compile(
    r"\b(shipped|deployed|is built|are done|on main|is live|verified|findings|released|is final|are live|"
    r"everything else|code is)\b", re.I)
EXPIRES_RE = re.compile(r"\bexpir\w*\s+(?:on\s+|at\s+)?(\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2})")
WINDOW_RE = re.compile(r"\b((?:before|after|by|until)\s+\d{1,2}:\d{2}\s*(?:PT|PDT|PST)?|in the morning|tonight|"
                       r"this evening)\b", re.I)


# ---------------------------------------------------------------- config

DEFAULT_CONFIG = {
    "timezone": "America/Los_Angeles",
    "owner": "you",
    "deadline_horizon_hours": 72,
    "max_action_chars": 240,
    "include_initial_blocked": True,
    "groups": [],          # [{"name": "iPhone", "keywords": ["iphone", ...], "weight": 1}]
    "shared_steps": [],    # [{"id", "label", "group", "sentence": regex, "value": regex, "agg": "max", "check"}]
    "rules": [],           # [{"match": regex, "check": "name {args}", "group": "...", "deadline": "..."}]
    "cards": {},           # {"t_x": {"action", "group", "check", "deadline", "unblocks_when"}}
    "checks": {},          # {"name": {"cmd": "shell {0}", "unblock": true, "timeout": 60}}
    "boards": None,        # None = default + every kanban/boards/* board
    "hermes_bin": None,
}


def load_config(path: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        with open(os.path.expanduser(path)) as f:
            cfg.update(json.load(f))
    return cfg


def tz(cfg):
    if ZoneInfo is None:
        return dt.timezone.utc
    return ZoneInfo(cfg.get("timezone") or "UTC")


# ---------------------------------------------------------------- board reading

def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def board_paths(home: Path, boards=None) -> list[tuple[str, Path]]:
    out = []
    if boards is None or "default" in boards:
        if (home / "kanban.db").exists():
            out.append(("default", home / "kanban.db"))
    for p in sorted(glob.glob(str(home / "kanban" / "boards" / "*" / "kanban.db"))):
        slug = Path(p).parent.name
        if slug.startswith("_"):
            continue
        if boards is None or slug in boards:
            out.append((slug, Path(p)))
    return out


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
    """Read-only open; `mode=ro` can fail transiently while a writer truncates the WAL, so retry, then fall
    back to a normal open with query_only."""
    import time
    for attempt in range(3):
        try:
            con = _ro_connect(path, timeout=15)
            con.execute("select 1 from tasks limit 1")
            break
        except sqlite3.OperationalError:
            time.sleep(0.5 * (attempt + 1))
    else:
        con = sqlite3.connect(str(path), timeout=15)
        con.execute("pragma query_only=1")
    con.row_factory = sqlite3.Row
    return con


def read_blocked(board: str, path: Path, include_initial=True) -> list[dict]:
    con = _ro(path)
    try:
        rows = con.execute(
            "select id, title, body, priority, block_kind, created_at from tasks where status='blocked'").fetchall()
        cards = []
        for r in rows:
            ev = con.execute(
                "select payload, created_at from task_events where task_id=? and kind='blocked' "
                "order by id desc limit 1", (r["id"],)).fetchone()
            payload = {}
            if ev and ev["payload"]:
                try:
                    payload = json.loads(ev["payload"]) or {}
                except ValueError:
                    payload = {}
            kind = payload.get("kind") or r["block_kind"]
            reason = payload.get("reason") or ""
            initial = reason == "initial_status" or (not ev)
            if kind not in HUMAN_KINDS and not (include_initial and initial and not kind):
                continue
            if initial:
                reason = initial_reason(r["body"] or "")
            cards.append({
                "id": r["id"], "board": board, "title": r["title"], "priority": r["priority"] or 0,
                "kind": kind or "initial", "reason": reason,
                "blocked_at": ev["created_at"] if ev else r["created_at"],
            })
        return cards
    finally:
        con.close()


def initial_reason(body: str) -> str:
    """A card created blocked: use its structured keys or 'Blocked until:' line from the body."""
    if KEY_RE.search(body):
        return body
    m = re.search(r"(?im)^\s*(?:\*\*)?blocked until(?:\*\*)?\s*:\s*(.+)$", body)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?im)^\s*(?:\*\*)?reason(?:\*\*)?\s*:\s*(.+(?:\n(?!\s*$|\s*\w[\w ]{0,20}:\s).+)*)", body)
    if m:   # escalation cards: a generic first line, the actual question on "Reason:"
        return m.group(1).strip()
    steps = re.search(r"(?ims)^.*(?:human|your|\w+'s) steps[^\n]*:\s*\n(.*?)(?=\n\s*(?:Agent steps|Done when|Acceptance)\b|\Z)", body)
    if steps:
        return steps.group(1).strip()
    first = next((ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")), "")
    return first


# ---------------------------------------------------------------- parsing

def parse_structured(reason: str) -> dict:
    """Split ACTION:/DEVICE:/UNBLOCKS-WHEN:/CHECK:/DEADLINE: fields. Empty dict if none."""
    marks = list(KEY_RE.finditer(reason))
    if not marks:
        return {}
    out: dict = {"actions": []}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(reason)
        val = reason[m.end():end].strip().rstrip(";|/").strip()
        key = m.group(1)
        if key == "ACTION":
            if val:
                out["actions"].append(val)
        else:
            out[key.lower().replace("-", "_")] = val
    return out


def sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    return [s.strip() for s in SENT_SPLIT.split(text) if s.strip()]


IMPERATIVE = re.compile(
    r"\b(open|turn|install|unlock|plug|tap|run|say|swipe|enable|disable|confirm|walk|ask|answer|tell|approve|"
    r"sign in|log in|scan|press|hold|charge|lower|raise|leave|keep|OK|get|start|stop|put|connect|pair|reply|"
    r"go|come|bring|grant|allow|accept|choose|pick|decide|wear|move|restart|reboot|update|enter|type|"
    r"look|listen|record|take|share|send|invite|remove|delete|unpair|reset|set|switch|toggle|click)\b", re.I)
LEAD_IN = re.compile(r"^(?:[A-Z][a-z]+|Still needed from [A-Z][a-z]+|[A-Z][a-z]+ needs to|you need to|To fix|To go ahead|"
                     r"Next|Needed)\s*:\s*", re.I)


def _strip_lead(s: str) -> str:
    """Drop a verb-less lead-in before ': ' ('The rest needs Sam's hands, in this order (...): 1) plug ...')."""
    s = LEAD_IN.sub("", s)
    s = re.sub(r"(?i)^(then|also|and)\s+", "", s)
    m = re.search(r":\s+", s)
    if m and not IMPERATIVE.search(s[:m.start()]):
        s = s[m.end():]
    return s[:1].upper() + s[1:]


def summarize_action(reason: str, limit=240) -> tuple[str, str]:
    """Heuristic (action, unblocks_when) from a free-text reason: keep the sentences that ask for something
    (imperative verbs), drop status/shipping chatter and verb-less 'needs <owner>' framing."""
    acts, cues, when = [], [], []
    for s in sentences(reason):
        if re.match(r"(?i)^(then )?unblock\b", s):
            when.append(s)
            continue
        if STATUS_ONLY.search(s) and not re.search(r"(?i)\b(needs?|still needed|to fix|to go ahead)\b", s):
            continue
        if IMPERATIVE.search(s):
            acts.append(_strip_lead(s))
        elif ACTION_CUES.search(s):
            cues.append(_strip_lead(s))
    action = " ".join(acts or cues) or reason.strip()
    return clip(action, limit), clip(" ".join(when), 160)


def drop_clauses(s: str, pat: re.Pattern) -> str:
    """Remove the clauses of sentence s that match pat (with their separator), keep the rest verbatim."""
    parts = re.split("(" + CLAUSE_SPLIT.pattern + ")", s, flags=re.I)
    clauses, seps = parts[0::2], parts[1::2]
    out, last = "", None
    for i, cl in enumerate(clauses):
        if pat.search(cl):
            continue
        if last is not None:
            out += seps[last]
        out += cl
        last = i
    out = out.strip()
    if out and not out.endswith((".", "!", "?")):
        out += "."
    return out[:1].upper() + out[1:]


def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = max(cut.rfind(". "), cut.rfind("; "))
    if sp > n * 0.6:
        return cut[:sp + 1] + " …"
    return cut[:cut.rfind(" ")].rstrip(",;:") + "…"


def parse_deadline(text: str, zone) -> dt.datetime | None:
    s = text.strip()
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})", s)
        if not m:
            return None
        try:
            d = dt.datetime.fromisoformat(f"{m.group(1)}T{int(m.group(2)):02d}:{m.group(3)}")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=zone)
    return d


def pick_group(text: str, groups: list[dict], default="Other") -> str:
    best, score = default, 0
    low = text.lower()
    for g in groups:
        sc = sum(len(re.findall(r"(?<![\w-])" + re.escape(k.lower()) + r"(?![\w])", low)) for k in g.get("keywords", []))
        sc *= g.get("weight", 1)
        if sc > score:
            best, score = g["name"], sc
    return best


# ---------------------------------------------------------------- building the digest

def build(cards: list[dict], cfg: dict, now: dt.datetime | None = None) -> dict:
    zone = tz(cfg)
    now = now or dt.datetime.now(zone)
    limit = cfg.get("max_action_chars", 240)
    shared: dict[str, dict] = {}
    items = []
    for c in cards:
        reason = c["reason"]
        st = parse_structured(reason)
        override = (cfg.get("cards") or {}).get(c["id"], {})
        free = " ".join(sentences(reason))
        # shared steps (dedupe) — pulled out of free text before summarizing
        for rule in cfg.get("shared_steps", []):
            sent_re = re.compile(rule["sentence"])
            hits = [s for s in sentences(reason) if sent_re.search(s)]
            if not hits:
                continue
            vals = []
            if rule.get("value"):
                for s in hits:
                    m = re.search(rule["value"], s)
                    if m:
                        vals.append(m.group(1))
            if rule.get("value") and not vals:
                continue
            step = shared.setdefault(rule["id"], {"rule": rule, "cards": [], "values": []})
            step["cards"].append(c)
            step["values"] += vals
            for s in hits:
                free = free.replace(s, drop_clauses(s, sent_re))
        if st.get("actions"):
            action = "; ".join(st["actions"])
            when = st.get("unblocks_when", "")
        else:
            action, when = summarize_action(free if free.strip() else reason, 10_000)
        group = override.get("group") or st.get("device") or None
        check = override.get("check") or st.get("check")
        deadline_txt = override.get("deadline") or st.get("deadline")
        for r in cfg.get("rules", []):
            if re.search(r["match"], reason):
                group = group or r.get("group")
                check = check or r.get("check")
                if r.get("action") and not st.get("actions"):
                    action = r["action"]
                if r.get("unblocks_when") and not when:
                    when = r["unblocks_when"]
        group = group or pick_group(action + " " + c["title"], cfg.get("groups", []))
        deadline = parse_deadline(deadline_txt, zone) if deadline_txt else None
        if not deadline:
            m = EXPIRES_RE.search(reason)
            deadline = parse_deadline(m.group(1), zone) if m else None
        windows = sorted({w.group(1) for w in WINDOW_RE.finditer(reason)})
        items.append({
            "kind": "card", "group": group, "action": override.get("action") or action,
            "unblocks_when": override.get("unblocks_when") or when, "check": check,
            "deadline": deadline, "windows": windows, "cards": [c], "priority": c["priority"],
            "blocked_at": c["blocked_at"], "status": None,
        })
    for sid, step in shared.items():
        rule = step["rule"]
        val = None
        if step["values"]:
            nums = [int(v) for v in step["values"] if v.isdigit()]
            agg = rule.get("agg", "max")
            val = (max(nums) if agg == "max" else min(nums)) if nums else step["values"][0]
        fmt = {"value": val if val is not None else "", "max": val if val is not None else ""}
        items.append({
            "kind": "shared", "id": sid, "group": rule.get("group") or "Other",
            "action": rule["label"].format(**fmt), "unblocks_when": rule.get("unblocks_when", "").format(**fmt),
            "check": rule["check"].format(**fmt) if rule.get("check") else None, "deadline": None, "windows": [],
            "cards": step["cards"], "priority": max(x["priority"] for x in step["cards"]) + 1,
            "blocked_at": min(x["blocked_at"] for x in step["cards"]), "status": None,
        })
    horizon = now + dt.timedelta(hours=cfg.get("deadline_horizon_hours", 72))
    for it in items:
        it["urgent"] = bool(it["deadline"] and it["deadline"] <= horizon)
    return {"now": now, "items": order(items, cfg), "cards": cards}


def order(items, cfg):
    gorder = {g["name"]: i for i, g in enumerate(cfg.get("groups", []))}
    far = dt.datetime.max.replace(tzinfo=dt.timezone.utc)

    def key(it):
        return (0 if it["urgent"] else 1, it["deadline"] or far, gorder.get(it["group"], 99),
                0 if it["kind"] == "shared" else 1, -it["priority"], it["blocked_at"])
    return sorted(items, key=key)


# ---------------------------------------------------------------- checks / auto-unblock

def run_check(spec: str, cfg: dict) -> tuple[str, str]:
    """-> (state, line) with state in ok / pending / error / unknown."""
    try:
        parts = shlex.split(spec)
    except ValueError:
        return "error", "invalid check syntax"
    if not parts:
        return "error", "empty check"
    name, args = parts[0], parts[1:]
    chk = (cfg.get("checks") or {}).get(name)
    if not chk:
        return "unknown", f"no check named {name!r} in config"
    try:
        quoted = [shlex.quote(a) for a in args]
        cmd = chk["cmd"].format(*quoted, args=" ".join(quoted))
    except (IndexError, KeyError, ValueError):
        return "error", "check arguments do not match configured command"
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=chk.get("timeout", 60))
    except subprocess.TimeoutExpired:
        return "error", "timed out"
    line = (p.stdout.strip().splitlines() or p.stderr.strip().splitlines() or [""])[0][:160]
    return {0: "ok", 1: "pending"}.get(p.returncode, "error"), line


def hermes_bin(cfg) -> str:
    return cfg.get("hermes_bin") or shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")


def unblock(card: dict, note: str, cfg: dict, dry=False) -> tuple[bool, str]:
    cmd = [hermes_bin(cfg), "kanban"]
    if card["board"] != "default":
        cmd += ["--board", card["board"]]
    cmd += ["unblock", "--reason", note, card["id"]]
    if dry:
        return True, "dry-run: " + " ".join(shlex.quote(x) for x in cmd)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-300:]


def apply_checks(digest: dict, cfg: dict, do_unblock=True, dry=False) -> list[dict]:
    events = []
    checks = cfg.get("checks") or {}
    for it in digest["items"]:
        if not it.get("check"):
            continue
        state, line = run_check(it["check"], cfg)
        it["status"] = {"state": state, "line": line}
        name = (it["check"].split() or [""])[0]
        if state == "ok" and it["kind"] == "card" and checks.get(name, {}).get("unblock") and do_unblock:
            card = it["cards"][0]
            ok, out = unblock(card, f"auto-check `{it['check']}` passed: {line}", cfg, dry=dry)
            it["status"]["unblocked"] = ok
            events.append({"card": card["id"], "board": card["board"], "check": it["check"], "line": line,
                           "unblocked": ok, "out": out})
    return events


# ---------------------------------------------------------------- rendering

def fmt_when(d: dt.datetime, now: dt.datetime) -> str:
    hrs = (d - now).total_seconds() / 3600
    rel = f"in {hrs:.0f}h" if hrs >= 0 else f"{-hrs:.0f}h ago"
    return d.astimezone(now.tzinfo).strftime("%a %b %-d %H:%M %Z") + f" ({rel})"


def _unesc(s: str) -> str:
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s or "")


def render(digest: dict, cfg: dict, max_chars: int | None = None) -> str:
    """Plain-text checklist. With max_chars, shrink per-item text until it fits (Slack posts cap ~3.5k)."""
    limits = [cfg.get("max_action_chars", 240), 200, 160, 120, 90, 60]
    text = ""
    for lim in limits:
        text = _render(digest, cfg, lim)
        if not max_chars or len(text) <= max_chars:
            break
    return text


def _render(digest: dict, cfg: dict, lim: int) -> str:
    now = digest["now"]
    items = digest["items"]
    n = len({(c["board"], c["id"]) for c in digest["cards"]})
    owner = cfg.get("owner", "you")
    if not n:
        return ""
    title_lim = max(30, lim // 4)
    out = [f"Hands-on digest: {n} card{'s' * (n != 1)} waiting on {owner} — {now.strftime('%a %b %-d, %H:%M %Z')}"]
    todo = [i for i in items if not (i.get("status") or {}).get("state") == "ok"]
    done = [i for i in items if (i.get("status") or {}).get("state") == "ok"]
    out.append(f"{len(todo)} to do" + (f", {len(done)} already done (auto-checked)" if done else "")
               + ". Items marked 🤖 unblock their card by themselves; for the rest, tell Hermes \"done <card>\""
               " (or `hermes kanban unblock <card>`).")
    num = 0
    section = None
    for it in todo:
        sec = "⏰ TIME-SENSITIVE" if it["urgent"] else it["group"]
        if sec != section:
            out.append("")
            out.append(f"== {sec}")
            section = sec
        num += 1
        tag = f"[{it['group']}] " if it["urgent"] else ""
        bot = "🤖 " if _auto_unblocks(it, cfg) else ""
        out.append(f"{num}. {bot}{tag}{clip(_unesc(it['action']), lim)}")
        flags = []
        if it["deadline"]:
            flags.append("deadline " + fmt_when(it["deadline"], now))
        if it["windows"]:
            flags.append("timing: " + ", ".join(it["windows"]))
        if flags:
            out.append("   ⏰ " + "; ".join(flags))
        out.append("   → " + ", ".join(f"{c['id']}{'@' + c['board'] if c['board'] != 'default' else ''} "
                                         f"{clip(_unesc(c['title']), title_lim)}" for c in it["cards"]))
        det = detect_text(it, cfg)
        if det:
            out.append("   done when: " + clip(det, lim))
    if done:
        out.append("")
        out.append("== ✓ Already done (auto-checked)")
        for it in done:
            st = it["status"]
            extra = " → card unblocked" if st.get("unblocked") else ""
            out.append(f"✓ {clip(_unesc(it['action']), lim // 2)} — {st['line']}{extra} "
                       f"({', '.join(c['id'] for c in it['cards'])})")
    return "\n".join(out).rstrip() + "\n"


def _auto_unblocks(it, cfg) -> bool:
    if not it.get("check") or it["kind"] != "card":
        return False
    return bool((cfg.get("checks") or {}).get((it["check"].split() or [""])[0], {}).get("unblock"))


def detect_text(it, cfg) -> str:
    """Only real detection info: an auto-check and/or an explicit UNBLOCKS-WHEN. '' when it's 'tell Hermes'."""
    parts = []
    if it.get("check"):
        st = it.get("status")
        now = f" — now {st['state']}: {st['line']}" if st else ""
        parts.append(f"auto-check `{it['check']}`{now}")
    when = "" if it.get("check") else (it.get("unblocks_when") or "")
    if when and not re.match(r"(?i)^(then )?unblock (this card|afterwards|it)?\b\W*$", when) \
            and not re.match(r"(?i)^(then )?unblock (this card|afterwards|and)\b", when):
        parts.append(when)
    return "; ".join(parts)


def to_json(digest):
    def conv(o):
        if isinstance(o, dt.datetime):
            return o.isoformat()
        raise TypeError(type(o))
    items = [{**{k: v for k, v in it.items() if k != "cards"},
              "cards": [{"id": c["id"], "board": c["board"], "title": c["title"]} for c in it["cards"]]}
             for it in digest["items"]]
    return json.dumps({"now": digest["now"], "items": items}, default=conv, indent=1)


# ---------------------------------------------------------------- main

def collect(cfg, home: Path | None = None) -> list[dict]:
    home = home or hermes_home()
    cards = []
    for slug, path in board_paths(home, cfg.get("boards")):
        try:
            cards += read_blocked(slug, path, cfg.get("include_initial_blocked", True))
        except sqlite3.Error as e:
            if "no such table" not in str(e):  # an uninitialised board dir is not an error
                print(f"warn: {slug}: {e}", file=sys.stderr)
    return cards


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(hermes_home() / "kanban-hands/config.json") if (hermes_home() / "kanban-hands/config.json").exists() else None,
                    help="JSON config (defaults to ~/.hermes/kanban-hands/config.json when present)")
    ap.add_argument("--home", help="Hermes home (default $HERMES_HOME or ~/.hermes)")
    ap.add_argument("--run-checks", action="store_true", help="run auto-checks (and auto-unblock)")
    ap.add_argument("--no-unblock", action="store_true", help="run checks but never unblock")
    ap.add_argument("--dry-run", action="store_true", help="print the unblock commands instead of running them")
    ap.add_argument("--checks-only", action="store_true",
                    help="run checks; print only auto-unblock events (silent otherwise) — for a frequent cron")
    ap.add_argument("--full", action="store_true", help="retain complete actions (no chat-post shortening)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-chars", type=int, default=None, help="shrink item text until the digest fits")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.full:
        cfg["max_action_chars"] = 100_000
    cards = collect(cfg, Path(a.home).expanduser() if a.home else None)
    digest = build(cards, cfg)
    events = []
    if a.run_checks or a.checks_only:
        events = apply_checks(digest, cfg, do_unblock=not a.no_unblock, dry=a.dry_run)
    if a.checks_only:
        for e in events:
            verb = "unblocked" if e["unblocked"] else "FAILED to unblock"
            print(f"Auto-check {verb} {e['card']}: `{e['check']}` passed ({e['line']})"
                  + ("" if e["unblocked"] else f" — {e['out']}"))
        return 0
    print(to_json(digest) if a.json else render(digest, cfg, a.max_chars), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
