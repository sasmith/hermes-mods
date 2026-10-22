#!/usr/bin/env python3
"""Aggregate session digests from every host into KPIs and a labeling queue (stdlib only).

    coach_mine.py --data DIR [--days 7] [--top 10] [--write-kpis] [--write-adoption [--curriculum FILE ...]]

DIR is the digest repo (<host>/<profile>/<id>.json). Prints a Markdown context block for the coach
agent: KPIs per host and overall (this window vs the previous one), then the top-N unlabeled
sessions by waste score with their user turns. Labeled = id present in DIR/findings/*.jsonl.
Excluded profiles: DIR/exclude-profiles.txt lines and --exclude-profile args, each HOST/PROFILE_GLOB (e.g.
somehost/alpha*), drop shared team bots (Slack followers etc.) whose "user turns" are other people's chat, so
they never reach the KPIs or the labeling queue even if a feed pushed their digests.
--write-kpis also appends the window's KPIs to DIR/reports/kpis.jsonl (one line per run day).
--write-adoption evaluates every curriculum entry's `adopted` test over the digests and writes
DIR/reports/adoption.json (the coach plugin on each Mac reads it to retire adopted tips). Curriculum =
--curriculum files (default: curriculum.yaml next to this script, else hermes-mods plugins/coach/curriculum.yaml)
overlaid by DIR/curriculum/*.yaml (hosts' private overlays, copied there by coach_feed.py). Needs PyYAML or
ruamel.yaml for the YAML (the Hermes venv has ruamel).
"""
import argparse, collections, fnmatch, glob, json, os, re, statistics, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MACHINE = ("cron", "kanban", "oneshot", "api_server")
# Allow list, not a deny list: new bot sources (bb-reviewer, bb-judge, taxprep-*) appear all the time and are
# mostly 2-message sessions that would drag msgs_p50/p90 and per-host rows down. They show up in other_sources.
INTERACTIVE_SOURCES = ("desktop", "tui", "cli", "slack", "telegram", "discord", "whatsapp", "signal")


def is_interactive(d):
    """A human typed into this session: a human-facing source and at least one user turn."""
    return d.get("source") in INTERACTIVE_SOURCES and (d.get("user_turns_total") or 0) > 0


def other_sources(ds):
    """{source: {sessions, cost_usd}} for sources that are neither human-facing nor known machine lanes,
    so a new bot source stays visible instead of silently dropping out of the KPIs."""
    out = {}
    for d in ds:
        s = d.get("source") or "(none)"
        if s in INTERACTIVE_SOURCES or s in MACHINE:
            continue
        r = out.setdefault(s, {"sessions": 0, "cost_usd": 0.0})
        r["sessions"] += 1
        r["cost_usd"] += total_cost(d)
    return {s: {"sessions": r["sessions"], "cost_usd": round(r["cost_usd"], 2)}
            for s, r in sorted(out.items(), key=lambda kv: (-kv[1]["sessions"], kv[0]))}


def fmt_other_sources(o):
    return "other_sources: " + (", ".join(f"{s}={r['sessions']} (${r['cost_usd']:g})" for s, r in o.items())
                                or "none") + " (excluded from interactive KPIs)"


NON_DIGEST_DIRS = {"reports", "findings", "research", "eval"}
EXCLUDE_FILE = "exclude-profiles.txt"


def exclude_patterns(data: Path, extra=()):
    """HOST/PROFILE_GLOB patterns from DIR/exclude-profiles.txt (# comments ok) plus extra."""
    pats = []
    f = data / EXCLUDE_FILE
    if f.exists():
        pats += [l.split("#", 1)[0].strip() for l in f.read_text().splitlines()]
    pats += list(extra or ())
    return [p for p in pats if p and "/" in p]


def is_excluded(d, patterns):
    """Digest belongs to an excluded host/profile (shared team bot): fnmatch on both halves."""
    host, prof = d.get("host") or "", d.get("profile") or ""
    for p in patterns:
        ph, _, pp = p.partition("/")
        if fnmatch.fnmatchcase(host, ph) and fnmatch.fnmatchcase(prof, pp):
            return True
    return False


def labeling_queue(cur, done, top):
    """Top-N unlabeled sessions with user turns by waste score (cron excluded)."""
    return sorted((d for d in cur if d["id"] not in done and d.get("user_turns")
                   and d.get("source") not in ("cron",)), key=waste, reverse=True)[:top]


def load(data: Path, exclude=None):
    """Digests under DIR/<host>/<profile>/. exclude=None reads DIR/exclude-profiles.txt; pass a list to override."""
    pats = exclude_patterns(data) if exclude is None else exclude
    out = []
    for f in glob.glob(str(data / "*" / "*" / "*.json")):
        # By top-level path component, so a relative --data ("reports/askgate/x.json") is skipped too.
        try:
            top = Path(f).relative_to(data).parts[0]
        except ValueError:
            top = Path(f).parts[-3]
        if top in NON_DIGEST_DIRS:
            continue
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("host") and d.get("id"):  # research/state/seen.json etc. are not digests
            if not is_excluded(d, pats):
                out.append(d)
    return out


