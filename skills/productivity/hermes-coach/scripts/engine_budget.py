#!/usr/bin/env python3
"""engine_budget: one hard $20/UTC-day cap across every improvement-engine lane, plus the in-flight watchdog
(stdlib only).

    engine_budget.py status [--day YYYY-MM-DD]       today's per-lane spend vs caps (exit 1 when any gate is closed)
    engine_budget.py watchdog [--dry-run]            stop running engine cards over budget; resume yesterday's stops

Lanes (docs/improvement-engine-plan.md "Budget"): scout $3, fixer $7, trial $10, total $20 per UTC day. `bot` (the
coach Bot's own chat turns, logged by the nous-1 daily prerun as a cumulative-cost delta) has no lane cap of its own
but counts toward the total. Spend for a UTC day:

    scout, bot   ledger rows (research/state/spend.jsonl in the coach data repo)
    fixer        live worker-session cost of this host's coach fix cards (coach_fixer.LANE_WHERE) when the board
                 is readable, else this host's logged rows; plus other hosts' logged rows
    trial        the same for coach-trial cards, plus every coach_eval reserve/settle row (rows without "cards")

Every lane gate (scout X calls, coach_fixer.gate, coach_trial.gate, coach_eval.Budget) refuses when its lane cap OR
the total is reached. Raise a cap for one day: append {"date": "<UTC day>", "lane": "fixer|trial|scout|total",
"usd": N, "why": "..."} to research/state/budget-overrides.jsonl (the Inbox card's Do-it prompt does that).

Watchdog (in `coach-apply auto` and a launchd pass every 15 min on the MacBook): for every RUNNING engine card
(created_by coach, or a coach-fix:/coach-bug:/coach-trial: key) it stops the card when the card's own lifetime cost
reached its budget (a "Budget [for this card] <= $N" line in the body, else the lane cap) or its lane / the total cap is
reached: `hermes kanban schedule <id> "SCHEDULED_UNTIL=<next UTC midnight> — budget cap reached; resumes next UTC
day"`, then SIGTERM of its worker (only a process whose env carries HERMES_KANBAN_TASK=<id>). Non-engine cards are
never touched. Each stop is appended to research/state/budget-stops.jsonl (the weekly digest's budget section lists
them) and one Inbox card per UTC day (key engine-budget:<day>) carries the per-lane numbers with a Do-it prompt to
raise the cap. On a later UTC day the watchdog unblocks the cards it scheduled for a lane/total cap (still
`scheduled`). A card that spent its OWN budget is instead blocked needs_input and never auto-resumes (its lifetime
cost never drops, so a resume would only be stopped again every day): raise its Budget line or close it.

Roots (env): COACH_DATA (~/code/hermes-coach-data), HERMES_HOME (~/.hermes), COACH_KANBAN_BOARD (hermes).
"""
import argparse, datetime as dt, json, os, re, shutil, signal, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coach_fixer as fx  # noqa: E402

CAPS = {"scout": 3.0, "fixer": 7.0, "trial": 10.0, "selfheal": 25.0}
TOTAL_USD_DAY = 20.0
LANES = ("scout", "fixer", "trial", "bot", "selfheal")
# Lanes with their own approved daily cap that do not count toward (or stop at) the engine total: self-heal
# ($25/UTC day, plugins/coach/self_heal.py, approved separately from the $20 engine budget).
SEPARATE = ("selfheal",)
LIVE_LANES = ("fixer", "trial", "selfheal")
HOME = Path.home()
# "Budget for this card ≤ $8", "Budget ≤ $4", "budget: $5" (never the fixer body's "Budget: this card runs at most…")
CARD_BUDGET_RX = re.compile(r"(?i)\bbudget(?: for this card)?\s*(?:≤|<=|<|:|is|of)?\s*\$\s*(\d+(?:\.\d+)?)")


def host():
    return os.uname().nodename.split(".")[0]


def state_dir(data):
    return Path(data) / "research" / "state"


