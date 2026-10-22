"""signals: the self-heal lane's machine-signal sweeper. No agent, no LLM, stdlib only, Python 3.9+ (the Macs run it
under /usr/bin/python3).

It turns raw machine signals into `self_heal.file_defect` calls with stable fingerprints:
- errors      new tracebacks in <home>/logs/errors.log (any level), minus tool output and provider transients;
- hook        plugin hook exceptions (`Hook '<name>' callback <cb> raised: ...` from hermes_cli/plugins_dispatch);
- cron        a cron job whose sticky `last_failure.at` is newer than the cursor (cron/jobs.json);
- kanban      a card with >= 2 runs that crashed / timed out / failed to spawn (every board db);
- gateway     gateway crash-restarts: `gateway.previous_unclean_exit`, `asyncio.run.exception`,
              `gateway.exit_nonzero` in <home>/logs/gateway-exit-diag.log;
- coach       coach `fixloop_card` (2nd confirmed failure of a fix) and `reply_nudge` handoff events.
Profiles (<home>/profiles/*) are swept too, except the coach log, which lives in <home>/coach only.

Every collector reads from a cursor (<home>/coach/signals-cursor.json: byte offsets + inode per log, last seen
run id per board, last failure time per cron job) so each signal is seen once. A first run (no cursor) looks back
BACKFILL_S (7 days). A truncated or rotated log (inode change or size < offset) is read from the start.

Commands:
  sweep [--mode shadow|live]   hub: collect local signals + drain the forwarded spool, file each one (hub cron)
  collect [--dry-run]          print new raw signals as JSONL (advances the cursor unless --dry-run)
  forward [--hub user@addr]    Mac/mini: collect and ship to the hub's `ingest` over ssh; cursor advances on success
  ingest                       hub side of forward: append JSONL from stdin to <coach>/signals/spool.jsonl
  route-monitor                file one monitor finding (JSON on stdin) and print whether the lane owns it now

The fingerprint never contains the host for code defects (one traceback on two hosts is one defect); it does for
per-host objects (a cron job, a card, a gateway), whose names are only unique per host.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

BACKFILL_S = 7 * 86400
MAX_READ = 4 * 1024 * 1024        # never read more than this from one log per pass (start at the tail instead)
MAX_PER_KIND = 25                 # cap per collector per pass: a log storm files a handful, not hundreds
FAIL_OUTCOMES = ("crashed", "timed_out", "spawn_failed")
GATEWAY_TAGS = ("gateway.previous_unclean_exit", "asyncio.run.exception", "gateway.exit_nonzero")
KINDS = ("errors", "hook", "cron", "kanban", "gateway", "coach", "monitor")
SELF_DIR = Path(__file__).resolve().parent
HUB_CMD = "python3 ~/code/hermes-mods/plugins/coach/signals.py ingest"

REC_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:,\d+)? (DEBUG|INFO|WARNING|ERROR|CRITICAL) "
                    r"(?:\[([^\]]+)\] )?([\w.\-]+): ?(.*)$")
FRAME_RE = re.compile(r'^\s*File "([^"]+)", line \d+, in (\S+)')
EXC_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Failure|Timeout|Warning|Expired|Refused|"
                    r"Closed|Missing|Invalid|Denied|NotFound)\w*|[A-Za-z_][\w.]*\.[A-Z]\w+)(?::\s*(.*))?$")
HOOK_RE = re.compile(r"^(Hook|Event) '([\w.]+)' callback (\S+) raised: (.*?)(?: \(\w+ provides: .*)?$")
# Tool output (a traceback the agent's own command printed) and provider-side transients are not Hermes defects.
SKIP_LOGGERS = re.compile(r"^(agent\.tool_executor|tools\.terminal_tool|tools\.code_execution)")
TRANSIENT_RE = re.compile(r"RateLimitError|rate_limit_error|\b429\b|overloaded_error|\b529\b|APIConnectionError|"
                          r"APITimeoutError|ReadTimeout|ConnectTimeout|RemoteProtocolError|CancelledError|"
                          r"KeyboardInterrupt|InternalServerError.*\b5\d\d\b", re.I)
PLUGIN_PATH_RE = re.compile(r"/plugins/([\w.\-]+)/")


# ── small helpers ─────────────────────────────────────────────────────────────────────────────────────────────

def h10(*parts) -> str:
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:10]


def fp(component: str, *parts) -> str:
    return f"{re.sub(r'[^a-z]', '', component.lower()) or 'other'}-{h10(component, *parts)}"


def home_tilde(s: str) -> str:
    return re.sub(r"/(?:Users|home)/[^/\s\"']+", "~", s or "")


def _ts(s: str) -> float:
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


def _iso_ts(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def homes(home: Path) -> List[Tuple[str, Path]]:
    """(profile, home) for the default home and every profile under it."""
    out = [("default", home)]
    for p in sorted(glob.glob(str(home / "profiles" / "*"))):
        if Path(p).is_dir():
            out.append((Path(p).name, Path(p)))
    return out


def read_new(path: Path, cur: dict, key: str, now: float) -> List[str]:
    """New complete lines of `path` since cur[key] = {"ino", "off"}; updates cur. First sight: the last MAX_READ."""
    try:
        st = path.stat()
    except OSError:
        return []
    c = cur.get(key) or {}
    off = int(c.get("off") or 0)
    if c.get("ino") != st.st_ino or st.st_size < off:
        off = 0
    if not c:  # first sight: read the tail only
        off = max(0, st.st_size - MAX_READ)
    if st.st_size - off > MAX_READ:
        off = st.st_size - MAX_READ
    with path.open("rb") as f:
        f.seek(off)
        data = f.read(st.st_size - off)
    end = data.rfind(b"\n") + 1  # leave a half-written last line for the next pass
    cur[key] = {"ino": st.st_ino, "off": off + end}
    lines = data[:end].decode("utf-8", "replace").splitlines()
    if off and lines and not c:  # started mid-file: the first line may be partial
        lines = lines[1:]
    return lines


# ── collectors: each returns raw signals {kind, key, component, symptom, details, at, session_id?, quote?, fp} ─

def _own_frame(path: str) -> bool:
    q = path.replace("\\", "/")
    return "site-packages" not in q and "/lib/python" not in q


def component_of(paths: List[str]) -> Tuple[str, str]:
    """(component, plugin name) from traceback frame paths, innermost first."""
    for p in reversed(paths):
        m = PLUGIN_PATH_RE.search(p)
        if m:
            return "plugin", m.group(1)
    for p in reversed(paths):
        q = p.replace("\\", "/")
        if "site-packages" in q or "/lib/python" in q:
            continue
        for needle, comp in (("/gateway/", "gateway"), ("/cron/", "cron"), ("kanban", "kanban"),
                             ("/tui_gateway/", "desktop"), ("/apps/desktop/", "desktop"), ("/hermes_cli/", "agent"),
                             ("/agent/", "agent"), ("/tools/", "agent")):
            if needle in q:
                return comp, ""
    return "agent", ""


def parse_errors(lines: List[str]) -> List[dict]:
    """Records (header + continuation lines) -> traceback and hook-exception signals."""
    recs, cur = [], None
    for ln in lines:
        m = REC_RE.match(ln)
        if m:
            cur = {"at": _ts(m.group(1)), "level": m.group(2), "sid": m.group(3) or "", "logger": m.group(4),
                   "msg": m.group(5), "cont": []}
            recs.append(cur)
        elif cur is not None:
            cur["cont"].append(ln)
    out = []
    for r in recs:
        if SKIP_LOGGERS.match(r["logger"]):
            continue
        hm = HOOK_RE.match(r["msg"])
        if hm:
            surface, hook, cb, err = hm.groups()
            exc = err.split(":")[0][:60]
            plug = cb.split(".")[0]
            out.append({"kind": "hook", "component": "plugin", "at": r["at"], "session_id": r["sid"],
                        "symptom": f"plugin hook {hook} callback {cb} raised {err[:120]}",
                        "details": home_tilde(r["msg"])[:1500],
                        "fp": fp("plugin", "hook", hook, cb, re.sub(r"\d+", "N", exc))})
            continue
        if not any(l.startswith("Traceback (most recent call last)") for l in r["cont"]):
            continue
        # the last chained exception block
        start = max(i for i, l in enumerate(r["cont"]) if l.startswith("Traceback (most recent call last)"))
        block = r["cont"][start:]
        frames = [FRAME_RE.match(l).groups() for l in block if FRAME_RE.match(l)]
        exc_line = next((l for l in reversed(block) if l and not l[0].isspace() and EXC_RE.match(l)), "")
        exc_type = (EXC_RE.match(exc_line).group(1) if exc_line else "") or "Exception"
        text = r["msg"] + "\n" + exc_line
        if TRANSIENT_RE.search(text):
            continue
        comp, plug = component_of([f[0] for f in frames])
        if r["level"] == "WARNING" and not any(_own_frame(p) for p, _fn in frames):
            continue  # a library logging a handled exception (e.g. mcp's PermissionError); nothing of ours to fix
        inner = [(os.path.basename(p), fn) for p, fn in frames[-4:]]
        where = f"{inner[-1][1]} ({inner[-1][0]})" if inner else r["logger"]
        sym = f"{exc_type.split('.')[-1]} in {where}" + (f" [{plug}]" if plug else "") + f": {r['msg'][:90]}"
        details = home_tilde("\n".join([f"{r['level']} {r['logger']}: {r['msg']}"] + block))
        out.append({"kind": "errors", "component": comp, "at": r["at"], "session_id": r["sid"], "symptom": sym,
                    "details": details[-1500:], "fp": fp(comp, "tb", exc_type, *[f"{a}:{b}" for a, b in inner])})
    return out


def collect_errors(home: Path, cur: dict, now: float) -> List[dict]:
    out = []
    for prof, h in homes(home):
        lines = read_new(h / "logs" / "errors.log", cur, f"errors:{prof}", now)
        for s in parse_errors(lines):
            if s["at"] >= now - BACKFILL_S:
                s["profile"] = prof
                out.append(s)
    return out


def collect_cron(home: Path, cur: dict, now: float, host: str) -> List[dict]:
    out = []
    seen = cur.setdefault("cron", {})
    first = not seen
    for prof, h in homes(home):
        try:
            j = json.loads((h / "cron" / "jobs.json").read_text())
        except Exception:
            continue
        for job in (j.get("jobs") if isinstance(j, dict) else j) or []:
            lf = job.get("last_failure") or {}
            at = _iso_ts(lf.get("at"))
            k = f"{prof}:{job.get('id')}"
            prev = float(seen.get(k) or 0)
            if not at or at <= prev or (first and at < now - BACKFILL_S):
                seen[k] = max(prev, at)
                continue
            seen[k] = at
            name = job.get("name") or job.get("id")
            detail = str(lf.get("detail") or job.get("last_error") or "run failed")
            out.append({"kind": "cron", "component": "cron", "at": at, "profile": prof,
                        "symptom": f"cron job '{name}' failed on {host}: {detail.splitlines()[0][:120] if detail else ''}",
                        "details": home_tilde(f"job {job.get('id')} ({name}, profile {prof}) failure_streak="
                                              f"{job.get('failure_streak')} last_status={job.get('last_status')}\n"
                                              f"script={job.get('script')}\n{detail}")[:1500],
                        "fp": fp("cron", host, prof, name)})
    return out


def _ro(db: Path):
    """Read-only-safe open of a live WAL db (mode=ro fails when -wal/-shm were deleted): ro, else rw + query_only."""
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        c.execute("select 1 from sqlite_master limit 1")
        return c
    except sqlite3.Error:
        c = sqlite3.connect(f"file:{db}?mode=rw", uri=True, timeout=10)
        c.execute("PRAGMA query_only=ON")
        return c


def board_dbs(home: Path) -> List[Tuple[str, Path]]:
    out, seen = [], set()
    for prof, h in homes(home):
        cands = [("default", h / "kanban.db")] + [(Path(p).parent.name, Path(p)) for p in
                                                  sorted(glob.glob(str(h / "kanban" / "boards" / "*" / "kanban.db")))]
        for name, db in cands:
            rp = os.path.realpath(db)
            if db.is_file() and db.stat().st_size and rp not in seen:
                seen.add(rp)
                out.append((name if prof == "default" else f"{prof}/{name}", db))
    return out


def collect_kanban(home: Path, cur: dict, now: float, host: str) -> List[dict]:
    out = []
    seen = cur.setdefault("kanban", {})
    q = ",".join("?" * len(FAIL_OUTCOMES))
    for board, db in board_dbs(home):
        try:
            c = _ro(db)
            last = int(seen.get(board) or 0)
            top = c.execute("select coalesce(max(id),0) from task_runs").fetchone()[0]
            rows = c.execute(
                f"select r.task_id, count(*), max(r.id), max(coalesce(r.ended_at, r.started_at)), "
                f"group_concat(r.outcome), group_concat(substr(coalesce(r.error,''),1,200), ' | '), t.title, "
                f"coalesce(t.idempotency_key,''), t.status from task_runs r join tasks t on t.id = r.task_id "
                f"where r.outcome in ({q}) group by r.task_id having count(*) >= 2 and max(r.id) > ?",
                (*FAIL_OUTCOMES, last)).fetchall()
            c.close()
        except Exception:
            continue
        seen[board] = top
        for tid, n, _mx, ended, outcomes, errs, title, ikey, status in rows:
            if (ended or 0) < now - BACKFILL_S or ikey.startswith("selfheal:"):
                continue  # self-heal's own cards: the lane reconciles them (no card about a card about a card)
            out.append({"kind": "kanban", "component": "kanban", "at": float(ended or now),
                        "symptom": f"kanban card {tid} on {host}/{board} failed {n}x ({outcomes}): {title[:80]}",
                        "details": home_tilde(f"board {board} card {tid} status={status} title={title}\n"
                                              f"outcomes: {outcomes}\nerrors: {errs}")[:1500],
                        "fp": fp("kanban", host, board, tid)})
    return out


def collect_gateway(home: Path, cur: dict, now: float, host: str) -> List[dict]:
    out = []
    for prof, h in homes(home):
        for ln in read_new(h / "logs" / "gateway-exit-diag.log", cur, f"gateway:{prof}", now):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            tag = r.get("tag")
            if tag not in GATEWAY_TAGS:
                continue
            at = _iso_ts(r.get("ts"))
            if at < now - BACKFILL_S:
                continue
            why = r.get("exc_type") or ("suspected OOM" if r.get("suspected_oom") else "") or tag.split(".")[-1]
            out.append({"kind": "gateway", "component": "gateway", "at": at, "profile": prof,
                        "symptom": f"gateway ({prof}) on {host} died uncleanly: {tag} {why}",
                        "details": home_tilde(json.dumps({k: v for k, v in r.items() if k != "argv"}))[:1500],
                        "fp": fp("gateway", host, prof, tag, r.get("exc_type") or "")})
    return out


def collect_coach(home: Path, cur: dict, now: float) -> List[dict]:
    out = []
    for ln in read_new(home / "coach" / "log.jsonl", cur, "coach", now):
        if '"fixloop_card"' not in ln and '"reply_nudge"' not in ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        at = float(r.get("ts") or now)
        if at < now - BACKFILL_S:
            continue
        if r.get("event") == "fixloop_card":
            issue = str(r.get("issue") or "a fix that keeps failing")
            out.append({"kind": "coach", "component": "agent", "at": at, "session_id": r.get("session_id") or "",
                        "symptom": f"claimed fix failed {r.get('n') or 2}x: {issue[:120]}",
                        "details": f"coach fix_loop opened review card {r.get('task_id')} after the "
                                   f"{r.get('n') or 2}nd confirmed failure: {issue}"[:1500],
                        "fp": fp("agent", "fixloop", r.get("session_id") or "", issue[:80])})
        elif r.get("event") == "reply_nudge" and r.get("rule") == "handoff":
            ev = str(r.get("evidence") or "")
            out.append({"kind": "coach", "component": "agent", "at": at, "session_id": r.get("session_id") or "",
                        "symptom": f"agent handed the user a step it could do itself: {ev[:120]}",
                        "details": f"coach reply_check handoff nudge (p={r.get('p')}): {ev}"[:1500],
                        "fp": fp("agent", "handoff", r.get("session_id") or "")})
    return out


def collect(home: Path, cur: dict, host: str, now: Optional[float] = None) -> List[dict]:
    now = now or time.time()
    out = []
    for kind, fn in (("errors", lambda: collect_errors(home, cur, now)),
                     ("cron", lambda: collect_cron(home, cur, now, host)),
                     ("kanban", lambda: collect_kanban(home, cur, now, host)),
                     ("gateway", lambda: collect_gateway(home, cur, now, host)),
                     ("coach", lambda: collect_coach(home, cur, now))):
        try:
            sigs = fn()
        except Exception as exc:  # one broken source never stops the others
            sigs = [{"kind": "collector", "component": "plugin", "at": now,
                     "symptom": f"self-heal signal collector '{kind}' crashed: {type(exc).__name__}",
                     "details": f"{type(exc).__name__}: {exc}"[:600], "fp": fp("plugin", "collector", kind)}]
        by_fp: Dict[str, dict] = {}
        for s in sigs:  # one signal per fingerprint per pass (the latest), then the cap
            s.setdefault("count", 1)
            if s["fp"] in by_fp:
                s["count"] = by_fp[s["fp"]]["count"] + 1
            by_fp[s["fp"]] = s
        out += sorted(by_fp.values(), key=lambda s: s["at"])[-MAX_PER_KIND:]
    for s in out:
        s["host"] = host
    return out


def to_defect(s: dict) -> dict:
    """Raw signal -> self_heal.file_defect input."""
    d = {"component": s.get("component") or "other", "symptom": s.get("symptom") or s.get("kind"),
         "source": "signal", "kind": s.get("kind"), "details": s.get("details") or "", "host": s.get("host"),
         "at": s.get("at"), "fingerprint": s.get("fp"), "session_id": s.get("session_id") or None,
         "quote": s.get("quote") or ""}
    if (s.get("count") or 1) > 1:
        d["details"] = f"(seen {s['count']}x this pass)\n" + d["details"]
    for k in ("profile", "monitor"):
        if s.get(k):
            d[k] = s[k]
    return d


# ── state ────────────────────────────────────────────────────────────────────────────────────────────────────

def coach_dir(home: Path) -> Path:
    return home / "coach"


def load_cursor(home: Path) -> dict:
    try:
        return json.loads((coach_dir(home) / "signals-cursor.json").read_text())
    except Exception:
        return {}


def save_cursor(home: Path, cur: dict) -> None:
    p = coach_dir(home) / "signals-cursor.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1))
    tmp.replace(p)


def config(home: Path) -> dict:
    try:
        return json.loads((coach_dir(home) / "selfheal-config.json").read_text())
    except Exception:
        return {}


def short_host() -> str:
    return os.uname().nodename.split(".")[0]


def _self_heal():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_coach_self_heal_signals", SELF_DIR / "self_heal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def drain_spool(home: Path) -> List[dict]:
    spool = coach_dir(home) / "signals" / "spool.jsonl"
    offp = coach_dir(home) / "signals" / "spool.offset"
    if not spool.exists():
        return []
    data = spool.read_bytes()
    # Only newline-terminated lines are complete: a concurrent `ingest` may be
    # mid-write, and a partial last line must be left for the next sweep, not
    # dropped and skipped by the offset (the forwarder's cursor already moved).
    lines = data[:data.rfind(b"\n") + 1].decode("utf-8", "replace").split("\n")[:-1]
    try:
        off = int(offp.read_text().strip() or 0)
    except Exception:
        off = 0
    if off > len(lines):
        off = 0
    out = []
    for ln in lines[off:]:
        try:
            s = json.loads(ln)
        except Exception:
            continue
        if isinstance(s, dict) and s.get("fp") and s.get("kind") in KINDS + ("collector",):
            out.append(s)
    offp.write_text(str(len(lines)))
    return out


def sweep(home: Path, host: str, m: Optional[str] = None, filer: Optional[Callable] = None,
          now: Optional[float] = None) -> List[dict]:
    """Hub pass: local signals + forwarded ones -> file_defect. Returns [{signal, result}]."""
    now = now or time.time()
    cur = load_cursor(home)
    sigs = collect(home, cur, host, now) + drain_spool(home) + silent_forwarders(home, now)
    if filer is None:
        sh = _self_heal()
        filer = lambda d: sh.file_defect(d, m=m)  # noqa: E731
    out = []
    for s in sigs:
        try:
            res = filer(to_defect(s))
        except Exception as exc:
            res = {"action": "error", "why": f"{type(exc).__name__}: {exc}"[:300]}
        out.append({"signal": s, "result": res})
    save_cursor(home, cur)  # after filing: a crash mid-pass re-reads the same lines (file_defect dedupes)
    return out


def ssh_argv(cfg: dict) -> List[str]:
    """Same as self_heal.hub_ssh_argv (kept local so the Mac forwarder needs only this file): pin the hub key with
    IdentitiesOnly, since launchd's default identities are not the shell's and the hub refuses them preauth."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    kp = Path(os.path.expanduser(cfg.get("hub_ssh_key") or "~/.ssh/id_ed25519"))
    if cfg.get("hub_ssh_key") or kp.exists():
        argv += ["-i", str(kp), "-o", "IdentitiesOnly=yes"]
    return argv


