#!/usr/bin/env python3
"""Weekly coach digest context (stdlib only): KPI deltas, top findings, config-change checks, proposal state.

    coach_weekly.py --data DIR [--days 7] [--check "agent.max_turns=150@2026-10-14T05:39Z#synthetic_turn_cap"]

Prints a Markdown block the coach Bot turns into reports/weekly-<date>.md:
  1. The 6 plan KPIs (+ secondary ones): latest reports/kpis.jsonl row vs the row closest to --days earlier.
     Keys missing from older rows (e.g. kanban_share, added later) are recomputed from the digests for that window.
     KPI 4 (coaching precision) = applied / (applied + rejected) proposals; KPI 5 (adoption) = sessions using
     each feature a curriculum proposal taught, since it was applied.
  2. Top findings of the window from findings/*.jsonl, ranked by estimated waste, as `host/profile/id — title`.
  2b. Spend vs output per agent (host/profile x lane: each cron job, Kanban, chats, ...): $ this vs previous window,
     no-op share (empty / [SILENT] / 'nothing to fix' last replies), flags for mostly-no-op agents, crons repeated
     across hosts, and spend that doubled.
  2c. Self-heal lane: defects filed, proposed, approved, fixed, reverted, closed as not-a-defect; recurrence; $ per
     fix (selfheal_* coach events + `selfheal:` Kanban cards, or the hub's reports/selfheal-lane.jsonl snapshot).
  3. Each --check: before/after comparison for a config change at a timestamp (sessions fully before vs started
     after, equal-length windows), with a keep / raise / revert recommendation.
  4. Pending and rejected proposals (the Bot must not re-propose rejected ones).
"""
import argparse, collections, contextlib, glob, json, re, statistics, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coach_mine  # noqa: E402
import coach_proposals  # noqa: E402
import coach_yield  # noqa: E402

PLAN_KPIS = [  # (n, key, label, goal, better)
    (1, "repeat_fix_rate", "Repeat-fix rate", "halve", "down"),
    (2, "msgs_p90", "p90 session size (msgs)", "<200", "down"),
    (3, "hermes_meta_cost_share", "Hermes-meta share of spend", "<0.15", "down"),
    (4, "coaching_precision", "Coaching precision (proposals accepted)", ">=0.40", "up"),
    (5, "feature_adoption", "Feature adoption (taught features used >=3x in 2 wk)", "all taught", "up"),
    (6, "kanban_share", "Kanban share of delegated spend", ">0.50", "up"),
]
SECONDARY = ["sessions", "cost_usd", "msgs_p50", "over_300_msgs", "stuck_rate", "tool_error_rate",
             "chat_orchestration_sessions", "kanban_creates_from_chat", "subagents_per_session"]


def parse_ts(s):
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def act(d):
    return d.get("last_activity_at") or d.get("started_at") or 0


def kpi_rows(data):
    f = data / "reports" / "kpis.jsonl"
    rows = []
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def window_kpis(ds, lo, hi):
    return coach_mine.kpis([d for d in ds if lo <= act(d) < hi])


def proposal_kpis(data, ds, now):
    st = coach_proposals.state(data).values()
    acc = sum(1 for p in st if p.get("status") == "applied")
    rej = sum(1 for p in st if p.get("status") == "rejected")
    precision = round(acc / (acc + rej), 3) if acc + rej else None
    adoption, detail = None, []
    taught = [p for p in st if p.get("status") == "applied" and p.get("kind") == "curriculum" and p.get("feature")]
    if taught:
        ok = 0
        for p in taught:
            t0 = parse_ts(p["at"])
            uses = sum((d.get("tools") or {}).get(p["feature"], 0) for d in ds if t0 <= act(d) < t0 + 14 * 86400)
            ok += uses >= 3
            detail.append(f"{p['feature']}: {uses} uses since {p['at'][:10]}")
        adoption = round(ok / len(taught), 3)
    return {"coaching_precision": precision, "feature_adoption": adoption}, (acc, rej), detail


def fmt(v):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if abs(v) < 10 else f"{v:,.1f}"
    return str(v)