def jsonl(path):
    out = []
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def caps_for(data, day):
    """(lane caps, total cap) for a UTC day, with budget-overrides.jsonl applied (the last row per lane wins)."""
    caps, total = dict(CAPS), TOTAL_USD_DAY
    for r in jsonl(state_dir(data) / "budget-overrides.jsonl"):
        if r.get("date") != day:
            continue
        try:
            usd = float(r.get("usd"))
        except (TypeError, ValueError):
            continue
        if r.get("lane") == "total":
            total = usd
        elif r.get("lane") in caps:
            caps[r["lane"]] = usd
    return caps, total


def snapshot(home=None, board=None, data=None, now=None, live=True, by_lane=None, me=None):
    """Spend for the UTC day containing `now`:
    {"day", "lanes": {scout, fixer, trial, bot}, "eval", "cards": {task: usd}, "total", "caps", "total_cap", "live"}.
    by_lane = precomputed {"fixer"|"trial": coach_fixer.spend_by_day(...)} to skip a board read."""
    now = now or time.time()
    day = fx.utc_day(now)
    home = Path(home or os.environ.get("HERMES_HOME") or HOME / ".hermes")
    board = board or os.environ.get("COACH_KANBAN_BOARD") or "hermes"
    data = Path(data or os.environ.get("COACH_DATA") or HOME / "code/hermes-coach-data")
    me = me or host()
    lanes = {k: 0.0 for k in LANES}
    cards, ev = {}, 0.0
    live_lanes = {}
    if live and fx.kanban_db(home, board).exists():
        for lane in LIVE_LANES:
            bd = (by_lane or {}).get(lane)
            if bd is None or day not in bd:
                bd = fx.spend_by_day(home, board, 1, now, lane=lane)
            live_lanes[lane] = bd.get(day, {"usd": 0.0, "cards": {}})
    for r in jsonl(fx.spend_log_path(data)):
        lane = r.get("lane")
        if r.get("date") != day or lane not in lanes:
            continue
        try:
            usd = float(r.get("usd") or 0)
        except (TypeError, ValueError):
            continue
        if lane in live_lanes and "cards" in r and r.get("host", me) == me:
            continue  # this host's card rows are replaced by the live board read
        if lane == "fixer" and "cards" not in r and r.get("host", me) == me and lane in live_lanes:
            continue  # pre-"cards" fixer rows of this host: same thing
        lanes[lane] += usd
        if lane == "trial" and "cards" not in r:
            ev += usd
        for t, u in (r.get("cards") or {}).items():
            cards[t] = round(cards.get(t, 0.0) + float(u or 0), 4)
    for lane, d in live_lanes.items():
        lanes[lane] += d["usd"]
        for t, u in d["cards"].items():
            cards[t] = round(cards.get(t, 0.0) + u, 4)
    caps, total_cap = caps_for(data, day)
    lanes = {k: round(v, 4) for k, v in lanes.items()}
    total = sum(v for k, v in lanes.items() if k not in SEPARATE)
    return {"day": day, "lanes": lanes, "eval": round(ev, 4), "cards": cards, "total": round(total, 4),
            "caps": caps, "total_cap": total_cap, "live": bool(live_lanes)}


def check(snap, lane, extra=0.0):
    """(ok, reason): a lane gate is open while its lane is under its cap AND the engine total is under its cap."""
    cap = snap["caps"].get(lane)
    usd = snap["lanes"].get(lane, 0.0) + extra
    if cap is not None and usd >= cap:
        return False, f"{lane} lane spend today ${usd:.2f} >= ${cap:g} cap"
    if lane in SEPARATE:
        return True, ""
    tot = snap["total"] + extra
    if tot >= snap["total_cap"]:
        return False, f"engine total today ${tot:.2f} >= ${snap['total_cap']:g}/day cap ({lane_line(snap)})"
    return True, ""


def lane_line(snap):
    return ", ".join(f"{k} ${v:.2f}" + (f"/{snap['caps'][k]:g}" if k in snap["caps"] else "")
                     for k, v in snap["lanes"].items())


def status_text(snap):
    lines = [f"engine budget {snap['day']} (UTC day){'' if snap['live'] else ' [ledger only, no live board]'}:"]
    for k, v in snap["lanes"].items():
        cap = snap["caps"].get(k)
        lines.append(f"  {k:<6} ${v:6.2f}" + (f" / ${cap:g}" + ("  OVER" if v >= cap else "") if cap else "  (counts toward total)"))
    lines.append(f"  total  ${snap['total']:6.2f} / ${snap['total_cap']:g}" + ("  OVER" if snap["total"] >= snap["total_cap"] else ""))
    return "\n".join(lines)