def labeled_ids(data: Path):
    ids = set()
    for f in glob.glob(str(data / "findings" / "*.jsonl")):
        for line in open(f):
            try:
                ids.add(json.loads(line)["session_id"])
            except Exception:
                pass
    return ids


def total_cost(d):
    return (d.get("cost_usd") or 0) + ((d.get("children") or {}).get("cost_usd") or 0)


META_WORDS = ("hermes", "desktop app", "session panel", "composer", "shortcut", "hotkey", "plugin", "gateway",
              "cmd-", "shift-cmd", "skill", "kanban", "cron", "prompt coach", "topic-shift", "spin-out")


def is_meta(d):
    """Session is about Hermes itself: title says so, or Hermes is in >=30% of the user's turns."""
    t = (d.get("title") or "").lower()
    if any(w in t for w in META_WORDS):
        return True
    n = max(1, d.get("user_turns_total") or len(d.get("user_turns") or []))
    return (d.get("signals") or {}).get("hermes_meta", 0) / n >= 0.3


def kpis(ds):
    """Interactive sessions only: human sources (INTERACTIVE_SOURCES) with a user turn."""
    inter = [d for d in ds if is_interactive(d)]
    n = len(inter)
    if not n:
        return {"sessions": 0}
    # Delegated work by spend: Kanban workers vs subagents spawned from chat sessions.
    kan = sum(1 for d in ds if d.get("source") == "kanban")
    kan_cost = sum(total_cost(d) for d in ds if d.get("source") == "kanban")
    chat_sub_cost = sum((d.get("children") or {}).get("cost_usd") or 0 for d in inter)
    sizes = sorted(d["msgs"] for d in inter)
    sig = lambda d, k: (d.get("signals") or {}).get(k, 0)
    p90 = sizes[min(n - 1, int(round(0.9 * (n - 1))))]
    cost = sum(total_cost(d) for d in inter)
    meta_cost = sum(total_cost(d) for d in inter if is_meta(d))
    return {
        "sessions": n,
        "cost_usd": round(cost, 1),
        "msgs_p50": statistics.median(sizes), "msgs_p90": p90,
        "over_300_msgs": sum(1 for s in sizes if s > 300),
        "repeat_fix_rate": round(sum(1 for d in inter if sig(d, "repeat_fix") >= 3) / n, 3),
        "stuck_rate": round(sum(1 for d in inter if sig(d, "stuck") >= 2) / n, 3),
        "hermes_meta_cost_share": round(meta_cost / cost, 3) if cost else 0,
        "screenshot_sessions": sum(1 for d in inter if sig(d, "screenshot")),
        "tool_error_rate": round(sum(sum((d.get("tool_errors") or {}).values()) for d in inter)
                                 / max(1, sum(d.get("tool_calls") or 0 for d in inter)), 3),
        "kanban_sessions": kan,
        "kanban_share": round(kan_cost / (kan_cost + chat_sub_cost), 3) if kan_cost + chat_sub_cost else 0,
        "chat_orchestration_sessions": sum(1 for d in inter if (d.get("children") or {}).get("n", 0) >= 15),
        "kanban_creates_from_chat": sum((d.get("tools") or {}).get("kanban_create", 0) for d in inter),
        "subagents_per_session": round(sum((d.get("children") or {}).get("n", 0) for d in inter) / n, 2),
    }


def waste(d):
    s = d.get("signals") or {}
    errs = sum((d.get("tool_errors") or {}).values())
    return round(total_cost(d) * (1 + 0.15 * s.get("repeat_fix", 0) + 0.1 * s.get("stuck", 0))
                 + 0.02 * d.get("msgs", 0) + 0.05 * errs + 0.3 * s.get("frustration", 0), 2)


# ── curriculum adoption ──────────────────────────────────────────────────

def yaml_load(text):
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
    except ValueError:
        raise SystemExit("coach_mine: --write-adoption needs PyYAML or ruamel.yaml (run it with the Hermes venv python)")


def curriculum_files(data: Path, explicit):
    base = [Path(p) for p in explicit or []]
    if not base:
        for p in (HERE / "curriculum.yaml", HERE.parents[3] / "plugins" / "coach" / "curriculum.yaml"
                  if len(HERE.parents) > 3 else None):
            if p and p.exists():
                base = [p]
                break
    return base + sorted((data / "curriculum").glob("*.yaml"))