def delta(cur, prev, better):
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
        return "–"
    d = cur - prev
    if abs(d) < 1e-9:
        return "0 (flat)"
    good = (d < 0) == (better == "down")
    pct = f" ({d / prev:+.0%})" if prev else ""
    return f"{d:+.3g}{pct} {'better' if good else 'worse'}"


def section_kpis(data, ds, now, days):
    rows = kpi_rows(data)
    cur_row = rows[-1] if rows else None
    target = now - days * 86400
    base_row = None
    if rows:
        older = [r for r in rows if parse_ts(r["date"]) <= target + 43200]
        base_row = older[-1] if older else None
    cur = dict(cur_row["overall"]) if cur_row else window_kpis(ds, now - days * 86400, now + 1)
    cur_src = f"kpis.jsonl {cur_row['date']}" if cur_row else "computed now"
    if base_row:
        prev, prev_src = dict(base_row["overall"]), f"kpis.jsonl {base_row['date']}"
    else:
        prev, prev_src = {}, "no row ≥7 days old yet"
    live_cur = window_kpis(ds, now - days * 86400, now + 1)
    live_prev = window_kpis(ds, now - 2 * days * 86400, now - days * 86400)
    filled = []
    for k, v in live_cur.items():
        if k not in cur:
            cur[k] = v
            filled.append(f"{k} (this window)")
    for k, v in live_prev.items():
        if k not in prev and (base_row or live_prev.get("sessions", 0) >= 10):
            prev[k] = v
            if base_row:
                filled.append(f"{k} (baseline)")
    if not base_row and live_prev.get("sessions", 0) < 10:
        prev = {}  # too little history for a baseline (e.g. the digests start inside the previous window)
        prev_src = f"none: only {live_prev.get('sessions', 0)} sessions {days:g}–{2 * days:g} days ago"
    elif not base_row:
        prev_src = f"recomputed from digests, {days:g}–{2 * days:g} days ago ({live_prev.get('sessions', 0)} sessions)"
    pk, (acc, rej), adoption_detail = proposal_kpis(data, ds, now)
    cur.update(pk)
    out = [f"## KPI deltas (current: {cur_src}; baseline: {prev_src})", "",
           "| # | KPI | now | baseline | delta | goal |", "|---|---|---|---|---|---|"]
    for n, k, label, goal, better in PLAN_KPIS:
        out.append(f"| {n} | {label} (`{k}`) | {fmt(cur.get(k))} | {fmt(prev.get(k))} | "
                   f"{delta(cur.get(k), prev.get(k), better)} | {goal} |")
    out += ["", "Secondary: " + ", ".join(f"{k} {fmt(cur.get(k))} (was {fmt(prev.get(k))})" for k in SECONDARY), ""]
    out.append(f"KPI 4 basis: {acc} applied, {rej} rejected proposals"
               + (" (no decisions yet)" if not acc + rej else "")
               + ". The Phase 2 nudge log is not built yet, so proposal acceptance stands in.")
    out.append("KPI 5 basis: " + ("; ".join(adoption_detail) if adoption_detail else
                                  "no applied curriculum proposal with a `feature` yet (Phase 3 not built)."))
    if filled:
        out.append("Filled from digests (missing in kpis.jsonl): " + ", ".join(filled) + ".")
    return out


def section_findings(data, ds, now, days, top):
    by_id = {d["id"]: d for d in ds}
    rows = []
    for f in sorted(glob.glob(str(data / "findings" / "*.jsonl"))):
        day = Path(f).stem
        try:
            if parse_ts(day + "T00:00:00Z") < now - (days + 1) * 86400:
                continue
        except Exception:
            continue
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("pattern") in (None, "none"):
                continue
            rows.append(r)
    pats = collections.defaultdict(list)
    for r in rows:
        pats[r["pattern"]].append(r)
    ranked = sorted(pats.items(), key=lambda kv: -sum(float(x.get("waste_usd_est") or 0) for x in kv[1]))
    out = [f"## Findings this window ({len(rows)} findings, {len(pats)} patterns), ranked by est. waste", ""]
    for pat, rs in ranked[:top]:
        w = sum(float(x.get("waste_usd_est") or 0) for x in rs)
        out.append(f"### {pat} — ~${w:.0f} est., {len(rs)} findings")
        for r in sorted(rs, key=lambda x: -float(x.get("waste_usd_est") or 0))[:4]:
            d = by_id.get(r["session_id"], {})
            host, prof = r.get("host") or d.get("host", "?"), r.get("profile") or d.get("profile", "?")
            title = r.get("title") or d.get("title") or "(untitled)"
            out.append(f"- {host}/{prof}/{r['session_id']} — {title}: {r.get('evidence', '')[:300]}"
                       f" → fix ({r.get('fix_kind', '?')}): {r.get('fix', '')[:200]}")
        out.append("")
    if len(ranked) > top:
        out.append("Other patterns: " + ", ".join(f"{p} ({len(rs)})" for p, rs in ranked[top:]))
    if not rows:
        out.append("No findings in this window.")
    return out