# ── watchdog ──

def next_utc_midnight(now):
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (d + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def engine_cards(home, board, statuses=("running",)):
    """[{id, title, lane, status, body, worker_pid}] for coach engine cards in the given statuses."""
    kdb = fx.kanban_db(home, board)
    if not kdb.exists():
        return []
    q = ",".join("?" * len(statuses))
    out = []
    with fx._ro(kdb) as k:
        cols = {r[1] for r in k.execute("pragma table_info(tasks)")}
        pid = "t.worker_pid" if "worker_pid" in cols else "null"
        body = "coalesce(t.body, '')" if "body" in cols else "''"
        for lane in LIVE_LANES:
            for r in k.execute(f"select t.id, t.title, t.status, {body}, {pid} from tasks t "
                               f"where t.status in ({q}) and {fx.LANE_WHERE[lane]}", statuses):
                out.append({"id": r[0], "title": r[1], "status": r[2], "body": r[3], "worker_pid": r[4], "lane": lane})
    return out


def card_cost(home, board, task_id):
    """Lifetime worker-session cost of one card (every run, any day)."""
    kdb = fx.kanban_db(home, board)
    with fx._ro(kdb) as k:
        has_rec = k.execute("select 1 from sqlite_master where name='worker_recovery_sessions'").fetchone()
        rows = k.execute("select r.started_at, w.state_db, w.session_id, t.title, r.ended_at from task_runs r "
                         "join tasks t on t.id = r.task_id "
                         + ("left join worker_recovery_sessions w on w.run_id = r.id " if has_rec else
                            "left join (select null run_id, null state_db, null session_id) w on 0 ")
                         + "where r.task_id = ?", (task_id,)).fetchall()
    conns = {}
    try:
        return round(sum(fx.run_usd(home, conns, s, sdb, sid, title, e) for s, sdb, sid, title, e in rows), 4)
    finally:
        for c in conns.values():
            c.close()


def card_budget(card, snap):
    m = CARD_BUDGET_RX.search(card.get("body") or "")
    return float(m.group(1)) if m else snap["caps"].get(card["lane"], snap["total_cap"])


def decide(home, board, snap, cards=None):
    """[(card, card_usd, card_budget, reason)] for running engine cards that must stop now."""
    stops = []
    for c in (engine_cards(home, board) if cards is None else cards):
        usd = card_cost(home, board, c["id"])
        cap = card_budget(c, snap)
        if usd >= cap:
            stops.append((c, usd, cap, f"card cost ${usd:.2f} >= its ${cap:g} budget"))
            continue
        ok, why = check(snap, c["lane"])
        if not ok:
            stops.append((c, usd, cap, why))
    return stops


def worker_pids(task_id, hint=None):
    """Live pids whose environment carries HERMES_KANBAN_TASK=<task_id> (never anything else)."""
    cands = [hint] if hint else []
    try:
        r = subprocess.run(["pgrep", "-f", "hermes"], capture_output=True, text=True, timeout=10)
        cands += [int(p) for p in r.stdout.split()]
    except Exception:
        pass
    out = []
    for p in dict.fromkeys(x for x in cands if x):
        try:
            proc = Path(f"/proc/{p}/environ")
            env = (proc.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace") if proc.exists() else
                   subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", str(p)], capture_output=True,
                                  text=True, timeout=10).stdout)
        except Exception:
            continue
        if re.search(rf"\bHERMES_KANBAN_TASK={re.escape(task_id)}(\s|$)", env):
            out.append(p)
    return out


def stop_kind(usd, cap):
    """'card' = the card spent its own lifetime budget (never resumes by itself); 'cap' = lane/total cap (next day)."""
    return "card" if usd >= cap else "cap"