def load_curriculum(files):
    merged = {}
    for f in files:
        doc = yaml_load(Path(f).read_text()) or []
        if isinstance(doc, dict):
            doc = doc.get("entries") or []
        for e in doc:
            if not isinstance(e, dict):
                continue
            i = str(e.get("id") or e.get("feature") or "").strip()
            if i:
                merged[i] = {**merged.get(i, {}), **e, "id": i}
    return [e for e in merged.values() if e.get("enabled", True) is not False]


def _names(v):
    return [v] if isinstance(v, str) else list(v or [])


def linked_worktree(d):
    """True when the digest's cwd is inside a linked git worktree: `repo` is the git common root
    (session_digest records git_repo_root), and cwd sits outside it (~/code/babybear-x for repo
    ~/code/babybear) or under its .worktrees/ (hermes -w) or .git/worktrees/ dirs."""
    c, r = (d.get("cwd") or "").rstrip("/"), (d.get("repo") or "").rstrip("/")
    if not c or not r or c == r:
        return False
    if not c.startswith(r + "/"):
        return True
    rest = c[len(r) + 1:]
    return rest.startswith(".worktrees/") or rest.startswith(".git/worktrees/")


def adoption_test(t, ds, now):
    """(count, need, evidence) for one adopted-test mapping over the digests."""
    days = float(t.get("days", 14))
    lo = now - days * 86400
    src = t.get("sources", "any")
    keys = [("features", n) for n in _names(t.get("feature"))] + [("tools", n) for n in _names(t.get("tool"))] \
        + [("signals", n) for n in _names(t.get("signal"))]
    count, ev = 0, []
    for d in ds:
        if (d.get("last_activity_at") or d.get("started_at") or 0) < lo:
            continue
        if src == "interactive" and not is_interactive(d):
            continue
        if isinstance(src, list) and d.get("source") not in src:
            continue
        if t.get("profile") and not re.search(t["profile"], d.get("profile") or ""):
            continue
        if t.get("cwd") and not re.search(t["cwd"], d.get("cwd") or ""):
            continue
        if t.get("linked_worktree") and not linked_worktree(d):
            continue
        if t.get("match") and not re.search(t["match"], " ".join(str(d.get(k) or "") for k in ("repo", "cwd", "title"))):
            continue
        if t.get("min_msgs") and (d.get("msgs") or 0) < int(t["min_msgs"]):
            continue
        n = sum(int((d.get(sec) or {}).get(name) or 0) for sec, name in keys) if keys else 1
        if not n:
            continue
        count += 1 if t.get("unit") == "sessions" or not keys else n
        ev.append((d.get("started_at") or 0, f"{d['host']}/{d['profile']}/{d['id']}"))
    return count, int(t.get("min", 1)), [e for _, e in sorted(ev, reverse=True)[:5]]


def adoption(entries, ds, prev=None, now=None):
    """{id: record}; adoption is sticky (first_adopted kept once set), so a retired tip stays retired."""
    now = now or time.time()
    prev = prev or {}
    out = {}
    for e in entries:
        a = e.get("adopted")
        old = prev.get(e["id"]) or {}
        if not isinstance(a, dict):
            out[e["id"]] = {"adopted": bool(old.get("adopted")), "checked": False, "first_adopted": old.get("first_adopted"),
                            "note": "no machine-readable adopted test"}
            continue
        tests = a["any"] if isinstance(a.get("any"), list) else [a]
        best = None
        for i, t in enumerate(tests):
            t = {**{k: v for k, v in a.items() if k != "any"}, **t}
            count, need, ev = adoption_test(t, ds, now)
            r = {"test": i, "count": count, "need": need, "days": float(t.get("days", 14)), "evidence": ev}
            if best is None or (count >= need) > (best["count"] >= best["need"]) or \
                    ((count >= need) == (best["count"] >= best["need"]) and count / max(1, need) > best["count"] / max(1, best["need"])):
                best = r
        hit = best["count"] >= best["need"]
        first = old.get("first_adopted") or (time.strftime("%Y-%m-%d", time.gmtime(now)) if hit else None)
        out[e["id"]] = {"adopted": bool(hit or old.get("adopted")), "checked": True, "first_adopted": first,
                        "feature": e.get("feature"), **best}
    return out


def write_adoption(data: Path, entries, ds, now=None):
    f = data / "reports" / "adoption.json"
    try:
        prev = json.loads(f.read_text()).get("entries") or {}
    except Exception:
        prev = {}
    res = adoption(entries, ds, prev, now)
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time())),
                             "entries": res}, indent=1, sort_keys=True) + "\n")
    return res


