#!/usr/bin/env python3
"""coach fixer lane: the coach's `fix` proposals become Kanban fix cards, at most FIX_CARDS_PER_DAY a day and
only while the lane's spend today is under FIXER_USD_DAY (stdlib only).

    coach_fixer.py spend [--days 3] [--data DIR] [--no-log]   fixer spend per UTC day; logs lane "fixer" rows
                                                               to research/state/spend.jsonl in the data repo
    coach_fixer.py gate [--data DIR]                           can a fix card be filed now? (exit 0 yes, 1 no)

Called by `coach_apply.py auto` for every pending `kind: fix` proposal. Fixer cards = every card on the board
with created_by "coach" or a `coach-fix:<slug>` / `coach-bug:<hash>` idempotency key (Bot fix cards, panel bug cards,
and fix cards a worker filed by hand with kanban_create). A card's
cost is the summed session cost of its worker runs (state.db `estimated_cost_usd`, subagent sessions folded in),
charged to the UTC day the run started.

Roots (env): COACH_DATA (~/code/hermes-coach-data), HERMES_HOME (~/.hermes), COACH_KANBAN_BOARD (hermes).
"""
import argparse, datetime as dt, json, os, re, sqlite3, sys, time
from pathlib import Path

FIXER_USD_DAY = 7.0
FIX_CARDS_PER_DAY = 1
FIX_RUNTIME_S = 3600
MIN_SESSIONS = 2
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
HOME = Path.home()