def stop_card(hermes, board, card, reason, until, kill=os.kill, pids=worker_pids, run=subprocess.run, kind="cap"):
    """Stop the card, then SIGTERM its worker. Returns a short result string.

    kind 'cap' (lane/total cap): schedule to the next UTC midnight; resume_due unblocks it then.
    kind 'card' (its own lifetime budget): block needs_input for a human. Lifetime cost never drops, so an automatic
    resume would only restart it to be stopped again every day."""
    found = pids(card["id"], card.get("worker_pid"))
    if kind == "card":
        msg = (f"budget: {reason}. Not resumed automatically: raise this card's 'Budget for this card <= $N' line "
               "and unblock it, or close it.")
        r = run([hermes, "kanban", "--board", board, "block", card["id"], "--kind", "needs_input", msg],
                capture_output=True, text=True)
        verb, done = "block", "blocked needs_input (card budget; no auto-resume)"
    else:
        msg = f"SCHEDULED_UNTIL={until} — budget cap reached; resumes next UTC day ({reason})"
        r = run([hermes, "kanban", "--board", board, "schedule", card["id"], msg], capture_output=True, text=True)
        verb, done = "schedule", f"scheduled until {until}"
    if r.returncode:
        return f"{verb} FAILED: {(r.stderr or r.stdout).strip()[-200:]}"
    killed = []
    for p in found:
        try:
            kill(p, signal.SIGTERM)
            killed.append(p)
        except ProcessLookupError:
            pass
    return f"{done}; SIGTERM {killed or 'no live worker'}"


def resume_due(hermes, board, data, home, now, dry=False, run=subprocess.run):
    """Unblock cards this watchdog scheduled on an earlier UTC day for a lane/total cap that are still `scheduled`.

    Card-budget stops (kind 'card'; older rows without kind: card_usd >= card_budget) never auto-resume."""
    day = fx.utc_day(now)
    ids = {r["card"] for r in jsonl(state_dir(data) / "budget-stops.jsonl")
           if r.get("action") == "stop" and r.get("date", day) < day and r.get("card")
           and (r.get("kind") or stop_kind(r.get("card_usd") or 0, r.get("card_budget") or float("inf"))) != "card"}
    ids -= {r["card"] for r in jsonl(state_dir(data) / "budget-stops.jsonl")
            if r.get("action") == "resume" and r.get("card")}
    if not ids:
        return []
    kdb = fx.kanban_db(home, board)
    with fx._ro(kdb) as k:
        q = ",".join("?" * len(ids))
        due = [r[0] for r in k.execute(f"select id from tasks where id in ({q}) and status = 'scheduled'", sorted(ids))]
    out = []
    for tid in due:
        if dry:
            out.append((tid, "would resume"))
            continue
        r = run([hermes, "kanban", "--board", board, "unblock", tid], capture_output=True, text=True)
        out.append((tid, "resumed" if r.returncode == 0 else f"unblock FAILED: {(r.stderr or r.stdout).strip()[-200:]}"))
    return out


def record(data, rows):
    f = state_dir(data) / "budget-stops.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return f


