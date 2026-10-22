#!/usr/bin/env python3
"""Replay eval for the self-heal complaint detector (self_heal.detect_complaint) over a hand-labeled set.

Dataset: JSONL rows {id, label (1 defect report / 0 not), component, text (user message), prev_reply, split, ...}.
The real set holds the user's own words, so it lives in the private coach data repo
(hermes-coach-data/eval/selfheal-v1.jsonl), never here.

Runs the exact production path: prefilter, then the `coach_defect` judge with the production prompt, via the Hermes
auxiliary client. So run it under the Hermes interpreter:

  <hermes python> -I plugins/coach/eval/selfheal_replay.py --hermes-root ~/.hermes/hermes-agent \
      --data ~/code/hermes-coach-data/eval/selfheal-v1.jsonl --out /tmp/selfheal-eval [--model M --provider P]

Judge verdicts are cached by (prompt, model) in <out>/cache.jsonl, so re-running after a prefilter-only change is
free. Writes <out>/report.md + report.json: catch rate (recall), precision, every miss with its quote, every false
positive, per split (dev/test) and per component, and the prefilter's own recall.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_self_heal():
    spec = importlib.util.spec_from_file_location("selfheal_eval_mod", HERE.parent / "self_heal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def metrics(rows, pred_key="pred"):
    tp = sum(1 for r in rows if r["label"] == 1 and r[pred_key])
    fn = sum(1 for r in rows if r["label"] == 1 and not r[pred_key])
    fp = sum(1 for r in rows if r["label"] == 0 and r[pred_key])
    tn = sum(1 for r in rows if r["label"] == 0 and not r[pred_key])
    rec = tp / (tp + fn) if tp + fn else 0.0
    prec = tp / (tp + fp) if tp + fp else 0.0
    return {"n": len(rows), "pos": tp + fn, "neg": fp + tn, "tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "catch_rate": round(rec, 3), "precision": round(prec, 3),
            "fp_rate": round(fp / (fp + tn), 3) if fp + tn else 0.0}


def quote(t, n=220):
    return " ".join(str(t or "").split())[:n]


def main(argv=None, llm=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--hermes-root", default="")
    ap.add_argument("--model", default=os.environ.get("COACH_DEFECT_MODEL", ""))
    ap.add_argument("--provider", default=os.environ.get("COACH_DEFECT_PROVIDER", ""))
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-prev", action="store_true", help="hide the previous reply from the judge (ablation)")
    ap.add_argument("--mode", choices=("complaint", "admission"), default="complaint",
                    help="admission: rows' text is the agent's final reply, judged by detect_admission's path")
    a = ap.parse_args(argv)
    if a.hermes_root:
        sys.path.insert(0, a.hermes_root)
        import hermes_bootstrap  # noqa: F401  (credentials, provider routing)
    if a.model:
        os.environ["COACH_DEFECT_MODEL"] = a.model
    if a.provider:
        os.environ["COACH_DEFECT_PROVIDER"] = a.provider
    sh = load_self_heal()
    th = a.threshold if a.threshold is not None else sh.THRESHOLD
    rows = [json.loads(x) for x in open(a.data) if x.strip()]
    if a.limit:
        rows = rows[: a.limit]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
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
    model_tag = f"{a.provider}/{a.model}" if a.model else "aux:coach_defect"
    calls = {"n": 0, "err": 0}

    def judge(system, prompt):
        k = hashlib.sha1(f"{model_tag}\n{system}\n{prompt}".encode()).hexdigest()
        if k in cache:
            return cache[k]
        try:
            raw = (llm or sh.aux_judge)(system, prompt)
        except Exception as exc:
            with lock:
                calls["err"] += 1
            raise exc
        with lock:
            calls["n"] += 1
            cache[k] = raw
            with open(cache_path, "a") as fh:
                fh.write(json.dumps({"k": k, "raw": raw}) + "\n")
        return raw

    def one(r):
        prev = "" if a.no_prev else r.get("prev_reply") or ""
        if a.mode == "admission":
            r["prefilter"] = sh.admission_prefilter(r["text"], r.get("tool_log"))
            system, prompt = sh._with_profile(sh.AUDIT_SYS), sh.admission_prompt(r["text"], r.get("tool_log"),
                                                                                  r.get("ask") or "")
        else:
            r["prefilter"] = sh.prefilter(r["text"])
            system, prompt = sh._with_profile(sh.COMPLAINT_SYS), sh.complaint_prompt(prev, r["text"],
                                                                                      r.get("tool_log"))
        v = None
        if r["prefilter"]:
            for _ in range(2):  # one retry on a transient provider error
                try:
                    v = sh.parse(judge(system, prompt))
                    break
                except Exception as exc:
                    r["error"] = str(exc)[:200]
        r["p"] = v["p_defect"] if v else None
        r["pred_component"] = v["component"] if v else ""
        r["symptom"] = v["symptom"] if v else ""
        r["pred"] = bool(v and v["p_defect"] >= th and v.get("kind") not in ("product_bug", "external", "none"))
        return r

    with ThreadPoolExecutor(a.workers) as ex:
        rows = list(ex.map(one, rows))

    for r in rows:
        r["pre_pred"] = r["prefilter"]
    rep = {"threshold": th, "model": model_tag, "judge_calls": calls, "no_prev": a.no_prev,
           "all": metrics(rows), "prefilter_only": metrics(rows, "pre_pred"),
           "splits": {s: metrics([r for r in rows if r.get("split") == s]) for s in ("dev", "test")},
           "with_prev": metrics([r for r in rows if r.get("prev_reply")]),
           "without_prev": metrics([r for r in rows if not r.get("prev_reply")]),
           "random_sample": metrics([r for r in rows if r.get("src") == "random"]),
           "components": {}, "errors": sum(1 for r in rows if r.get("error") and r.get("p") is None)}
    comps = sorted({r["component"] for r in rows if r["label"] == 1})
    for c in comps:
        sub = [r for r in rows if r["label"] == 1 and r["component"] == c]
        rep["components"][c] = {"pos": len(sub), "caught": sum(1 for r in sub if r["pred"])}
    misses = [r for r in rows if r["label"] == 1 and not r["pred"]]
    fps = [r for r in rows if r["label"] == 0 and r["pred"]]
    rep["misses"] = [{"id": r["id"], "component": r["component"], "prefilter": r["prefilter"], "p": r["p"],
                      "quote": quote(r["text"]), "prev": quote(r.get("prev_reply"), 160)} for r in misses]
    rep["false_positives"] = [{"id": r["id"], "p": r["p"], "pred_component": r["pred_component"],
                               "symptom": r["symptom"], "quote": quote(r["text"])} for r in fps]
    (out / "report.json").write_text(json.dumps(rep, indent=1))
    with open(out / "rows.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    def line(name, m):
        return (f"| {name} | {m['n']} | {m['pos']} | {m['tp']} | **{m['catch_rate']:.0%}** | {m['fp']} | "
                f"**{m['precision']:.2f}** | {m['fp_rate']:.1%} |")
    md = [f"# Self-heal {a.mode} detector replay ({model_tag}, threshold {th})", "",
          "| set | n | defects | caught | catch rate | FPs | precision | FP rate on non-defects |",
          "|---|---|---|---|---|---|---|---|",
          line("all", rep["all"]), line("dev split", rep["splits"]["dev"]), line("test split", rep["splits"]["test"]),
          line("random turns only", rep["random_sample"]), line("with prev reply", rep["with_prev"]),
          line("without prev reply", rep["without_prev"]), line("prefilter alone", rep["prefilter_only"]), "",
          f"Judge calls this run: {calls['n']} (errors {calls['err']}); unjudged after retry: {rep['errors']}.", "",
          "## Per component", "", "| component | defects | caught |", "|---|---|---|"]
    md += [f"| {c} | {v['pos']} | {v['caught']} |" for c, v in rep["components"].items()]
    md += ["", f"## Misses ({len(misses)})", ""]
    md += [f"- `{m['id']}` [{m['component']}] {'prefilter miss' if not m['prefilter'] else 'p=' + str(m['p'])}: "
           f"\"{m['quote']}\"" for m in rep["misses"]]
    md += ["", f"## False positives ({len(fps)})", ""]
    md += [f"- `{f['id']}` p={f['p']} [{f['pred_component']}: {f['symptom']}]: \"{f['quote']}\""
           for f in rep["false_positives"]]
    (out / "report.md").write_text("\n".join(md) + "\n")
    print(json.dumps({k: rep[k] for k in ("all", "prefilter_only", "splits", "random_sample", "errors")}, indent=1))


if __name__ == "__main__":
    main()
