#!/usr/bin/env python3
"""Replay self_heal's two detectors over an incident-labeled turn set and score them like the self-correct replay.

Inputs (private, from the self-correct replay; never committed here):
  turns.jsonl      every real user turn: {sid, idx, text, prev_reply, ...}   (prev_reply = agent reply before it)
  turns2.jsonl     per-turn telemetry: {sid, idx, api_err, interrupted, repeat, no_reply, ...}
  episodes.json    incident episodes: [{eid, sid, first, last, turns: [{sid, idx}, ...]}]
  fivewhys.jsonl   per-episode verdict: {eid, real_incident, explicit_fix_ask_turn}
  fpcheck.jsonl    optional relabels of sampled "false positive" turns: {sid, idx, real_incident}

Step 1 `run`: for every turn, the complaint path (prefilter + coach_defect judge on (prev_reply, text)) and the
admission path (prefilter + judge on prev_reply, i.e. the agent's own final reply just before the user spoke).
Judge verdicts are cached in <out>/cache.jsonl; per-turn verdicts go to <out>/verdicts.jsonl (p kept below the
threshold too, so `score` can sweep thresholds for free). Run under the Hermes interpreter (aux client):

  <hermes python> -I selfheal_incident_replay.py run --hermes-root ~/.hermes/hermes-agent --data DIR --out OUT

Step 2 `score`: score3.py's definitions. An episode is caught when, for some turn k from its first flagged turn up to
the explicit fix ask (or last turn + 1), either the pre-emptive path fires on the agent reply before turn k
(admission, plus telemetry with --telemetry) or the reactive path fires on turn k with k before the fix ask
("unasked"). "with_ask" also counts the fix ask itself. False positives: either path firing on a turn that is not in
any real episode; --relabel moves fpcheck turns judged real out of the false-positive count.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIT_KINDS = ("admission",)  # mirrors self_heal.AUDIT_KINDS: the audit kinds that count as a detection


def load_self_heal():
    spec = importlib.util.spec_from_file_location("selfheal_incident_mod", HERE.parent / "self_heal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def jl(path):
    return [json.loads(x) for x in open(path) if x.strip()]


def run(a, llm=None, confirm_llm=None):
    """Every turn through the production code: prefilter + complaint_verdict (cascade) on the user's message, and
    machine_signal + admission_prefilter + audit_verdict on the agent reply just before it. p is kept below the
    thresholds, so `score` can sweep them. Judge calls are cached per (model tag, system, prompt)."""
    if a.hermes_root:
        sys.path.insert(0, a.hermes_root)
        import hermes_bootstrap  # noqa: F401
    sh = load_self_heal()
    confirm_model = getattr(a, "confirm_model", "")
    if confirm_model and not confirm_llm:
        os.environ["COACH_DEFECT_CONFIRM_MODEL"] = confirm_model
        os.environ["COACH_DEFECT_CONFIRM_PROVIDER"] = getattr(a, "confirm_provider", "")
        confirm_llm = sh.confirm_judge()
    data, out = Path(a.data), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    turns = jl(data / "turns.jsonl")
    if a.limit:
        turns = turns[: a.limit]
    cache_path = out / "cache.jsonl"
    cache = {}
    if cache_path.exists():
        for x in cache_path.read_text().splitlines():
            try:
                d = json.loads(x)
                cache[d["k"]] = d["raw"]
            except Exception:
                pass
    lock = threading.Lock()
    stats = collections.Counter()

    def cached(tag, fn):
        def judge(system, prompt):
            k = hashlib.sha1(f"{tag}\n{system}\n{prompt}".encode()).hexdigest()
            if k in cache:
                return cache[k]
            raw = ""
            for i in range(3):
                try:
                    raw = fn(system, prompt)
                    if sh.parse(raw):
                        break
                except Exception:
                    time.sleep(2 * (i + 1))
            with lock:
                stats[f"calls:{tag}"] += 1
                if not sh.parse(raw):
                    stats["errors"] += 1
                    return raw
                cache[k] = raw
                with open(cache_path, "a") as fh:
                    fh.write(json.dumps({"k": k, "raw": raw}) + "\n")
            return raw
        return judge

    judge = cached(a.tag, llm or sh.aux_judge)
    cj = cached(confirm_model or "confirm", confirm_llm) if confirm_llm else None
    X = {(r["sid"], r["idx"]): r for r in jl(data / "turns2.jsonl")}

    def one(t):
        r = {"sid": t["sid"], "idx": t["idx"]}
        log = X.get((t["sid"], t["idx"]), {}).get("prev_log") or []
        prev = t.get("prev_reply") or ""
        cprev = prev
        if getattr(a, "clip_at", 0) and len(prev) == a.clip_at:  # dataset clipped this reply; production won't
            cprev += " [… rest of this message clipped in the dataset]"
        r["c_pre"] = sh.prefilter(t.get("text") or "")
        if r["c_pre"]:
            v = sh.complaint_verdict(cprev, t.get("text") or "", judge, log, cj) or {}
            r.update(c_p=v.get("p_defect"), c_kind=v.get("kind"), c_comp=v.get("component"),
                     c_sym=v.get("symptom"), c_stages=v.get("stages"))
        if not prev and not log:
            return r
        sig = sh.machine_signal(prev, log)
        r["a_sig"] = sig["kind"] if sig else None
        r["a_pre"] = sh.admission_prefilter(prev, log)
        if r["a_pre"]:
            v = sh.audit_verdict(prev, judge, log, t.get("prev_user") or "", cj) or {}
            r.update(a_p=v.get("p_defect"), a_kind=v.get("kind"), a_comp=v.get("component"), a_sym=v.get("symptom"))
        return r

    with ThreadPoolExecutor(a.workers) as ex:
        rows = list(ex.map(one, turns))
    with open(out / "verdicts.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(json.dumps({"turns": len(rows), **stats}))


def score(data: Path, verdicts: list, tc: float, ta: float, telemetry: bool, relabel: bool):
    eps = {e["eid"]: e for e in json.load(open(data / "episodes.json"))}
    F = {r["eid"]: r for r in jl(data / "fivewhys.jsonl")}
    X = {(r["sid"], r["idx"]): r for r in jl(data / "turns2.jsonl")}
    T = {(r["sid"], r["idx"]): r for r in jl(data / "turns.jsonl")}
    V = {(r["sid"], r["idx"]): r for r in verdicts}
    fpc = {}
    if relabel and (data / "fpcheck.jsonl").exists():
        fpc = {(r["sid"], r["idx"]): bool(r.get("real_incident")) for r in jl(data / "fpcheck.jsonl")}

    def pre(k):
        v, x = V.get(k, {}), X.get(k, {})
        h = []
        if (v.get("a_p") or 0) >= ta and v.get("a_kind") in AUDIT_KINDS:
            h.append(f"turn-end audit ({v.get('a_kind')}): {v.get('a_sym')}")
        if v.get("a_sig"):
            h.append(f"machine signal: {v['a_sig']}")
        if telemetry:
            h += [n for n in ("api_err", "interrupted", "repeat") if x.get(n)]
        return h

    def react(k):
        v = V.get(k, {})
        return (v.get("c_p") or 0) >= tc and v.get("c_kind") not in ("none", "product_bug", "external")

    real = [e for e in F if F[e].get("real_incident") is True]
    pos = {(t["sid"], t["idx"]) for e in real for t in eps[e]["turns"]}
    rows = []
    for e in real:
        ep = eps[e]
        sid, first, last = ep["sid"], int(ep["first"]), int(ep["last"])
        ex = F[e].get("explicit_fix_ask_turn")
        ex = ex if isinstance(ex, int) else None
        end = ex if (ex is not None and first <= ex <= last + 1) else last + 1
        res = {"eid": e, "sid": sid, "first": first, "explicit": ex, "mode": None, "mode_with_ask": None,
               "cluster": None}
        for k in range(first, end + 1):
            if pre((sid, k)):
                res["mode"] = res["mode"] or "preemptive"
                res["mode_with_ask"] = res["mode_with_ask"] or "preemptive"
                res["trigger"] = pre((sid, k))[0]
                break
            if react((sid, k)):
                if (ex is None or k < ex) and (k < end or ex is None):
                    res["mode"] = "reactive"
                    res["mode_with_ask"] = res["mode_with_ask"] or "reactive"
                    res["trigger"] = "complaint: " + str(V[(sid, k)].get("c_sym"))
                    break
                res["mode_with_ask"] = res["mode_with_ask"] or "fix_ask"
        res["cut"] = k if res["mode"] else None
        q = T.get((sid, first), {}).get("text", "")
        res["quote"] = " ".join(str(q).split())[:240]
        res["first_c_p"] = V.get((sid, first), {}).get("c_p")
        res["first_c_pre"] = V.get((sid, first), {}).get("c_pre")
        rows.append(res)
    n = len(rows)
    caught = sum(1 for r in rows if r["mode"])
    caught_ask = sum(1 for r in rows if r["mode_with_ask"])
    neg = [k for k in T if k not in pos]
    fp = []
    for k in neg:
        p, rr = pre(k), react(k)
        if p or rr:
            fp.append({"sid": k[0], "idx": k[1], "pre": p, "react": rr, "sym": V.get(k, {}).get("c_sym") if rr else "",
                       "quote": " ".join(str(T[k].get("text", "")).split())[:240],
                       "fpcheck": fpc.get(k)})
    true_fp = [f for f in fp if f["fpcheck"] is not True]
    m = collections.Counter(r["mode"] for r in rows)
    return {"tc": tc, "ta": ta, "telemetry": telemetry, "n": n, "caught": caught, "catch_rate": round(caught / n, 3),
            "preemptive": m["preemptive"], "reactive": m["reactive"],
            "caught_with_ask": caught_ask, "catch_rate_with_ask": round(caught_ask / n, 3),
            "neg_turns": len(neg), "fp_any": len(fp), "fp_rate": round(len(fp) / len(neg), 3),
            "fp_relabeled_real": len(fp) - len(true_fp), "fp_true": len(true_fp),
            "fp_true_rate": round(len(true_fp) / len(neg), 3), "fp_per_day": round(len(fp) / 7, 1),
            "episodes": rows, "fps": fp}


def write_report(res: dict, data: Path, path: Path, baseline: str = "") -> None:
    """Markdown: headline vs baseline, per-cluster catch, every miss with its quote, every false positive."""
    clusters = {}
    cl_names = {}
    if (data / "clusters.json").exists():
        for c in json.load(open(data / "clusters.json")).get("clusters", []):
            cl_names[c["id"]] = c.get("name", "")
            for m in c.get("members", []):
                clusters[m] = c["id"]
    eps = res["episodes"]
    md = [f"# Self-heal detector replay: {res['n']} real incidents, {res['neg_turns']} normal turns", "",
          f"Thresholds: complaint {res['tc']}, turn-end audit {res['ta']}; telemetry flags "
          f"{'on' if res['telemetry'] else 'off'}.", "",
          "| | caught before any fix ask | counting the fix ask | false positives (normal turns) |", "|---|---|---|---|",
          f"| this detector | {res['caught']}/{res['n']} = {res['catch_rate']:.0%} (pre-emptive {res['preemptive']}, "
          f"reactive {res['reactive']}) | {res['caught_with_ask']}/{res['n']} = {res['catch_rate_with_ask']:.0%} | "
          f"{res['fp_any']}/{res['neg_turns']} = {res['fp_rate']:.1%}; after fpcheck relabel {res['fp_true']} = "
          f"{res['fp_true_rate']:.1%} (~{res['fp_true'] / 7:.0f}/day) |"]
    if baseline:
        md.append(baseline)
    md += ["", "## By cluster", "", "| cluster | caught / incidents |", "|---|---|"]
    by = collections.defaultdict(lambda: [0, 0])
    for r in eps:
        c = clusters.get(r["eid"], "—")
        by[c][1] += 1
        by[c][0] += bool(r["mode"])
    for c in sorted(by):
        md.append(f"| {c} {cl_names.get(c, '')} | {by[c][0]}/{by[c][1]} |")
    misses = [r for r in eps if not r["mode"]]
    md += ["", f"## Misses ({len(misses)})", "",
           "`ask-only` = caught only on the explicit fix ask; `pre-only` = the fix ask was the first user turn, so only "
           "a turn-end signal could have caught it unasked.", ""]
    for r in sorted(misses, key=lambda r: (clusters.get(r["eid"], ""), r["eid"])):
        why = ("ask-only" if r["mode_with_ask"] else "missed") + (
            " pre-only" if r["explicit"] is not None and r["explicit"] <= r["first"] else "")
        md.append(f"- {r['eid']} {clusters.get(r['eid'], '')} [{why}] (`{r['sid']}` turn {r['first']}): "
                  f"\"{r['quote'][:200]}\"")
    md += ["", f"## False positives ({res['fp_any']}; fpcheck=real marks a turn an independent check found to be a "
           "real incident)", ""]
    for f in res["fps"]:
        what = "; ".join(f["pre"]) if f["pre"] else f"complaint: {f['sym']}"
        tag = " fpcheck=real" if f["fpcheck"] is True else (" fpcheck=no-fault" if f["fpcheck"] is False else "")
        md.append(f"- `{f['sid']}` turn {f['idx']}{tag} — {what} — \"{f['quote'][:160]}\"")
    path.write_text("\n".join(md) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--data", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--hermes-root", default="")
    r.add_argument("--workers", type=int, default=12)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--tag", default="aux:coach_defect")
    r.add_argument("--clip-at", type=int, default=0,
                   help="replies exactly this long were clipped by the dataset builder; mark them so the judge "
                        "doesn't read the cut as a stalled turn (the self-correct set clips at 1500)")
    r.add_argument("--confirm-model", default="", help="second-stage model (production: coach_defect_confirm)")
    r.add_argument("--confirm-provider", default="")
    s = sp.add_parser("score")
    s.add_argument("--data", required=True)
    s.add_argument("--verdicts", required=True)
    s.add_argument("--tc", type=float, default=0.6)
    s.add_argument("--ta", type=float, default=0.9)
    s.add_argument("--telemetry", action="store_true")
    s.add_argument("--relabel", action="store_true")
    s.add_argument("--json", default="")
    s.add_argument("--report", default="", help="write a markdown report here")
    s.add_argument("--baseline", default="", help="a markdown table row for the baseline detector")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return run(a)
    res = score(Path(a.data), jl(a.verdicts), a.tc, a.ta, a.telemetry, a.relabel)
    print(json.dumps({k: v for k, v in res.items() if k not in ("episodes", "fps")}))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1))
    if a.report:
        write_report(res, Path(a.data), Path(a.report), a.baseline)


if __name__ == "__main__":
    main()