def inbox_post(hermes, snap, stops, data, run=subprocess.run):
    """One Inbox card per UTC day (key engine-budget:<day>); a re-post updates it. Best effort."""
    if os.environ.get("COACH_INBOX", "1") == "0":
        return None
    over = [k for k, v in snap["lanes"].items() if k in snap["caps"] and v >= snap["caps"][k]]
    if snap["total"] >= snap["total_cap"]:
        over.append("total")
    title = f"Engine budget: {', '.join(over) or 'card budget'} cap hit {snap['day']}"
    rows = "\n".join(f"| {k} | ${v:.2f} | " + (f"${snap['caps'][k]:g}" if k in snap["caps"] else "—") + " |"
                     for k, v in snap["lanes"].items())
    stopped = "\n".join(f"- `{c['id']}` {c['title'][:80]} ({c['lane']}, card ${u:.2f}): {why}" for c, u, _b, why in stops)
    body = (f"**Now** the improvement engine is paused for UTC day {snap['day']} (resumes at the next UTC midnight).\n\n"
            f"| lane | today | cap |\n|---|---:|---:|\n{rows}\n| **total** | **${snap['total']:.2f}** | "
            f"**${snap['total_cap']:g}** |\n\n" + (f"**Stopped cards**\n{stopped}\n\n" if stopped else "")
            + "Cards stopped on a lane/total cap are scheduled, not lost: the watchdog unblocks them on the next UTC day. "
            "Cards that spent their own budget are blocked for you: raise the card's 'Budget for this card' line and "
            "unblock it, or close it.")
    prompt = (f"The improvement engine hit its budget cap today (UTC day {snap['day']}): {lane_line(snap)}, total "
              f"${snap['total']:.2f}/${snap['total_cap']:g}. Raise the cap for today only: ask me how much (default: "
              f"total ${snap['total_cap'] + 10:g}), then append one JSON row per raised lane, e.g. "
              f"{{\"date\": \"{snap['day']}\", \"lane\": \"total\", \"usd\": {snap['total_cap'] + 10:g}, \"why\": \"...\"}} "
              f"to {state_dir(data)}/budget-overrides.jsonl, commit + push the data repo, then run "
              "`python3 ~/code/hermes-mods/skills/productivity/hermes-coach/scripts/engine_budget.py status` and unblock "
              "today's stopped cards listed in research/state/budget-stops.jsonl (`hermes kanban --board hermes unblock "
              "<id>`). Report the new caps.")
    base = [hermes, "inbox", "post", title, "--body", body, "--prompt", prompt, "--key", f"engine-budget:{snap['day']}",
            "--poster", "coach · engine budget", "--cwd", str(data)]
    label = ["--prompt-label", "Raise today's engine cap"]
    try:
        r = None
        # richest first; an older inbox plugin lacks --type / --prompt-label, and a host without the type installed
        # rejects it: any failure falls back to the plainer card
        for extra in (["--type", "engine-budget", "--field", f"day={snap['day']}"] + label, label, []):
            r = run(base + extra, capture_output=True, text=True)
            if r.returncode == 0:
                break
        return r.returncode == 0
    except Exception:
        return None


def card_decided(hermes, day, run=subprocess.run):
    """True when David pressed Leave paused on today's engine-budget card: don't re-post it (a decision card re-opens
    as a new card on a re-post, which would nag after he said 'leave'). Raise cap is not final: if the raised cap is
    hit again the state key changes and a new card is posted."""
    try:
        r = run([hermes, "inbox", "outcome", f"engine-budget:{day}"], capture_output=True, text=True)
        o = json.loads(r.stdout or "{}")
    except Exception:
        return False
    return bool(o.get("found")) and o.get("state") == "resolved" and o.get("outcome") == "leave"


def _seen_path(home):
    return Path(home) / "cache" / "engine-budget" / "posted.json"