def forward(home: Path, host: str, hub: str, runner: Optional[Callable] = None, now: Optional[float] = None,
            cfg: Optional[dict] = None) -> dict:
    """Mac/mini: ship new raw signals to the hub. The cursor only moves when the hub accepted them. Every pass sends
    a heartbeat line first (even with no signals), so the hub knows the forwarder is alive (signals/hosts.json) and
    its sweep flags a host whose forwarder went silent."""
    cur = load_cursor(home)
    sigs = collect(home, cur, host, now)
    beat = {"kind": "heartbeat", "host": host, "at": now or time.time(), "n": len(sigs)}
    payload = "".join(json.dumps(s) + "\n" for s in [beat] + sigs)
    run = runner or (lambda argv, stdin: subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=90))
    try:
        r = run(ssh_argv(cfg if cfg is not None else config(home)) + [hub, HUB_CMD], payload)
        ok, detail = r.returncode == 0, (r.stdout or r.stderr or "").strip()[-200:]
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"[:200]
    if ok:
        save_cursor(home, cur)
    return {"ok": ok, "sent": len(sigs) if ok else 0, "pending": 0 if ok else len(sigs), "detail": detail}


SILENT_S = 6 * 3600              # a forwarder (launchd, every 30 min) quiet this long is itself a defect


