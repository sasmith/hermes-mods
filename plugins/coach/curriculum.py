"""coach curriculum: in-line feature reminders on the coach bus (Phase 3).

Entries come from the public `curriculum.yaml` next to this file, overlaid by `<HERMES_HOME>/coach/curriculum.yaml`
(same `id` replaces, `enabled: false` drops). Each entry has a live `trigger`, a one-line `tip`, and an `adopted` test
that the miner (hermes-coach `coach_mine.py --write-adoption`) evaluates over the digest repo into
`reports/adoption.json`. This module only does the live half:

  * `text_hits(text)`         -> ids whose text/list trigger matches a user turn (recorded by Store.user_turn)
  * `candidates(...)`         -> at most one tip whose trigger holds now and that spacing/adoption allow
  * `on_shown / on_choice`    -> spaced repetition in `<coach>/curriculum_state.json`

Spacing: a shown tip waits SHOW_GAP_S before it can show again anywhere; an accepted one ACCEPT_GAP_S (time to try it);
each dismissal doubles the wait (DISMISS_BASE_S * 2^(k-1), capped). A tip the miner marks adopted retires for good.
At most DAILY_MAX tips show per day across all sessions. The bus gate (1 nudge / 15 turns / session, session mute
on dismiss, weekly demotion) still applies on top; tips use rule names `tip:<id>`.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

SHOW_GAP_S = 20 * 3600
ACCEPT_GAP_S = 3 * 86400
DISMISS_BASE_S = 2 * 86400
DISMISS_MAX_S = 30 * 86400
DAILY_MAX = 2
TEXT_TURNS = 2           # a text trigger counts for this many user turns after the matching one
LIST_RE = re.compile(r"(?m)^\s*(?:\d{1,2}[.)]|[-*•]|\[ ?\])\s+\S")
PUBLIC = Path(__file__).resolve().parent / "curriculum.yaml"


# ── loading ───────────────────────────────────────────────────────────────

def _yaml_load(text: str):
    """PyYAML when present, else ruamel.yaml (what Hermes ships), else JSON."""
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        pass
    try:
        from ruamel.yaml import YAML  # type: ignore
        return YAML(typ="safe", pure=True).load(text)
    except ImportError:
        pass
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError("curriculum: needs PyYAML or ruamel.yaml to read YAML") from exc


def _entries(path: Path) -> list:
    try:
        data = _yaml_load(path.read_text())
    except FileNotFoundError:
        return []
    if isinstance(data, dict):
        data = data.get("entries") or []
    return [e for e in (data or []) if isinstance(e, dict)]


def entry_id(e: dict) -> str:
    return str(e.get("id") or e.get("feature") or "").strip()


def runnable(e: dict) -> bool:
    """Has a machine-readable trigger + adopted test (prose entries from a proposal are kept but never fire)."""
    return isinstance(e.get("trigger"), (dict, list)) and isinstance(e.get("adopted"), dict) and bool(e.get("tip"))


_cache: dict = {}


def load(overlay: Optional[Path] = None, public: Path = PUBLIC) -> list[dict]:
    """Merged curriculum, public order first, overlay-only entries appended. Cached on file mtimes."""
    paths = [public] + ([overlay] if overlay else [])
    key = tuple((str(p), p.stat().st_mtime_ns if p.exists() else None) for p in paths)
    if _cache.get("key") == key:
        return _cache["val"]
    merged: dict = {}
    for p in paths:
        for e in _entries(p):
            i = entry_id(e)
            if i:
                merged[i] = {**merged.get(i, {}), **e, "id": i}
    val = [e for e in merged.values() if e.get("enabled", True) is not False]
    _cache.update(key=key, val=val)
    return val


# ── adoption (written by the miner on the coach Bot, read here) ──────────

def adoption_path() -> Path:
    env = os.environ.get("COACH_ADOPTION")
    if env:
        return Path(env).expanduser()
    data = os.environ.get("COACH_DATA_DIR") or "~/code/hermes-coach-data"
    return Path(data).expanduser() / "reports" / "adoption.json"


def adopted_ids(path: Optional[Path] = None) -> set:
    try:
        d = json.loads((path or adoption_path()).read_text())
    except Exception:
        return set()
    return {i for i, r in (d.get("entries") or {}).items() if isinstance(r, dict) and r.get("adopted")}


# ── live triggers ─────────────────────────────────────────────────────────

def _conds(trigger) -> list[dict]:
    """A trigger as a list of alternatives (each a dict of AND-ed conditions)."""
    if isinstance(trigger, list):
        return [t for t in trigger if isinstance(t, dict)]
    if isinstance(trigger, dict):
        if isinstance(trigger.get("any"), list):
            rest = {k: v for k, v in trigger.items() if k != "any"}
            return [{**rest, **t} for t in trigger["any"] if isinstance(t, dict)]
        return [trigger]
    return []


def _text_match(cond: dict, text: str) -> bool:
    ok = True
    if "text" in cond:
        try:
            ok = bool(re.search(cond["text"], text))
        except re.error:
            ok = False
    if ok and "list_items" in cond:
        ok = len(LIST_RE.findall(text)) >= int(cond["list_items"])
    return ok


def text_hits(entries: list[dict], text: str) -> list[str]:
    """Ids with a text/list condition that this user turn satisfies (other conditions are checked later)."""
    out = []
    t = (text or "")[:4000]
    for e in entries:
        if not runnable(e):
            continue
        if any(("text" in c or "list_items" in c) and _text_match(c, t) for c in _conds(e["trigger"])):
            out.append(e["id"])
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


class Facts:
    """Lazy state.db facts about one session (each query runs at most once per evaluation)."""

    def __init__(self, ids: list[str], db_path: Optional[Path], clock: Callable[[], float] = time.time):
        self.ids, self.db_path, self.clock = ids, db_path, clock
        self._c: dict = {}

    def _con(self):
        try:
            con = _ro_connect(self.db_path, timeout=5)
            con.row_factory = sqlite3.Row
            return con
        except Exception:
            return None

    def _q(self, key, fn):
        if key not in self._c:
            con = self._con()
            try:
                self._c[key] = fn(con) if con else None
            except Exception:
                self._c[key] = None
            finally:
                if con:
                    con.close()
        return self._c[key]

    def session(self) -> dict:
        def fn(con):
            cols = {r[1] for r in con.execute("pragma table_info(sessions)")}
            want = [c for c in ("source", "cwd", "git_repo_root", "title") if c in cols]
            row = con.execute(f"select {', '.join(want) or 'id'} from sessions where id=?", (self.ids[-1],)).fetchone()
            return dict(row) if row else {}
        return self._q("session", fn) or {}

    def repo(self) -> str:
        s = self.session()
        return str(s.get("git_repo_root") or s.get("cwd") or "")

    def tools(self) -> dict:
        def fn(con):
            q = ",".join("?" * len(self.ids))
            n: dict = {}
            for (raw,) in con.execute(f"select tool_calls from messages where session_id in ({q}) and tool_calls is not null",
                                      self.ids):
                try:
                    calls = json.loads(raw)
                except Exception:
                    continue
                for c in calls if isinstance(calls, list) else []:
                    fn_ = (c.get("function") or {}) if isinstance(c, dict) else {}
                    name = fn_.get("name") or (c.get("name") if isinstance(c, dict) else None)
                    if not name:
                        continue
                    n[name] = n.get(name, 0) + 1
                    if name == "tool_call":
                        try:
                            a = fn_.get("arguments")
                            a = json.loads(a) if isinstance(a, str) else (a or {})
                            for inner in a.get("calls") or []:
                                if isinstance(inner, dict) and inner.get("name"):
                                    n[inner["name"]] = n.get(inner["name"], 0) + 1
                        except Exception:
                            pass
            return n
        return self._q("tools", fn) or {}

    def repo_sessions(self, days: float, minutes: Optional[float] = None) -> int:
        """Top-level sessions in this session's repo (cwd or git root), started in the window / active recently."""
        repo = self.repo()
        if not repo:
            return 0
        key = f"repo:{days}:{minutes}"

        def fn(con):
            cols = {r[1] for r in con.execute("pragma table_info(sessions)")}
            if "started_at" not in cols:
                return 0
            where = ["parent_session_id is null"]
            args: list = []
            loc = [c for c in ("git_repo_root", "cwd") if c in cols]
            where.append("(" + " or ".join(f"{c} = ?" for c in loc) + ")")
            args += [repo] * len(loc)
            if minutes is not None:
                act = "coalesce(last_activity_at, started_at)" if "last_activity_at" in cols else "started_at"
                where.append(f"{act} >= ?")
                args.append(self.clock() - minutes * 60)
                if "ended_at" in cols:
                    where.append("ended_at is null")
            else:
                where.append("started_at >= ?")
                args.append(self.clock() - days * 86400)
            if "source" in cols:
                where.append("coalesce(source,'') not in ('cron','kanban','oneshot','api_server','tool')")
            # compression children are not separate sessions
            return con.execute(f"select count(*) from sessions where {' and '.join(where)}", args).fetchone()[0]
        return int(self._q(key, fn) or 0)