def watchdog(home=None, board=None, data=None, hermes=None, now=None, dry=False, out=print, cards=None,
             kill=os.kill, pids=worker_pids, run=subprocess.run):
    """One pass. Returns {"snap", "stops": [(card, usd, budget, reason, result)], "resumed": [...], "changed": bool}."""
    now = now or time.time()
    home = Path(home or os.environ.get("HERMES_HOME") or HOME / ".hermes")
    board = board or os.environ.get("COACH_KANBAN_BOARD") or "hermes"
    data = Path(data or os.environ.get("COACH_DATA") or HOME / "code/hermes-coach-data")
    hermes = hermes or os.environ.get("COACH_HERMES") or shutil.which("hermes") or str(HOME / ".local/bin/hermes")
    snap = snapshot(home, board, data, now)
    out(status_text(snap))
    resumed = resume_due(hermes, board, data, home, now, dry, run)
    for tid, res in resumed:
        out(f"  resume {tid}: {res}")
    todo = decide(home, board, snap, cards)
    until = next_utc_midnight(now)
    stops, rows = [], []
    for c, usd, cap, why in todo:
        kind = stop_kind(usd, cap)
        res = "dry-run: would stop" if dry else stop_card(hermes, board, c, why, until, kill, pids, run, kind)
        out(f"  stop {c['id']} [{c['lane']}] {c['title'][:70]}: {why} -> {res}")
        stops.append((c, usd, cap, why, res))
        rows.append({"date": snap["day"], "at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds"),
                     "action": "stop", "kind": kind, "card": c["id"], "lane": c["lane"], "card_usd": usd, "card_budget": cap,
                     "lane_usd": snap["lanes"].get(c["lane"]), "total_usd": snap["total"], "reason": why,
                     "result": res, "host": host()})
    rows += [{"date": snap["day"], "at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds"),
              "action": "resume", "card": tid, "result": res, "host": host()} for tid, res in resumed if res == "resumed"]
    if not todo:
        out("  no engine card to stop" + (f" ({len(engine_cards(home, board))} running)" if cards is None else ""))
    over = any(snap["lanes"][k] >= snap["caps"][k] for k in snap["caps"]) or snap["total"] >= snap["total_cap"]
    if not dry:
        if rows:
            record(data, rows)
        seen = _seen_path(home)
        key = json.dumps([snap["day"], sorted(k for k in snap["caps"] if snap["lanes"][k] >= snap["caps"][k]),
                          snap["total"] >= snap["total_cap"], sorted(c["id"] for c, *_ in todo)])
        prev = seen.read_text() if seen.exists() else ""
        if (over or todo) and key != prev and card_decided(hermes, snap["day"], run):
            out(f"  inbox engine-budget:{snap['day']}: already decided, not re-posted")
            seen.parent.mkdir(parents=True, exist_ok=True)
            seen.write_text(key)
        elif (over or todo) and key != prev:
            ok = inbox_post(hermes, snap, [(c, u, b, w) for c, u, b, w, _r in stops], data, run)
            out(f"  inbox engine-budget:{snap['day']}: {'posted' if ok else 'post failed'}")
            if ok:
                seen.parent.mkdir(parents=True, exist_ok=True)
                seen.write_text(key)
    elif over or todo:
        out(f"  dry-run: would post inbox engine-budget:{snap['day']}")
    return {"snap": snap, "stops": stops, "resumed": resumed, "changed": bool(rows)}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="engine_budget.py", description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=os.environ.get("COACH_DATA") or str(HOME / "code/hermes-coach-data"))
    ap.add_argument("--home", default=os.environ.get("HERMES_HOME") or str(HOME / ".hermes"))
    ap.add_argument("--board", default=os.environ.get("COACH_KANBAN_BOARD") or "hermes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    w = sub.add_parser("watchdog")
    w.add_argument("--dry-run", action="store_true")
    w.add_argument("--no-push", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        snap = snapshot(a.home, a.board, a.data)
        print(json.dumps(snap, indent=1) if a.json else status_text(snap))
        return 0 if all(check(snap, k)[0] for k in snap["caps"]) else 1
    print(f"[engine-budget watchdog {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}]")
    pushing = not (a.dry_run or a.no_push) and (Path(a.data) / ".git").exists()
    if pushing:
        subprocess.run(["git", "pull", "-q", "--rebase", "--autostash"], cwd=a.data, capture_output=True)
    res = watchdog(a.home, a.board, a.data, dry=a.dry_run)
    if pushing:
        # keep the shared ledger fresh for the other hosts' gates (scout on nous-1): this host's card spend rows
        snap = res["snap"]
        logged = {ln: fx.logged_spend(a.data, snap["day"], ln) for ln in ("fixer", "trial")}
        if res["changed"] or abs(logged["fixer"] - snap["lanes"]["fixer"]) >= 0.25 or \
                abs(logged["trial"] - snap["lanes"]["trial"]) >= 0.25:
            for ln in ("fixer", "trial"):
                fx.log_spend(a.data, fx.spend_by_day(a.home, a.board, 2, lane=ln), lane=ln)
            paths = [p for p in ("research/state/spend.jsonl", "research/state/budget-stops.jsonl")
                     if (Path(a.data) / p).exists()]
            subprocess.run(["git", "add", "--", *paths], cwd=a.data, capture_output=True)
            if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=a.data).returncode:
                subprocess.run(["git", "commit", "-q", "-m", f"engine-budget: watchdog {snap['day']}", "--", *paths],
                               cwd=a.data, capture_output=True)
                for _ in range(3):
                    if subprocess.run(["git", "push", "-q"], cwd=a.data, capture_output=True).returncode == 0:
                        break
                    subprocess.run(["git", "pull", "-q", "--rebase", "--autostash"], cwd=a.data, capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