def ingest(home: Path, text: str, now: Optional[float] = None) -> int:
    spool = coach_dir(home) / "signals" / "spool.jsonl"
    spool.parent.mkdir(parents=True, exist_ok=True)
    now = now or time.time()
    n = 0
    beats = {}
    with spool.open("a") as f:
        for ln in text.splitlines():
            try:
                s = json.loads(ln)
            except Exception:
                continue
            if isinstance(s, dict) and s.get("kind") == "heartbeat" and s.get("host"):
                beats[str(s["host"])[:40]] = {"at": now, "n": s.get("n")}
                continue
            if not isinstance(s, dict) or not s.get("fp") or s.get("kind") not in KINDS + ("collector",):
                continue
            s["received_at"] = now
            f.write(json.dumps(s) + "\n")
            n += 1
    if beats:
        hp = coach_dir(home) / "signals" / "hosts.json"
        try:
            hosts = json.loads(hp.read_text())
        except Exception:
            hosts = {}
        hosts.update(beats)
        tmp = hp.with_suffix(".tmp")
        tmp.write_text(json.dumps(hosts, indent=1))
        tmp.replace(hp)
    return n


def silent_forwarders(home: Path, now: Optional[float] = None) -> List[dict]:
    """Hosts that forwarded before but not in SILENT_S: the forwarder (or the host's feed job / ssh) broke."""
    now = now or time.time()
    try:
        hosts = json.loads((coach_dir(home) / "signals" / "hosts.json").read_text())
    except Exception:
        return []
    out = []
    for h, v in sorted(hosts.items()):
        age = now - float(v.get("at") or 0)
        if age > SILENT_S:
            out.append({"kind": "collector", "component": "plugin", "at": now, "host": h,
                        "symptom": f"self-heal signal forwarder on {h} has not reported for {age / 3600:.0f} h",
                        "details": f"last heartbeat {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(v.get('at') or 0))}"
                                   f"; the forwarder rides on that host's launchd coach feed (coach_feed.py, every "
                                   f"30 min) and ssh to the hub",
                        "fp": fp("plugin", "forwarder-silent", h)})
    return out