def utc_day(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def kanban_db(home, board):
    p = Path(home) / "kanban" / "boards" / board / "kanban.db"
    return p if p.exists() else Path(home) / "kanban.db"


def _ro(path):
    """Read-only connection. `mode=ro` fails on a WAL db whose -shm is absent (nobody holds it open), so fall
    back to a normal open pinned with query_only (sqlite may create the -shm; writes are still refused)."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        c.execute("select 1 from sqlite_master limit 1").fetchall()
        return c
    except sqlite3.OperationalError:
        if not Path(path).exists():
            raise
        c = sqlite3.connect(path, timeout=10)
        c.execute("pragma query_only = on")
        return c


def session_cost(conn, sid, _seen=None):
    """Cost of a session plus every subagent/continuation session under it."""
    seen = _seen if _seen is not None else set()
    if sid in seen:
        return 0.0
    seen.add(sid)
    row = conn.execute("select coalesce(actual_cost_usd, estimated_cost_usd, 0) from sessions where id=?",
                       (sid,)).fetchone()
    total = float(row[0] or 0) if row else 0.0
    for (child,) in conn.execute("select id from sessions where parent_session_id=?", (sid,)).fetchall():
        total += session_cost(conn, child, seen)
    return total


# Which cards a lane pays for. Trial cards (coach_trial.py) are created_by coach too but belong to lane "trial".
LANE_WHERE = {
    "fixer": "((t.created_by = 'coach' and coalesce(t.idempotency_key, '') not like 'coach-trial:%') "
             "or t.idempotency_key like 'coach-fix:%' or t.idempotency_key like 'coach-bug:%')",
    "trial": "t.idempotency_key like 'coach-trial:%'",
    # self-heal investigation + fix cards (plugins/coach/self_heal.py): `selfheal:<component>-<10 hex>[...]`; the
    # numbered build cards (`selfheal:1-detector`) carry no hash and are not paid by the lane
    "selfheal": "(t.idempotency_key glob 'selfheal:*-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*' "
                "or t.idempotency_key like 'selfheal-fix:%')",
}


def fixer_runs(kdb, since_ts, lane="fixer"):
    """[(task_id, idempotency_key, run_id, started_at, state_db, session_id)] for coach-created cards' runs."""
    with _ro(kdb) as k:
        cols = {r[1] for r in k.execute("pragma table_info(tasks)")}
        if "created_by" not in cols:
            return []
        has_rec = k.execute("select 1 from sqlite_master where name='worker_recovery_sessions'").fetchone()
        q = ("select r.task_id, t.idempotency_key, r.id, r.started_at, w.state_db, w.session_id, t.title, r.ended_at "
             "from task_runs r join tasks t on t.id = r.task_id "
             + ("left join worker_recovery_sessions w on w.run_id = r.id " if has_rec else
                "left join (select null run_id, null state_db, null session_id) w on 0 ")
             + f"where {LANE_WHERE[lane]} and r.started_at >= ? order by r.started_at")
        return k.execute(q, (int(since_ts),)).fetchall()


def spend_by_day(home, board, days=3, now=None, lane="fixer"):
    """{day: {"usd": float, "cards": {task_id: usd}, "runs": n}} for the last `days` UTC days (today included)."""
    now = now or time.time()
    start = dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start -= dt.timedelta(days=days - 1)
    out = {(start + dt.timedelta(days=i)).strftime("%Y-%m-%d"): {"usd": 0.0, "cards": {}, "runs": 0}
           for i in range(days)}
    kdb = kanban_db(home, board)
    if not kdb.exists():
        return out
    conns = {}
    try:
        for task_id, _key, _run, started, sdb, sid, title, ended in fixer_runs(kdb, start.timestamp(), lane):
            day = out.setdefault(utc_day(started), {"usd": 0.0, "cards": {}, "runs": 0})
            day["runs"] += 1
            usd = run_usd(home, conns, started, sdb, sid, title, ended)
            day["usd"] = round(day["usd"] + usd, 4)
            day["cards"][task_id] = round(day["cards"].get(task_id, 0.0) + usd, 4)
    finally:
        for c in conns.values():
            c.close()
    return out


def run_usd(home, conns, started, sdb, sid, title, ended):
    """Session cost of one worker run (subagents folded in). `conns` caches read-only state.db connections."""
    sdb = sdb or str(Path(home) / "state.db")
    if sdb not in conns and Path(sdb).exists():
        conns[sdb] = _ro(sdb)
    c = conns.get(sdb)
    if c is not None and not sid and title:
        # runs older than the worker-recovery ledger: the worker session carries the card title
        r = c.execute("select id from sessions where source='kanban' and parent_session_id is null and "
                      "title like ? and started_at between ? and ? order by started_at limit 1",
                      (title[:60].replace("%", "_") + "%", started - 300, (ended or started + 14400))).fetchone()
        sid = r[0] if r else None
    return session_cost(c, sid) if c is not None and sid else 0.0


def spend_log_path(data):
    return Path(data) / "research" / "state" / "spend.jsonl"


def log_spend(data, by_day, host=None, lane="fixer"):
    """Replace this host's card-spend rows of `lane` for the given days in spend.jsonl (one row per day; idempotent).
    Rows without a "cards" key (e.g. coach_eval runs logged under lane trial) are kept."""
    f = spend_log_path(data)
    f.parent.mkdir(parents=True, exist_ok=True)
    host = host or os.uname().nodename.split(".")[0]
    keep = []
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                keep.append(line)
                continue
            if not (r.get("lane") == lane and r.get("date") in by_day and r.get("host", host) == host
                    and ("cards" in r or lane == "fixer")):
                keep.append(line)
    at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for day, d in sorted(by_day.items()):
        if d["runs"] or d["usd"]:
            keep.append(json.dumps({"date": day, "at": at, "lane": lane, "usd": round(d["usd"], 4), "host": host,
                                    "what": f"{d['runs']} run(s) on {len(d['cards'])} coach card(s)",
                                    "cards": d["cards"]}))
    f.write_text("\n".join(keep) + ("\n" if keep else ""))
    return f


def logged_spend(data, day, lane="fixer"):
    """Lane spend for a day from spend.jsonl (what the Bot on another host can see)."""
    f, usd = spend_log_path(data), 0.0
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("lane") == lane and r.get("date") == day:
                usd += float(r.get("usd") or 0)
    return round(usd, 4)


def fix_cards_filed(home, board, day):
    kdb = kanban_db(home, board)
    if not kdb.exists():
        return []
    lo = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()
    with _ro(kdb) as k:
        return [r[0] for r in k.execute("select id from tasks where idempotency_key like 'coach-fix:%' "
                                        "and created_at >= ? and created_at < ?", (lo, lo + 86400))]


def existing_card(home, board, slug):
    kdb = kanban_db(home, board)
    if not kdb.exists():
        return None
    with _ro(kdb) as k:
        r = k.execute("select id, status from tasks where idempotency_key = ? order by created_at desc limit 1",
                      (f"coach-fix:{slug}",)).fetchone()
    return {"id": r[0], "status": r[1]} if r else None


def gate(home, board, now=None, by_day=None, data=None, snap=None):
    """(ok, reason, today_usd). ok=False while the lane is at its daily card cap or spend cap, or the whole
    improvement engine is at its $20/day total (engine_budget)."""
    now = now or time.time()
    day = utc_day(now)
    by_day = by_day or spend_by_day(home, board, 1, now)
    usd = by_day.get(day, {}).get("usd", 0.0)
    if usd >= FIXER_USD_DAY:
        return False, f"fixer lane spend today ${usd:.2f} >= ${FIXER_USD_DAY:.0f} cap", usd
    import engine_budget as eb
    snap = snap or eb.snapshot(home, board, data, now, by_lane={"fixer": by_day})
    ok, why = eb.check(snap, "fixer")
    if not ok:
        return False, why, usd
    filed = fix_cards_filed(home, board, day)
    if len(filed) >= FIX_CARDS_PER_DAY:
        return False, f"fixer lane already filed {', '.join(filed)} today (cap {FIX_CARDS_PER_DAY}/day)", usd
    return True, "", usd


def validate_fix(p):
    """Extra checks for kind fix (coach_proposals.validate calls this). Returns a list of errors."""
    errs, ch = [], p.get("change") if isinstance(p.get("change"), dict) else {}
    if not SLUG.match(str(p.get("target") or "")):
        errs.append("fix target must be a pattern slug (lowercase-hyphenated, 3-61 chars)")
    sess = ch.get("sessions")
    if not (isinstance(sess, list) and len({str(s) for s in sess}) >= MIN_SESSIONS):
        errs.append(f"fix change needs sessions: [>= {MIN_SESSIONS} distinct session ids] (it must recur)")
    if not str(ch.get("hypothesis") or "").strip():
        errs.append("fix change needs a hypothesis")
    acc = ch.get("acceptance")
    if not (isinstance(acc, list) and acc and all(str(a).strip() for a in acc)):
        errs.append("fix change needs acceptance: [criteria]")
    return errs


def card_body(p):
    ch = p["change"]
    sess = "\n".join(f"- {s}" for s in ch["sessions"])
    acc = "\n".join(f"- {a}" for a in ch["acceptance"])
    area = f"\nArea: {ch['area']}" if ch.get("area") else ""
    repro = f"\n\n## Repro\n{ch['repro']}" if ch.get("repro") else ""
    return (f"Fixer lane card from the coach Bot ({p.get('report')}, proposal {p['id']}). Pattern `{p['target']}`."
            f"{area}\n\n## Evidence\n{p.get('evidence')}\n\n## Sessions (repro)\n{sess}{repro}\n\n"
            f"## Hypothesis\n{ch['hypothesis']}\n\n## Acceptance\n{acc}\n"
            "- A test that fails before the fix and passes after.\n"
            "- Committed + pushed (plugin/skill: hermes-mods per the hermes-mods-repo skill; core: a `[daveey]` "
            "commit via hermes-core-wt).\n\n"
            "Rules: reproduce first; if the evidence doesn't reproduce, say so and close with the reason. "
            f"Budget: this card runs at most {FIX_RUNTIME_S // 60} min (fixer lane cap ${FIXER_USD_DAY:.0f}/day). "
            "Finish with kanban_request_review.")


def card_title(p):
    return f"Fix: {p.get('title') or p['target']}"[:120]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="coach_fixer.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("spend", "gate"):
        s = sub.add_parser(name)
        s.add_argument("--data", default=os.environ.get("COACH_DATA") or str(HOME / "code/hermes-coach-data"))
        s.add_argument("--home", default=os.environ.get("HERMES_HOME") or str(HOME / ".hermes"))
        s.add_argument("--board", default=os.environ.get("COACH_KANBAN_BOARD") or "hermes")
        if name == "spend":
            s.add_argument("--days", type=int, default=3)
            s.add_argument("--no-log", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "spend":
        by_day = spend_by_day(a.home, a.board, a.days)
        for day, d in sorted(by_day.items()):
            print(f"{day}  fixer ${d['usd']:.2f}  {d['runs']} run(s)  {json.dumps(d['cards'])}")
        if not a.no_log:
            print(f"logged to {log_spend(a.data, by_day)}")
        return 0
    ok, why, usd = gate(a.home, a.board, data=a.data)
    print(f"fixer gate: {'open' if ok else 'closed'} (today ${usd:.2f}/${FIXER_USD_DAY:.0f}){': ' + why if why else ''}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
