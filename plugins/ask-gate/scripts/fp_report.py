"""False-positive run: how many final replies of the last N days of Desktop sessions would ask-gate have held?

Run under the Hermes runtime interpreter (see replay.py). Writes raw results as JSON; a human reads every hold.
  FP_DAYS=14 FP_OUT=out.json (or argv) ; runs under the runtime interpreter
Historical card state: a card counts as done only if it was completed BEFORE the reply (tasks.completed_at).
The judge runs with an empty ledger (none existed then) and this machine's capability sheet.
"""
import concurrent.futures as cf
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ask_gate_fp", HERE / "__init__.py", submodule_search_locations=[str(HERE)])
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
core = gate.core

days = float(os.environ.get("FP_DAYS") or (sys.argv[1] if len(sys.argv) > 1 else 14))
out_path = os.environ.get("FP_OUT") or (sys.argv[2] if len(sys.argv) > 2 else "fp-raw.json")
tmp = Path(tempfile.mkdtemp())
os.environ["ASK_GATE_LEDGER"] = str(tmp / "l.sqlite")
gate._LEDGER.update(path=str(tmp / "l.sqlite"), obj=core.Ledger(tmp / "l.sqlite"))

state = core._ro_connect(Path.home() / '.hermes/state.db', timeout=5)
cutoff = time.time() - days * 86400
sessions = [r[0] for r in state.execute(
    "select id from sessions where source='desktop' and started_at>=? order by started_at", (cutoff,))]

# card completion times for historical state
done_at = {}
for p in core.boards():
    try:
        c = core._ro_connect(p, timeout=5)
        for tid, st, ca in c.execute("select id, status, completed_at from tasks"):
            done_at[tid] = (st, ca)
        c.close()
    except sqlite3.Error:
        continue

items, total = [], 0
for sid in sessions:
    rows = state.execute("select id, role, content, tool_calls, tool_name, tool_call_id, timestamp from messages "
                         "where session_id=? order by id", (sid,)).fetchall()
    turn_start = 0
    for idx, r in enumerate(rows):
        mid, role, content, tcalls, _, _, ts = r
        if role == "user":
            turn_start = idx
        if role != "assistant" or not content or not content.strip() or tcalls:
            continue
        total += 1
        prev_users = [x[2] for x in rows[:idx] if x[1] == "user" and isinstance(x[2], str)
                      and not x[2].startswith("[IMPORTANT")]
        asks = core.find_asks(content, howto=bool(prev_users) and core.asked_how_to(prev_users[-1]))
        if not asks:
            continue
        calls, tools = {}, []
        for rr in rows[turn_start:idx]:
            if rr[1] == "assistant" and rr[3]:
                try:
                    for tc in json.loads(rr[3]):
                        fn = tc.get("function", {})
                        e = {"tool": fn.get("name"), "args": core._short(fn.get("arguments"), 400), "result": "", "t": 0}
                        calls[tc.get("id")] = e
                        tools.append(e)
                except ValueError:
                    pass
            elif rr[1] == "tool" and rr[5] in calls:
                calls[rr[5]]["result"] = core._short(rr[2] or "", 400)
        users = [x[2] for x in rows[:idx] if x[1] == "user" and isinstance(x[2], str)
                 and not x[2].startswith("[IMPORTANT")][-30:]
        items.append(dict(sid=sid, mid=mid, ts=ts, reply=content, asks=asks, tools=tools[-40:], users=users))

caps = core.capability_sheet()


def hist_status(ids, ts):
    out = {}
    for i in ids:
        if i in done_at:
            st, ca = done_at[i]
            at = "done" if (st in ("done", "archived") and ca and ca <= ts) else "blocked/open"
            out[i] = {"id": i, "title": "", "status": at, "completed": ca, "result": ""}
    return out


def run(it):
    core.card_status = lambda ids, db_paths=None: hist_status(ids, it["ts"])
    ids = core.card_ids(" ".join(it["asks"]) + " " + it["reply"])
    v = None
    for attempt in range(2):
        try:
            v = gate.judge_mod.judge(it["asks"], tools_text=core.render_tools(it["tools"]), user_msgs=it["users"],
                                     card_status=hist_status(ids, it["ts"]), ledger_text="", caps=caps,
                                     reply=it["reply"], llm=gate.default_llm,
                                     soft=[core.is_soft_ask(x) for x in it["asks"]])
            break
        except Exception as e:
            err = repr(e)
    return {**{k: it[k] for k in ("sid", "mid", "ts", "asks")}, "verdicts": v, "error": None if v else err,
            "reply_head": it["reply"][:300]}


with cf.ThreadPoolExecutor(5) as ex:
    results = list(ex.map(run, items))
held = [r for r in results if r["verdicts"] and any(x["verdict"] != "ok" for x in r["verdicts"])]
errs = [r for r in results if r["error"]]
json.dump({"days": days, "sessions": len(sessions), "final_replies": total, "with_asks": len(items),
           "held": len(held), "errors": len(errs), "results": results}, open(out_path, "w"), indent=1)
print(f"sessions={len(sessions)} final_replies={total} detector_flagged={len(items)} held={len(held)} errors={len(errs)}"
      f" held_rate={100 * len(held) / max(total, 1):.2f}%")