def _project(repo: str) -> str:
    name = Path(repo.rstrip("/")).name if repo else ""
    return re.sub(r"[-_](wt|worktree|\d+)$", "", name) or "this project"


def trigger_holds(e: dict, sig: dict, facts: Facts, msgs: int, recent_text: set) -> Optional[dict]:
    """The fill-in values when some alternative of the entry's trigger holds now, else None."""
    for c in _conds(e.get("trigger")):
        fmt: dict = {}
        ok = True
        if ("text" in c or "list_items" in c) and e["id"] not in recent_text:
            ok = False
        if ok and c.get("long_job") and not sig.get("long_job"):
            ok = False
        for k in ("subagents", "still_broken"):
            if ok and k in c and int(sig.get(k) or 0) < int(c[k]):
                ok = False
        if ok and "min_msgs" in c and msgs < int(c["min_msgs"]):
            ok = False
        if ok and "source" in c:
            src = facts.session().get("source")
            ok = src in (c["source"] if isinstance(c["source"], list) else [c["source"]])
        if ok and isinstance(c.get("tool"), dict):
            used = facts.tools()
            ok = all(used.get(name, 0) >= int(n) for name, n in c["tool"].items())
        if ok and c.get("unless_tool"):
            used = facts.tools()
            names = c["unless_tool"] if isinstance(c["unless_tool"], list) else [c["unless_tool"]]
            ok = not any(used.get(n) for n in names)
        for k in ("project_sessions", "concurrent"):
            if ok and isinstance(c.get(k), dict):
                spec = c[k]
                repo = facts.repo()
                if spec.get("match") and not re.search(spec["match"], repo or ""):
                    ok = False
                    break
                n = facts.repo_sessions(float(spec.get("days", 14)),
                                        float(spec.get("minutes", 30)) if k == "concurrent" else None)
                ok = n >= int(spec.get("n", 2))
                fmt.update(n=n, repo=repo.replace(str(Path.home()), "~", 1), project=_project(repo))
        if ok:
            return fmt
    return None


