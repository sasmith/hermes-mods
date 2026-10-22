#!/usr/bin/env python3
"""How often would the second-opinion actions have fired? Replays the coach detectors over state.db.

  python3 second_opinion_replay.py --days 14                 # deterministic prefilter counts only (free)
  hermes --run-module cProfile -o /dev/null second_opinion_replay.py --days 14 --judge   # + the real aux judges

Turns are rebuilt from `messages`: a turn starts at a real user message (Hermes-written synthetic user turns are
skipped, out-of-band steers unwrapped, as the live plugin does) and ends at the last assistant text before the next
one. The tool log is rebuilt in the plugin's own `-> tool(arg)` form, so the same functions the hooks call decide.

- fix-loop card: user message passes fix_loop.prefilter after an agent reply; with --judge the coach_fixloop judge
  scores it (threshold 0.8) and the window logic of fix_loop.assess counts loops; a card = the 2nd confirmed failure
  inside one window (later failures in the same window reuse it).
- diff review: the reply passes the `unverified` prefilter AND the turn changed a git repo (second_opinion.
  changed_repos on today's filesystem: deleted scratch workspaces don't count, so this undercounts Kanban work);
  with --judge the coach_reply judge must score unverified >= 0.7. cron turns are skipped (pre_verify skips them).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(f"_replay_{name}", HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


core, fl, rc, so = load("core"), load("fix_loop"), load("reply_check"), load("second_opinion")


def real_user(text: str):
    if not isinstance(text, str) or not text.strip():
        return None
    m = core.OOB_RE.match(text)
    if m:
        return m.group(1)
    if core.SYNTHETIC_RE.match(text):
        return None
    return text


def tool_lines(tool_calls: str) -> list[str]:
    out = []
    try:
        calls = json.loads(tool_calls or "[]")
    except Exception:
        return out
    for c in calls or []:
        f = c.get("function") or {}
        name = f.get("name") or c.get("name") or "?"
        try:
            args = json.loads(f.get("arguments") or "{}")
        except Exception:
            args = {}
        if name in ("tool_call",) and isinstance(args.get("calls"), list):  # deferred tools: unwrap
            for cc in args["calls"]:
                a2 = cc.get("arguments") or {}
                out.append(_line(cc.get("name") or "?", a2 if isinstance(a2, dict) else {}))
            continue
        out.append(_line(name, args if isinstance(args, dict) else {}))
    return out


def _line(name, args):
    name = re.sub(r"^mcp__(?=(patch|write_file|terminal)$)", "", name)
    arg = next((str(args[k]) for k in ("command", "code", "path", "url", "query", "goal", "name") if args.get(k)),
               json.dumps(args, default=str))
    # the live log clips at 140 chars; keep paths whole so repo detection sees them
    return f"-> {name}({arg if name in ('patch', 'write_file') else arg[:140]})"


def turns(db, since):
    rows = db.execute(
        "select m.session_id, m.role, m.content, m.tool_calls, m.timestamp, s.source from messages m "
        "join sessions s on s.id=m.session_id where m.timestamp > ? and s.source != 'cron' "
        "order by m.session_id, m.id", (since,))
    cur = None
    for sid, role, content, tcs, ts, source in rows:
        if cur and cur["sid"] != sid:
            yield cur
            cur = None
        if role == "user":
            u = real_user(content)
            if u is None:
                continue
            if cur:
                yield cur
            n = (cur["n"] + 1) if cur and cur["sid"] == sid else 1
            cur = {"sid": sid, "source": source, "n": n, "user": u, "log": [], "reply": "", "ts": ts}
        elif cur and role == "assistant":
            if tcs:
                cur["log"] += tool_lines(tcs)
            if isinstance(content, str) and content.strip():
                cur["reply"] = content
    if cur:
        yield cur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--db", default=str(core.hermes_home() / "state.db"))
    ap.add_argument("--judge", action="store_true", help="run the real coach_fixloop / coach_reply aux judges")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    db = core._ro_connect(a.db, timeout=5)
    since = time.time() - a.days * 86400
    all_turns = list(turns(db, since))
    by_sid: dict[str, list] = {}
    for t in all_turns:
        by_sid.setdefault(t["sid"], []).append(t)
    repo_cache: dict = {}

    def repos_of(log):
        key = tuple(so.files_touched(log)) + tuple(so.commit_dirs(log))
        if key not in repo_cache:
            repo_cache[key] = so.changed_repos(log, [])
        return repo_cache[key]

    fl_cands, dr_cands = [], []
    for sid, ts in by_sid.items():
        for prev, t in zip(ts, ts[1:]):
            if prev["reply"] and fl.prefilter(t["user"]):
                fl_cands.append((sid, t["n"], prev, t))
        for t in ts:
            if t["reply"] and "unverified" in rc.prefilter(t["reply"]) and repos_of(t["log"]):
                dr_cands.append(t)
    res = {"days": a.days, "turns": len(all_turns), "sessions": len(by_sid),
           "sources": dict(Counter(t["source"] for t in all_turns)),
           "fixloop_prefilter_hits": len(fl_cands), "diff_review_prefilter_and_repo": len(dr_cands),
           "diff_review_by_source": dict(Counter(t["source"] for t in dr_cands))}
    if a.judge:
        def fj(c):
            sid, n, prev, t = c
            try:
                v = fl.parse(fl.aux_judge(fl.JUDGE_SYS, fl.judge_prompt(prev["reply"], prev["log"][-25:], t["user"])))
            except Exception as e:
                v = {"err": str(e)[:80]}
            return c, v

        def rj(t):
            try:
                v = rc.parse_verdict(rc.aux_judge(rc.judge_system(), rc.judge_prompt(t["user"], t["log"], t["reply"])))
            except Exception as e:
                v = {"err": str(e)[:80]}
            return t, v
        with cf.ThreadPoolExecutor(a.jobs) as ex:
            fres = list(ex.map(fj, fl_cands))
            rres = list(ex.map(rj, dr_cands))
        # fix-loop windows exactly as fix_loop.assess counts them
        conf: dict[str, list[int]] = {}
        for (sid, n, prev, t), v in fres:
            if v and not v.get("err") and v.get("p", 0) >= 0.8:
                conf.setdefault(sid, []).append(n)
        cards, fails, card_sessions = 0, 0, []
        for sid, turns_ in conf.items():
            window: list[int] = []
            carded_from = None
            for n in sorted(turns_):
                window = [x for x in window if n - x < fl.WINDOW_TURNS] + [n]
                fails += 1
                if len(window) >= 2 and carded_from != window[0]:
                    cards += 1
                    carded_from = window[0]
                    card_sessions.append(sid)
        res.update({"fixloop_judged": len(fres), "fixloop_judge_errors": sum(1 for _, v in fres if not v or v.get("err")),
                    "fixloop_confirmed_failures": fails, "fixloop_cards": cards,
                    "fixloop_card_sessions": card_sessions})
        th = 0.7
        fired = [t for t, v in rres if v and not v.get("err") and v.get("unverified", 0) >= th]
        res.update({"diff_review_judged": len(rres),
                    "diff_review_judge_errors": sum(1 for _, v in rres if not v or v.get("err")),
                    "diff_reviews": len(fired), "diff_reviews_by_source": dict(Counter(t["source"] for t in fired)),
                    "diff_review_sessions": sorted({t["sid"] for t in fired})})
    print(json.dumps(res, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