NOOP_RE = re.compile(r"^\s*\[SILENT\]|^\s*$|\b(nothing (new |to )?(fix|report|do|change)|no action( needed)?|"
                     r"no changes?|all (clear|healthy)|healthy, no action|self-managing)\b", re.I)
INTERACTIVE = {"desktop", "cli", "tui", "slack", "discord", "telegram", "whatsapp", "signal", "imessage"}


def agent_key(d):
    """(host/profile, lane): one cron job, the profile's Kanban workers, its chats, or another session source."""
    src = d.get("source") or "?"
    sid = d.get("id") or ""
    if src == "cron" or sid.startswith("cron_"):
        job = sid.split("_")[1] if sid.count("_") >= 2 else "?"
        name = (d.get("title") or "").split(" · ")[0].strip() or job
        lane = f"cron:{name} ({job})"
    elif src in INTERACTIVE:
        lane = "chats"
    else:
        lane = src
    return f"{d.get('host', '?')}/{d.get('profile', '?')}", lane


def is_noop(d):
    return bool(NOOP_RE.search((d.get("last_reply") or "")[:200]))


def section_spend(ds, now, days, top=15, min_cost=5.0, noop_flag=0.5):
    """Spend vs output per agent: cost this window vs the previous one, and the share of runs that produced nothing
    (an empty / [SILENT] / 'nothing to fix' last reply). Flags mostly-no-op agents and cron jobs repeated across hosts."""
    lo, plo = now - days * 86400, now - 2 * days * 86400
    cur, prev = collections.defaultdict(list), collections.defaultdict(float)
    for d in ds:
        t, c = act(d), float(d.get("cost_usd") or 0)
        if lo <= t < now:
            cur[agent_key(d)].append(d)
        elif plo <= t < lo:
            prev[agent_key(d)] += c
    rows = []
    for k, v in cur.items():
        cost = sum(float(d.get("cost_usd") or 0) for d in v)
        noop = sum(is_noop(d) for d in v) / len(v)
        stuck = sum(1 for d in v if (d.get("signals") or {}).get("stuck")) / len(v)
        rows.append({"key": k, "runs": len(v), "cost": cost, "prev": prev.get(k, 0.0), "noop": noop, "stuck": stuck})
    rows.sort(key=lambda r: -r["cost"])
    total = sum(r["cost"] for r in rows) or 1.0
    out = [f"## Spend vs output by agent (last {days:g} days; {len(rows)} agents, ${total:,.0f})", "",
           "No-op = runs whose last reply is empty, [SILENT] or 'nothing to fix/report'. "
           "Flag one of these per week as a cut / merge / lower-frequency proposal.", "",
           "| Agent | Lane | Runs | $ | $ prev | $/run | No-op | Stuck | Flag |",
           "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    by_cron = collections.defaultdict(set)
    for r in rows:
        if r["key"][1].startswith("cron:"):
            by_cron[r["key"][1].split(" (")[0]].add(r["key"][0])
    flagged = []
    for r in rows:
        (agent, lane), flag = r["key"], []
        if r["cost"] >= min_cost and r["noop"] >= noop_flag:
            flag.append(f"mostly no-op: ~${r['cost'] * r['noop']:,.0f} spent on nothing")
        if lane.startswith("cron:") and len(by_cron[lane.split(" (")[0]]) > 1:
            flag.append("same cron on " + ", ".join(sorted(by_cron[lane.split(" (")[0]])))
        if r["prev"] >= min_cost and r["cost"] >= 2 * r["prev"]:
            flag.append(f"spend x{r['cost'] / r['prev']:.1f} vs prev")
        r["flag"] = "; ".join(flag)
        if flag:
            flagged.append(r)
    shown = rows[:top] + [r for r in flagged if r not in rows[:top]]
    for r in shown:
        agent, lane = r["key"]
        out.append(f"| {agent} | {lane} | {r['runs']} | {r['cost']:,.2f} | {r['prev']:,.2f} | "
                   f"{r['cost'] / r['runs']:,.2f} | {r['noop']:.0%} | {r['stuck']:.0%} | {r['flag']} |")
    rest = [r for r in rows if r not in shown]
    if rest:
        out.append(f"\n{len(rest)} more agents, ${sum(r['cost'] for r in rest):,.2f} together.")
    if not rows:
        out.append("No sessions in this window.")
    return out


def check_change(ds, spec, now):
    """spec = key=value@ISO-UTC[#signal]. Sessions fully before vs started after, equal windows."""
    head, _, rest = spec.partition("@")
    when, _, signal = rest.partition("#")
    signal = signal or "synthetic_turn_cap"
    t = parse_ts(when)
    span = now - t  # after: everything since the change; before: the 7 days up to it (rates, not totals, compare)
    before = [d for d in ds if t - 7 * 86400 <= (d.get("started_at") or 0) and act(d) < t and d.get("msgs")]
    after = [d for d in ds if (d.get("started_at") or 0) >= t and d.get("msgs")]

    def stats(xs):
        n = len(xs)
        if not n:
            return {"n": 0}
        costs = [coach_mine.total_cost(d) for d in xs]
        hits = [(d.get("signals") or {}).get(signal, 0) for d in xs]
        sizes = sorted(d["msgs"] for d in xs)
        return {"n": n, "hits": sum(hits), "hits_per_session": round(sum(hits) / n, 3),
                "sessions_hit": sum(1 for h in hits if h), "share_hit": round(sum(1 for h in hits if h) / n, 3),
                "cost_mean": round(statistics.mean(costs), 2), "cost_median": round(statistics.median(costs), 2),
                "msgs_p90": sizes[min(n - 1, int(round(0.9 * (n - 1))))]}

    b, a = stats(before), stats(after)
    hours = span / 3600
    if a["n"] < 15 or hours < 72:
        rec = (f"KEEP (provisional): only {a['n']} sessions / {hours:.0f} h since the change; need ≥15 sessions "
               "and ≥3 days to judge. Re-check next week.")
    else:
        hit_drop = 1 - (a["hits_per_session"] / b["hits_per_session"]) if b.get("hits_per_session") else 0
        cost_rise = a["cost_mean"] / b["cost_mean"] - 1 if b.get("cost_mean") else 0
        if cost_rise > 0.25 and hit_drop < 0.3:
            rec = f"REVERT: cost/session up {cost_rise:+.0%} while cap hits/session only fell {hit_drop:.0%}."
        elif a["share_hit"] > 0.05 and cost_rise <= 0.25:
            rec = (f"RAISE: {a['share_hit']:.0%} of sessions still hit the cap (cost/session {cost_rise:+.0%}); "
                   "try the next step up.")
        else:
            rec = f"KEEP: cap hits/session {-hit_drop:+.0%}, cost/session {cost_rise:+.0%}."
    out = [f"### Check: `{head}` since {datetime.fromtimestamp(t, timezone.utc):%Y-%m-%d %H:%M} UTC (signal `{signal}`)",
           f"Windows: 7 days before (sessions that ended before the change) vs {hours:.0f} h after "
           "(sessions started after it; sessions spanning the change are left out). All sources.", "",
           "| | sessions | cap hits | hits/session | sessions hitting | cost/session mean | median | msgs p90 |",
           "|---|---|---|---|---|---|---|---|"]
    for name, s in (("before", b), ("after", a)):
        out.append(f"| {name} | {s['n']} | {s.get('hits', '–')} | {s.get('hits_per_session', '–')} | "
                   f"{s.get('sessions_hit', '–')} ({fmt(s.get('share_hit'))}) | ${s.get('cost_mean', '–')} | "
                   f"${s.get('cost_median', '–')} | {s.get('msgs_p90', '–')} |")
    out += ["", f"Recommendation: {rec}"]
    return out


def section_proposals(data, now=None, days=7):
    """Proposal state by rollout tier. Every auto/canary apply of the last `days` + 1 is listed with its revert."""
    now = now or time.time()
    st = list(coach_proposals.state(data).values())
    by = lambda *s: [p for p in st if p.get("status") in s]  # noqa: E731

    def recent(p):
        t = parse_ts(p.get("at", "")) if p.get("at") else 0
        return t and now - t <= (days + 1) * 86400

    out = ["## Proposal state", "",
           "Tiers: auto = applied by `coach-apply auto` once its gate passes; canary = MacBook first + 7-day check, "
           "promoted to every host on KEEP; review = a Kanban card. Emit `tier` only to RAISE the default.", ""]
    pend = by("pending")
    out += [f"Pending ({len(pend)}):"]
    out += [f"- {p['id']} [{p.get('tier')}] {p['kind']} {p['target']}: {p.get('title', '')}" for p in pend] or ["- none"]
    rec = [p for p in by("applied", "canary") if recent(p)]
    out += ["", f"Applied in the last {days:g} days, each with its one-line revert ({len(rec)}):"]
    out += [f"- {p['id']} [{p.get('tier')}{', canary on the MacBook' if p['status'] == 'canary' else ''}] "
            f"{p.get('title') or p['kind'] + ' ' + p['target']} ({p.get('at', '')[:10]}) — "
            + (f"revert: `coach-apply revert {p['id']}`" if revertible(p) else "applied by hand, revert by hand")
            for p in rec] or ["- none"]
    can = by("canary")
    if can:
        out += ["", "Canary checks in flight (verdict decides promote/revert):"]
        out += [f"- {p['id']} `{p.get('check')}`" for p in can]
    card = by("carded")
    out += ["", f"Review tier, on the Kanban board ({len(card)}):"]
    out += [f"- {p['id']} card {p.get('card')}: {p.get('title', '')}" for p in card] or ["- none"]
    gone = [p for p in by("dropped", "reverted") if recent(p)]
    if gone:
        out += ["", "Dropped / reverted this week (do not re-propose unchanged):"]
        out += [f"- {p['id']} {p['status']}: {p.get('note') or p.get('result') or ''}"[:300] for p in gone]
    app = by("applied")
    out += ["", f"Applied, all time ({len(app)}):"]
    out += [f"- {p['id']} {p['kind']} {p['target']} ({p.get('at', '')[:10]})" for p in app] or ["- none"]
    rej = by("rejected")
    out += ["", f"REJECTED — never propose these again ({len(rej)}):"]
    out += [f"- {p['kind']} {p['target']}: {json.dumps(p['change'], ensure_ascii=False)[:160]}"
            f"{' — ' + p['note'] if p.get('note') else ''}" for p in rej] or ["- none"]
    return out


def revertible(p):
    return any(p.get(k) for k in ("commit", "shared_commit", "backup")) or "prev" in p


# ---- Self-heal lane ---------------------------------------------------------------------------------------------
# Sources (all optional; the section reads "no activity" when there are none):
#   events  `selfheal_*` rows in <home>/coach/log.jsonl (and <home>/profiles/*/coach/log.jsonl), written by
#           plugins/coach/self_heal.py and its lane. Vocabulary (one row per transition, `fingerprint` required,
#           `card` / `component` / `recurrence` / `cost_usd` optional):
#             selfheal_detect, selfheal_would_file  shadow mode: what it would have filed
#             selfheal_filed        investigation card created     selfheal_proposed  Inbox proposal posted
#             selfheal_approved     Do it pressed, fix card started selfheal_fixed     fix verified live ("Fixed: …")
#             selfheal_reverted     fix undone                      selfheal_not_defect closed as not a defect
#             selfheal_declined     Not now / Never                 selfheal_recurrence a fixed fingerprint came back
#   cards   Kanban tasks whose idempotency key starts with `selfheal` and ends in a hashed defect fingerprint
#           (`selfheal:<fp>` investigation, `selfheal:<fp>:fix` or `selfheal-fix:<fp>` fix card) on every board of
#           the home: cost = lifetime worker-session cost.
#   snapshot  <data>/reports/selfheal-lane.jsonl, written by `--selfheal-export` on the hub, so a host without the
#           lane's logs and boards (the cloud coach Bot) still sees it. Local rows win over snapshot rows.
SELFHEAL_STAGES = [("filed", "Filed (investigation card)"), ("proposed", "Proposed (Inbox, What happened / Why / Plan)"),
                   ("approved", "Approved (Do it)"), ("fixed", "Fixed (verified live)"), ("reverted", "Reverted"),
                   ("not_defect", "Closed as not-a-defect (false positive)"), ("declined", "Declined (Not now / Never)")]
SELFHEAL_EVENT_KEYS = ("ts", "event", "fingerprint", "card", "component", "recurrence", "cost_usd", "host", "board")
NOT_DEFECT_RX = re.compile(r"\bnot[ -]a[ -]defect\b|\bno defect\b|\bfalse positive\b", re.I)
# A defect fingerprint ends in a hash (plugins/coach/self_heal.fingerprint: `<component>-<10 hex>`). Cards keyed
# `selfheal:` without one (e.g. the lane's own numbered build cards, `selfheal:1-detector`) are not defects.
DEFECT_FP_RX = re.compile(r"-[0-9a-f]{8,}$")


def selfheal_fp(key):
    """Fingerprint of a `selfheal:` idempotency key (investigation and fix cards share it)."""
    k = re.sub(r"^selfheal(?:-fix)?:", "", key or "")
    return re.sub(r"(?::r\d+)?(?::fix)?$", "", k)  # `:r<n>` = a recurrence's investigation (self_heal.file_defect)


def selfheal_homes(homes=None):
    import os
    if homes:
        return [Path(h).expanduser() for h in homes]
    return [Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")]


def selfheal_events(homes):
    out = []
    for home in homes:
        for p in [home / "coach" / "log.jsonl", *sorted(home.glob("profiles/*/coach/log.jsonl"))]:
            if not p.exists():
                continue
            for line in p.read_text(errors="replace").splitlines():
                if '"selfheal_' not in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and str(r.get("event", "")).startswith("selfheal_"):
                    out.append({k: r[k] for k in SELFHEAL_EVENT_KEYS if k in r})
    return out


def selfheal_cards(homes, cost=True):
    """[{id, key, fingerprint, board, status, created_at, completed_at, fix, not_defect, cost_usd}] across boards."""
    try:
        import coach_fixer as fx  # stdlib-only sibling; a host without it (cloud Bot) uses the snapshot
    except ImportError:
        return []
    out = []
    for home in homes:
        dbs = [("default", home / "kanban.db")] + [(p.parent.name, p) for p in sorted(home.glob("kanban/boards/*/kanban.db"))]
        seen = set()
        for board, db in dbs:
            if not db.exists() or db.resolve() in seen:
                continue
            seen.add(db.resolve())
            try:
                with contextlib.closing(fx._ro(db)) as k:
                    cols = {r[1] for r in k.execute("pragma table_info(tasks)")}
                    if "idempotency_key" not in cols:
                        continue
                    rows = k.execute("select id, idempotency_key, status, created_at, completed_at, coalesce(result, '') "
                                     "from tasks where idempotency_key like 'selfheal%'").fetchall()
            except Exception:
                continue
            for tid, key, status, c_at, done_at, result in rows:
                if not DEFECT_FP_RX.search(selfheal_fp(key)):
                    continue
                usd = 0.0
                if cost:
                    try:
                        import engine_budget
                        usd = engine_budget.card_cost(home, board, tid)
                    except Exception:
                        usd = 0.0
                out.append({"id": tid, "key": key, "fingerprint": selfheal_fp(key), "board": board, "status": status,
                            "created_at": c_at, "completed_at": done_at,
                            "fix": key.endswith(":fix") or key.startswith("selfheal-fix:"),
                            "not_defect": bool(NOT_DEFECT_RX.search(result or "")), "cost_usd": usd})
    return out


def selfheal_snapshot(data):
    p = Path(data) / "reports" / "selfheal-lane.jsonl"
    ev, cards = [], []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            (cards if r.pop("type", "event") == "card" else ev).append(r)
    return ev, cards


def selfheal_load(data, homes=None):
    homes = selfheal_homes(homes)
    ev, cards = selfheal_events(homes), selfheal_cards(homes)
    sev, scards = selfheal_snapshot(data)
    seen_ev = {(r.get("ts"), r.get("event"), r.get("fingerprint")) for r in ev}
    ev += [r for r in sev if (r.get("ts"), r.get("event"), r.get("fingerprint")) not in seen_ev]
    ids = {c["id"] for c in cards}
    cards += [c for c in scards if c.get("id") not in ids]
    return ev, cards


def selfheal_export(path, homes=None):
    """Write the lane's events + cards (content-free fields only) for hosts that cannot read them directly."""
    homes = selfheal_homes(homes)
    rows = [{"type": "event", **r} for r in selfheal_events(homes)] + [{"type": "card", **c} for c in selfheal_cards(homes)]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    tmp.replace(p)
    return len(rows)


def selfheal_stats(events, cards, lo, hi):
    """Per-stage distinct fingerprints in [lo, hi), recurrence, shadow would-files, $ and $ per fix."""
    stage = {s: set() for s, _ in SELFHEAL_STAGES}
    shadow, recur = set(), set()
    fixed_before = {}  # fingerprint -> first time it was fixed (any time), for recurrence
    for r in events:
        if r.get("event") == "selfheal_fixed" and r.get("fingerprint"):
            fp = r["fingerprint"]
            fixed_before[fp] = min(fixed_before.get(fp, r.get("ts") or 0), r.get("ts") or 0)
    for c in cards:
        if c.get("fix") and c.get("status") == "done" and c.get("completed_at"):
            fp = c["fingerprint"]
            fixed_before[fp] = min(fixed_before.get(fp, c["completed_at"]), c["completed_at"])
    usd = 0.0
    for r in events:
        t, fp = r.get("ts") or 0, r.get("fingerprint") or r.get("card") or "?"
        if not lo <= t < hi:
            continue
        ev = r.get("event", "")[len("selfheal_"):]
        if ev in ("detect", "would_file"):
            shadow.add(fp)
        elif ev == "recurrence":
            recur.add(fp)
            stage["filed"].add(fp)
        elif ev in stage:
            stage[ev].add(fp)
        if ev == "filed" and (r.get("recurrence") or (fp in fixed_before and fixed_before[fp] < t)):
            recur.add(fp)
        usd += float(r.get("cost_usd") or 0)
    for c in cards:
        fp, t = c["fingerprint"], c.get("created_at") or 0
        if lo <= t < hi:
            usd += float(c.get("cost_usd") or 0)
            if c.get("fix"):
                stage["approved"].add(fp)
            else:
                stage["filed"].add(fp)
                if fp in fixed_before and fixed_before[fp] < t:
                    recur.add(fp)
        done = c.get("completed_at") or 0
        if lo <= done < hi and c.get("status") == "done":
            if c.get("not_defect"):
                stage["not_defect"].add(fp)
            elif c.get("fix"):
                stage["fixed"].add(fp)
    n = {s: len(v) for s, v in stage.items()}
    filed = n["filed"]
    return {**n, "shadow": len(shadow), "recurrence": len(recur),
            "recurrence_rate": round(len(recur) / filed, 3) if filed else None,
            "usd": round(usd, 2), "usd_per_fix": round(usd / n["fixed"], 2) if n["fixed"] else None,
            "false_positive_rate": round(n["not_defect"] / filed, 3) if filed else None}


def section_selfheal(data, now, days, homes=None, events=None, cards=None):
    if events is None or cards is None:
        try:
            ev, cs = selfheal_load(data, homes)
        except Exception as e:
            return [f"## Self-heal lane (last {days:g} days)", "", f"Could not read the lane: {type(e).__name__}: {e}"]
        events = ev if events is None else events
        cards = cs if cards is None else cards
    cur = selfheal_stats(events, cards, now - days * 86400, now + 1)
    prev = selfheal_stats(events, cards, now - 2 * days * 86400, now - days * 86400)
    out = [f"## Self-heal lane (last {days:g} days)", "",
           "Defects Hermes filed and fixed without being asked (`selfheal_*` coach events + `selfheal:` cards). "
           "Counts are distinct defect fingerprints. Pair with the ask-gate KPI `had_to_ask_fix` (target 0).", ""]
    if not events and not cards:
        return out + ["No self-heal activity yet: no `selfheal_*` events and no `selfheal:` cards on this host or in "
                      "reports/selfheal-lane.jsonl (the lane is not live, or nothing was detected)."]
    out += ["| Stage | this window | previous |", "|---|---:|---:|"]
    for s, label in SELFHEAL_STAGES:
        out.append(f"| {label} | {cur[s]} | {prev[s]} |")
    out.append(f"| Shadow would-file (not created) | {cur['shadow']} | {prev['shadow']} |")
    out += ["",
            f"Recurrence: {cur['recurrence']} of {cur['filed']} filed came back after a fix "
            f"({fmt(cur['recurrence_rate'])}; previous {fmt(prev['recurrence_rate'])}). Each needs a detection-level guard.",
            f"False-positive rate (not-a-defect / filed): {fmt(cur['false_positive_rate'])} "
            f"(previous {fmt(prev['false_positive_rate'])}).",
            f"Spend: ${cur['usd']:,.2f} (previous ${prev['usd']:,.2f}); $ per fix: "
            + (f"${cur['usd_per_fix']:,.2f}" if cur["usd_per_fix"] is not None else "n/a (no fixes this window)")
            + (f" (previous ${prev['usd_per_fix']:,.2f})" if prev["usd_per_fix"] is not None else "") + "."]
    return out


def canary_checks(data):
    """CHECKS entries registered by canary applies (status canary with a `check` spec)."""
    return [p["check"] for p in coach_proposals.state(data).values() if p.get("status") == "canary" and p.get("check")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--sources-dir", default=str(Path(__file__).resolve().parent),
                    help="Read-only directory containing coach_scout.py and coach_research.py source lists")
    ap.add_argument("--check", action="append", default=[],
                    help="key=value@ISO-UTC[#signal], e.g. agent.max_turns=150@2026-10-14T05:39Z#synthetic_turn_cap")
    ap.add_argument("--source-target-root", help="Local rollout checkout directory for source-list proposals")
    ap.add_argument("--home", action="append", default=[],
                    help="Hermes home(s) whose coach log + Kanban boards feed the self-heal section (default ~/.hermes)")
    ap.add_argument("--selfheal-export", metavar="PATH",
                    help="write the self-heal lane snapshot (events + cards) to PATH and exit; the hub writes "
                         "<data>/reports/selfheal-lane.jsonl so the cloud Bot sees the lane")
    a = ap.parse_args()
    data = Path(a.data)
    if a.selfheal_export:
        print(f"selfheal-lane: {selfheal_export(a.selfheal_export, a.home or None)} rows -> {a.selfheal_export}")
        return
    ds = coach_mine.load(data)
    now = time.time()
    out = [f"# Weekly coach context — {datetime.fromtimestamp(now, coach_yield.PT):%Y-%m-%d %Z} (last {a.days:g} days)",
           f"Digests: {len(ds)} across hosts {sorted({d['host'] for d in ds})}.", ""]
    out += section_kpis(data, ds, now, a.days) + [""]
    out += section_findings(data, ds, now, a.days, a.top) + [""]
    out += section_spend(ds, now, a.days) + [""]
    out += section_selfheal(data, now, a.days, homes=a.home or None) + [""]
    checks = list(dict.fromkeys(a.check + canary_checks(data)))
    if checks:
        out += ["## Config-change checks", ""]
        for c in checks:
            out += check_change(ds, c, now) + [""]
    ideas = [p for p in (data / "ideas.md", data / "reports" / "ideas.md") if p.exists()]
    out.append(f"## Phase 4 ideas: {ideas[0].relative_to(data)}" if ideas else
               "## Phase 4 ideas: none yet (no ideas.md in the data repo).")
    out.append("")
    out += section_proposals(data, now, a.days)
    out += [""] + coach_yield.section_yield(data, now, a.sources_dir, a.source_target_root)
    print("\n".join(out))


if __name__ == "__main__":
    main()