# ── spaced repetition state ───────────────────────────────────────────────

class Spacing:
    def __init__(self, root: Path, clock: Callable[[], float] = time.time):
        self.path = Path(root) / "curriculum_state.json"
        self.clock = clock

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def _write(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, sort_keys=True))
        os.replace(tmp, self.path)

    def ready(self, tip: str, d: Optional[dict] = None) -> bool:
        r = (d if d is not None else self.read()).get("tips", {}).get(tip, {})
        return self.clock() >= float(r.get("next_at") or 0)

    def shown_today(self, d: Optional[dict] = None) -> int:
        cut = self.clock() - 86400
        return sum(1 for t in (d if d is not None else self.read()).get("shows", []) if t >= cut)

    def on_shown(self, tip: str) -> None:
        d = self.read()
        now = self.clock()
        r = d.setdefault("tips", {}).setdefault(tip, {})
        r["shows"] = int(r.get("shows") or 0) + 1
        r["last_shown"] = now
        r["next_at"] = max(float(r.get("next_at") or 0), now + SHOW_GAP_S)
        d["shows"] = [t for t in d.get("shows", []) if t >= now - 86400] + [now]
        self._write(d)

    def on_choice(self, tip: str, choice: str) -> None:
        d = self.read()
        now = self.clock()
        r = d.setdefault("tips", {}).setdefault(tip, {})
        if choice == "dismissed":
            k = r["dismissals"] = int(r.get("dismissals") or 0) + 1
            r["next_at"] = now + min(DISMISS_MAX_S, DISMISS_BASE_S * 2 ** (k - 1))
        elif choice == "accepted":
            r["accepts"] = int(r.get("accepts") or 0) + 1
            r["next_at"] = now + ACCEPT_GAP_S
        elif choice == "expired":  # never really seen: free it again (the session key still blocks a repeat there)
            r["next_at"] = now
            # drop this tip's own show from the daily count, not whichever tip showed last
            last, shows = r.get("last_shown"), list(d.get("shows", []))
            if last in shows:
                shows.remove(last)
            d["shows"] = shows
        else:
            return
        r["last_choice"] = choice
        self._write(d)

    def retire(self, adopted: set) -> None:
        d = self.read()
        changed = False
        for tip in adopted:
            r = d.setdefault("tips", {}).setdefault(tip, {})
            if not r.get("retired_at"):
                r["retired_at"] = self.clock()
                changed = True
        if changed:
            self._write(d)


def candidates(entries: list[dict], sig: dict, facts: Facts, msgs: int, recent_text: set, spacing: Spacing,
               adopted: set) -> list[dict]:
    """Due tips whose trigger holds, in curriculum order. The bus gate shows at most one: if it rejects
    the first (e.g. tip:X muted in this session), the next one gets its chance in the same check."""
    st = spacing.read()
    spacing.retire(adopted & {e["id"] for e in entries})
    if spacing.shown_today(st) >= DAILY_MAX:
        return []
    out = []
    for e in entries:
        if not runnable(e) or e["id"] in adopted or not spacing.ready(e["id"], st):
            continue
        fmt = trigger_holds(e, sig, facts, msgs, recent_text)
        if fmt is None:
            continue
        out.append({"rule": f"tip:{e['id']}", "key": f"tip:{e['id']}", "fmt": fmt, "entry": e})
    return out


class _Fmt(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def tip_nudge(c: dict) -> dict:
    e, fmt = c["entry"], _Fmt(c["fmt"] or {})
    try_text = str(e.get("try") or "").format_map(fmt) or None
    return {"title": f"Tip · {str(e.get('feature') or e['id']).format_map(fmt)}",
            "text": str(e["tip"]).format_map(fmt), "button": "Try it" if try_text else "Got it", "action": "tip",
            "data": {"tip": e["id"], "try": try_text}}