# ── monitors: a propose-only monitor files its finding to the lane instead of posting a bare Do-it card ──────

def monitor_defect(m: dict, host: Optional[str] = None) -> dict:
    """{monitor, key, title, body, prompt?, component?} -> file_defect input. The fingerprint is the monitor's own
    dedupe key (e.g. worktree-leaks:<host>), so a daily re-report of the same finding dedupes in the lane. The old
    Do-it prompt rides along as the monitor's suggested first step; the investigation card turns the finding into
    the What happened / Why / Plan proposal."""
    key = m.get("key") or m.get("title") or "monitor"
    comp = m.get("component") or "agent"
    body = home_tilde(m.get("body") or "")
    hint = home_tilde(m.get("prompt") or "")
    details = (body[:1100] + (f"\n\nMonitor's own suggested next step: {hint}" if hint else ""))[:1500]
    s = {"kind": "monitor", "component": comp, "at": time.time(), "host": host or short_host(),
         "symptom": m.get("title") or key, "details": details,
         "fp": fp(comp, "monitor", key), "monitor": m.get("monitor") or key.split(":")[0]}
    return to_defect(s)


def lane_owns(res: dict) -> bool:
    """True when the self-heal lane took the finding (filed, forwarded and filed on the hub, or already open there),
    so the monitor must not also post its old Do-it card. Shadow (would_file), off, and errors -> False."""
    a = (res or {}).get("action")
    if a == "forwarded":
        return lane_owns(res.get("hub_result") or {})
    if a == "filed":
        return True
    return a == "skip" and "COACH_SELFHEAL=off" not in str(res.get("why"))