def fmt_adoption(res):
    rows = ["| tip | adopted | count / need (days) | latest evidence |", "|---|---|---|---|"]
    for i, r in res.items():
        if not r.get("checked"):
            rows.append(f"| {i} | {'yes' if r['adopted'] else 'no'} | unchecked | |")
            continue
        rows.append(f"| {i} | {'yes (' + r['first_adopted'] + ')' if r['adopted'] else 'no'} | "
                    f"{r['count']} / {r['need']} ({r['days']:g}) | {(r['evidence'] or [''])[0]} |")
    return "\n".join(rows)


def fmt_kpis(cur, prev):
    keys = [k for k in cur if k != "sessions"] if cur.get("sessions") else []
    rows = [f"| sessions | {cur.get('sessions', 0)} | {prev.get('sessions', 0)} |"]
    for k in keys:
        rows.append(f"| {k} | {cur.get(k)} | {prev.get(k, '–')} |")
    return "| KPI | this window | previous |\n|---|---|---|\n" + "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--turn-chars", type=int, default=300)
    ap.add_argument("--write-kpis", action="store_true")
    ap.add_argument("--write-adoption", action="store_true")
    ap.add_argument("--curriculum", action="append", default=[], help="public curriculum file(s); overlays come from DIR/curriculum/")
    ap.add_argument("--exclude-profile", action="append", default=[],
                    help="HOST/PROFILE_GLOB to drop (shared team bot); adds to DIR/exclude-profiles.txt")
    a = ap.parse_args()
    data = Path(a.data)
    pats = exclude_patterns(data, a.exclude_profile)
    ds = load(data, pats)
    now = time.time()
    lo, plo = now - a.days * 86400, now - 2 * a.days * 86400
    act = lambda d: d.get("last_activity_at") or d.get("started_at") or 0
    cur = [d for d in ds if act(d) >= lo]
    prev = [d for d in ds if plo <= act(d) < lo]

    by_host = collections.defaultdict(list)
    for d in cur:
        by_host[d["host"]].append(d)
    overall = kpis(cur)
    out = [f"# Coach data — last {a.days:g} days ({time.strftime('%Y-%m-%d')})",
           f"Digests loaded: {len(ds)} across hosts {sorted({d['host'] for d in ds})}"
           + (f" (excluded profiles: {', '.join(pats)})." if pats else "."), "",
           "## KPIs, all hosts (interactive sessions)", fmt_kpis(overall, kpis(prev)), "",
           fmt_other_sources(other_sources(cur)), ""]
    for h in sorted(by_host):
        k = kpis(by_host[h])
        out.append(f"- **{h}**: " + ", ".join(f"{x}={k[x]}" for x in
                   ("sessions", "cost_usd", "msgs_p90", "repeat_fix_rate", "hermes_meta_cost_share") if x in k))
    if a.write_kpis:
        (data / "reports").mkdir(exist_ok=True)
        # One row per day: a rerun on the same day replaces that day's row.
        kf = data / "reports" / "kpis.jsonl"
        today = time.strftime("%Y-%m-%d")
        rows = [l for l in (kf.read_text().splitlines() if kf.exists() else [])
                if l.strip() and not l.startswith('{"date": "%s"' % today)]
        rows.append(json.dumps({"date": today, "days": a.days, "overall": overall, "other_sources": other_sources(cur),
                                "by_host": {h: kpis(v) for h, v in by_host.items()}}))
        kf.write_text("\n".join(rows) + "\n")

    if a.write_adoption:
        res = write_adoption(data, load_curriculum(curriculum_files(data, a.curriculum)), ds, now)
        out += ["", f"## Curriculum adoption ({sum(r['adopted'] for r in res.values())}/{len(res)} adopted, reports/adoption.json)",
                fmt_adoption(res)]

    done = labeled_ids(data)
    queue = labeling_queue(cur, done, a.top)
    out += ["", f"## Labeling queue: top {len(queue)} unlabeled sessions by waste score", ""]
    for d in queue:
        st = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["started_at"]))
        out.append(f"### {d['host']}/{d['profile']}/{d['id']} — {d.get('title') or '(untitled)'}")
        out.append(f"hermes_meta={is_meta(d)} source={d['source']} started={st} msgs={d['msgs']} user_turns={d['user_turns_total']} "
                   f"cost=${total_cost(d):.2f} (children {d['children']['n']}) waste={waste(d)} "
                   f"repo={d.get('repo')} signals={d.get('signals')} tool_errors={d.get('tool_errors')}")
        out.append("top tools: " + ", ".join(f"{k}={v}" for k, v in list((d.get("tools") or {}).items())[:10]))
        out.append("user turns:")
        for u in d["user_turns"]:
            t = u["text"].replace("\n", " ")
            out.append(f"  - {t[:a.turn_chars]}{'…' if len(t) > a.turn_chars else ''}")
        out.append(f"last reply: {(d.get('last_reply') or '').replace(chr(10), ' ')[:400]}")
        out.append("")
    print("\n".join(out))


if __name__ == "__main__":
    main()