def route_monitor(m: dict, filer: Optional[Callable] = None) -> dict:
    d = monitor_defect(m)
    try:
        res = (filer or _self_heal().file_defect)(d)
    except Exception as exc:
        res = {"action": "error", "why": f"{type(exc).__name__}: {exc}"[:300]}
    return {"owned": lane_owns(res), "fingerprint": d["fingerprint"], "result": res}


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _summ(res: List[dict]) -> List[str]:
    lines = []
    for r in res:
        a = r["result"].get("action")
        if a == "skip":
            continue
        s = r["signal"]
        lines.append(f"{a}: [{s.get('host')}/{s.get('kind')}] {s.get('fp')} {s.get('symptom', '')[:140]}"
                     + (f" -> {r['result'].get('card')}" if r["result"].get("card") else "")
                     + (f" ({r['result'].get('why')})" if a == "error" else ""))
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="signals.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    ap.add_argument("--host", default=None, help="host label (default: the short hostname)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sweep")
    s.add_argument("--mode", choices=("shadow", "live"))
    s.add_argument("--verbose", action="store_true", help="also print skipped signals")
    c = sub.add_parser("collect")
    c.add_argument("--dry-run", action="store_true", help="do not advance the cursor")
    f = sub.add_parser("forward")
    f.add_argument("--hub", help="ssh target (default: hub_ssh in <home>/coach/selfheal-config.json)")
    sub.add_parser("ingest")
    sub.add_parser("route-monitor")
    a = ap.parse_args(argv)
    home = Path(a.home).expanduser()
    host = a.host or short_host()
    if a.cmd == "sweep":
        res = sweep(home, host, m=a.mode)
        for ln in _summ(res) if not a.verbose else [json.dumps(r)[:400] for r in res]:
            print(ln)
    elif a.cmd == "collect":
        cur = load_cursor(home)
        for sg in collect(home, cur, host):
            print(json.dumps(sg))
        if not a.dry_run:
            save_cursor(home, cur)
    elif a.cmd == "forward":
        cfg = config(home)
        hub = a.hub or cfg.get("hub_ssh")
        # Every outcome prints one line: the feed records it (<host>/feed-status.json in the coach data repo), so
        # the hub can tell "no config yet" from "ssh failing" from "working" without a shell on this host.
        if cfg.get("hub_host") and cfg.get("hub_host") == short_host():
            print("signals: this host is the hub (its own cron sweeps locally)")
            return 0
        if not hub:
            print(f"signals: no hub configured (<home>/coach/selfheal-config.json missing or has no hub_ssh)")
            return 0
        r = forward(home, host, hub, cfg=cfg)
        if not r["ok"]:
            print(f"signals forward to {hub} failed ({r['pending']} pending): {r['detail']}", file=sys.stderr)
            return 1
        print(f"signals: forwarded {r['sent']} to {hub}" if r["sent"] else f"signals: heartbeat ok to {hub}")
    elif a.cmd == "ingest":
        print(json.dumps({"ingested": ingest(home, sys.stdin.read())}))
    elif a.cmd == "route-monitor":
        print(json.dumps(route_monitor(json.loads(sys.stdin.read()))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
