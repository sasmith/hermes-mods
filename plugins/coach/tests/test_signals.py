"""self-heal machine-signal sweeper (signals.py): one test per signal source with a fixture, the cursor, the hub
sweep (file_defect per signal, shadow), the Mac forwarder (cursor only moves on success), hub ingest, and the
monitor re-route (lane owns it -> no Do-it card)."""
import importlib.util
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "signals"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sg = load("coach_signals_test", ROOT / "signals.py")
sh = load("coach_self_heal_signals_test", ROOT / "self_heal.py")
core = load("coach_core_test_signals", ROOT / "core.py")

NOW = 1792660000.0  # 2026-10-22 ~09:06 UTC
OPEN = lambda board=None, now=None: {"usd": 0.0, "cap": 25.0, "running": 0, "ok_usd": True, "ok_running": True,
                                     "why": ""}


def make_board(db: Path, runs):
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.execute("create table tasks (id text primary key, title text, status text, idempotency_key text)")
    c.execute("create table task_runs (id integer primary key autoincrement, task_id text, status text, "
              "started_at integer, ended_at integer, outcome text, error text)")
    for tid, title, key in {(r[0], r[1], r[2]) for r in runs}:
        c.execute("insert into tasks values (?,?,?,?)", (tid, title, "ready", key))
    for tid, _title, _key, outcome, err in runs:
        c.execute("insert into task_runs (task_id, status, started_at, ended_at, outcome, error) values "
                  "(?,?,?,?,?,?)", (tid, outcome, int(NOW) - 600, int(NOW) - 300, outcome, err))
    c.commit()
    c.close()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "logs").mkdir(parents=True)
    (h / "cron").mkdir()
    (h / "coach").mkdir()
    shutil.copy(FIX / "errors.txt", h / "logs" / "errors.log")
    shutil.copy(FIX / "gateway-exit-diag.txt", h / "logs" / "gateway-exit-diag.log")
    shutil.copy(FIX / "jobs.json", h / "cron" / "jobs.json")
    shutil.copy(FIX / "coach-log.jsonl", h / "coach" / "log.jsonl")
    make_board(h / "kanban.db", [
        ("t_crash2", "build the thing", None, "crashed", "worker died: SIGKILL"),
        ("t_crash2", "build the thing", None, "timed_out", "max_runtime exceeded"),
        ("t_once", "crashed once", None, "crashed", "worker died"),
        ("t_ok", "fine card", None, "completed", None),
        ("t_sh", "self-heal: x", "selfheal:agent-0123456789", "crashed", "a"),
        ("t_sh", "self-heal: x", "selfheal:agent-0123456789", "crashed", "b"),
    ])
    return h


def kinds(sigs, kind):
    return [s for s in sigs if s["kind"] == kind]


# ── one test per source ────────────────────────────────────────────────────────────────────────────────────

def test_errors_log_tracebacks(home):
    sigs = kinds(sg.collect(home, {}, "hubhost", NOW), "errors")
    syms = [s["symptom"] for s in sigs]
    # 429 (provider transient) and the tool-output traceback are not defects; the two malformed-db tracebacks
    # with different line numbers are ONE fingerprint (frames by file:function, no line numbers)
    assert not any("RateLimit" in x for x in syms)
    assert not any("tool_executor" in s["details"] for s in sigs)
    db = [s for s in sigs if "DatabaseError" in s["symptom"]]
    assert len(db) == 1 and db[0]["count"] == 2
    assert db[0]["symptom"].startswith("DatabaseError in reconcile_state_schema (hermes_state_schema.py)")
    plug = [s for s in sigs if s["component"] == "plugin"]
    assert len(plug) == 1 and "[example-plugin]" in plug[0]["symptom"] and plug[0]["fp"].startswith("plugin-")
    assert "/home/you" not in db[0]["details"] and "~/.hermes/hermes-agent" in db[0]["details"]


def test_library_warning_traceback_is_not_a_defect_but_ours_is():
    def rec(level, logger, path):
        return [f"2026-10-22 08:00:00,000 {level} {logger}: Failed to clean up child process",
                "Traceback (most recent call last):",
                f'  File "{path}", line 10, in terminate_tree',
                "    os.killpg(pgid, sig)",
                "PermissionError: [Errno 1] Operation not permitted"]
    lib = "/home/you/.hermes/hermes-agent/venv/lib/python3.11/site-packages/mcp/os/posix/utilities.py"
    own = "/home/you/.hermes/hermes-agent/tools/mcp_tool.py"
    assert sg.parse_errors(rec("WARNING", "mcp.os.posix.utilities", lib)) == []
    assert len(sg.parse_errors(rec("WARNING", "tools.mcp_tool", own))) == 1
    assert len(sg.parse_errors(rec("ERROR", "mcp.os.posix.utilities", lib))) == 1


def test_plugin_hook_exception(home):
    hooks = kinds(sg.collect(home, {}, "hubhost", NOW), "hook")
    assert len(hooks) == 1
    h = hooks[0]
    assert h["component"] == "plugin" and "pre_llm_call" in h["symptom"] and "on_pre" in h["symptom"]
    assert h["session_id"] == "20261022_080400_bbbbbb"


def test_failed_cron_runs(home):
    crons = kinds(sg.collect(home, {}, "hubhost", NOW), "cron")
    names = sorted(s["symptom"].split("'")[1] for s in crons)
    # a failed run is caught even after the job recovered (sticky last_failure); a month-old one is not
    assert names == ["nightly export", "recovered job"]
    assert all(s["fp"].startswith("cron-") for s in crons)


def test_kanban_cards_failed_twice(home):
    ks = kinds(sg.collect(home, {}, "hubhost", NOW), "kanban")
    assert [s["symptom"].split()[2] for s in ks] == ["t_crash2"]  # once is not enough; self-heal's own cards skip
    assert "crashed" in ks[0]["details"] and "timed_out" in ks[0]["details"]


def test_gateway_crash_restarts(home):
    gw = kinds(sg.collect(home, {}, "hubhost", NOW), "gateway")
    tags = sorted(s["symptom"].split(": ")[1].split()[0] for s in gw)
    # a clean exit and a code-75 restart request are not crashes
    assert tags == ["asyncio.run.exception", "gateway.previous_unclean_exit"]
    assert any("suspected OOM" in s["symptom"] for s in gw)


def test_coach_fixloop_and_handoff_events(home):
    cs = kinds(sg.collect(home, {}, "hubhost", NOW), "coach")
    assert len(cs) == 2  # fixloop_card + the handoff nudge; a confirm nudge is not a handoff
    assert any("claimed fix failed 2x" in s["symptom"] for s in cs)
    assert any("handed the user a step" in s["symptom"] for s in cs)


# ── cursor ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_cursor_sees_each_signal_once_and_reads_appends(home):
    cur = {}
    first = sg.collect(home, cur, "hubhost", NOW)
    assert first
    assert sg.collect(home, cur, "hubhost", NOW) == []
    with (home / "logs" / "errors.log").open("a") as f:
        f.write("2026-10-22 09:00:00,000 ERROR cron.scheduler: tick failed\nTraceback (most recent call last):\n"
                '  File "/home/you/.hermes/hermes-agent/cron/scheduler.py", line 9, in tick\n    x()\n'
                "ZeroDivisionError: division by zero\n")
    again = sg.collect(home, cur, "hubhost", NOW)
    assert [s["component"] for s in again] == ["cron"] and "ZeroDivisionError" in again[0]["symptom"]


def test_cursor_rereads_a_rotated_log(home):
    cur = {}
    sg.collect(home, cur, "hubhost", NOW)
    p = home / "logs" / "errors.log"
    text = p.read_text()
    tmp = p.with_name("errors.log.new")
    tmp.write_text(text)  # written while the old file still exists: a new inode
    tmp.replace(p)
    assert kinds(sg.collect(home, cur, "hubhost", NOW), "errors")


# ── hub sweep / forwarder / ingest ───────────────────────────────────────────────────────────────────────────

def test_sweep_files_each_signal_in_shadow_with_stable_fingerprints(home):
    store = core.Store(home / "coach")
    got = []

    def filer(d):
        got.append(d)
        return sh.file_defect(d, store=store, m="shadow", cfg={}, now=NOW, budget_fn=OPEN)
    res = sg.sweep(home, "hubhost", filer=filer, now=NOW)
    assert {r["signal"]["kind"] for r in res} == {"errors", "hook", "cron", "kanban", "gateway", "coach"}
    assert all(r["result"]["action"] == "would_file" for r in res)
    assert all(d["source"] == "signal" and sh.FP_RE.match(d["fingerprint"]) for d in got)
    logged = [json.loads(x) for x in (home / "coach" / "log.jsonl").read_text().splitlines()]
    wf = [r for r in logged if r.get("event") == "selfheal_would_file" and r.get("source") == "signal"]
    assert len(wf) == len(res) and all(r["args"]["idempotency_key"].startswith("selfheal:") for r in wf)
    # second pass: nothing new
    assert sg.sweep(home, "hubhost", filer=filer, now=NOW) == []


def test_same_traceback_on_two_hosts_is_one_fingerprint(home, tmp_path):
    a = kinds(sg.collect(home, {}, "macbook", NOW), "errors")
    b = kinds(sg.collect(home, {}, "mini", NOW), "errors")
    assert {s["fp"] for s in a} == {s["fp"] for s in b}
    ca, cb = kinds(sg.collect(home, {}, "macbook", NOW), "cron"), kinds(sg.collect(home, {}, "mini", NOW), "cron")
    assert {s["fp"] for s in ca}.isdisjoint({s["fp"] for s in cb})  # per-host objects stay per host


def test_forward_moves_cursor_only_on_success(home):
    calls = []

    def bad(argv, stdin):
        calls.append(argv)
        return SimpleNamespace(returncode=255, stdout="", stderr="ssh: connect to host: timed out")
    r = sg.forward(home, "macbook", "you@hub.example.com", runner=bad, now=NOW)
    assert not r["ok"] and r["pending"] > 0 and not (home / "coach" / "signals-cursor.json").exists()
    sent = []

    def good(argv, stdin):
        sent.append(stdin)
        return SimpleNamespace(returncode=0, stdout='{"ingested": 9}', stderr="")
    r = sg.forward(home, "macbook", "you@hub.example.com", runner=good, now=NOW)
    assert r["ok"] and r["sent"] == r_count(sent[0]) - 1  # + the heartbeat line
    assert json.loads(sent[0].splitlines()[0])["kind"] == "heartbeat"
    assert calls[0][:3] == ["ssh", "-o", "BatchMode=yes"] and calls[0][-1] == sg.HUB_CMD
    assert sg.forward(home, "macbook", "you@hub.example.com", runner=good, now=NOW)["sent"] == 0
    assert r_count(sent[-1]) == 1  # nothing new: the heartbeat still goes, so the hub sees the forwarder alive


def test_forward_pins_the_hub_key(home, tmp_path, monkeypatch):
    # The mini's launchd forward was refused preauth while its shell ssh to the hub worked: pin -i + IdentitiesOnly.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir(exist_ok=True)
    (tmp_path / ".ssh" / "id_ed25519").write_text("x")
    calls = []

    def good(argv, stdin):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"ingested": 1}', stderr="")
    sg.forward(home, "mini", "you@hub.example.com", runner=good, now=NOW, cfg={})
    argv = calls[0]
    assert argv[argv.index("-i") + 1] == str(tmp_path / ".ssh" / "id_ed25519")
    assert "IdentitiesOnly=yes" in argv and argv[-2:] == ["you@hub.example.com", sg.HUB_CMD]
    sg.forward(home, "mini", "you@hub.example.com", runner=good, now=NOW, cfg={"hub_ssh_key": "/k/hub"})
    assert calls[1][calls[1].index("-i") + 1] == "/k/hub"


def test_hub_flags_a_silent_forwarder(tmp_path):
    hub = tmp_path / "hub"
    beat = json.dumps({"kind": "heartbeat", "host": "macbook", "n": 0})
    assert sg.ingest(hub, beat + "\n", now=NOW) == 0  # heartbeats are not spooled
    assert sg.silent_forwarders(hub, now=NOW + 3600) == []
    late = sg.silent_forwarders(hub, now=NOW + 7 * 3600)
    assert [s["host"] for s in late] == ["macbook"] and "has not reported for 7 h" in late[0]["symptom"]
    got = []
    sg.sweep(hub, "hermes-hub", filer=lambda d: got.append(d) or {"action": "would_file"}, now=NOW + 7 * 3600)
    assert [d["host"] for d in got] == ["macbook"] and got[0]["component"] == "plugin"


def r_count(payload):
    return len([x for x in payload.splitlines() if x.strip()])


def test_ingest_then_hub_sweep_files_forwarded_signals(home, tmp_path):
    mac = home
    hub = tmp_path / "hub"
    (hub / "coach").mkdir(parents=True)
    payload = "".join(json.dumps(s) + "\n" for s in sg.collect(mac, {}, "macbook", NOW))
    n = sg.ingest(hub, payload + "not json\n" + json.dumps({"kind": "evil"}) + "\n", now=NOW)
    assert n == r_count(payload)
    got = []
    res = sg.sweep(hub, "hermes-hub", filer=lambda d: got.append(d) or {"action": "would_file"}, now=NOW)
    assert len(res) == n and {d["host"] for d in got} == {"macbook"}
    assert sg.sweep(hub, "hermes-hub", filer=lambda d: {"action": "would_file"}, now=NOW) == []  # spool drained


def test_drain_spool_leaves_a_half_written_last_line_for_the_next_sweep(tmp_path):
    hub = tmp_path / "hub"
    spool = hub / "coach" / "signals" / "spool.jsonl"
    spool.parent.mkdir(parents=True)
    a = {"kind": "cron", "fp": "cron-aaaaaaaaaa", "host": "macbook"}
    b = {"kind": "errors", "fp": "agent-bbbbbbbbbb", "host": "macbook"}
    whole_b = json.dumps(b) + "\n"
    spool.write_text(json.dumps(a) + "\n" + whole_b[:15])  # `ingest` is mid-write of b
    assert [s["fp"] for s in sg.drain_spool(hub)] == ["cron-aaaaaaaaaa"]
    with spool.open("a") as f:  # the write finishes
        f.write(whole_b[15:])
    assert [s["fp"] for s in sg.drain_spool(hub)] == ["agent-bbbbbbbbbb"]
    assert sg.drain_spool(hub) == []


# ── monitors re-routed to the lane ───────────────────────────────────────────────────────────────────────────

MON = {"monitor": "mac-worktree-gc", "key": "worktree-gc-abort:macbook",
       "title": "macbook: worktree GC aborted, cannot read what is live",
       "body": "`mac-worktree-gc.py` (GC) removed nothing and reported nothing: lsof timed out after 600s",
       "prompt": "Find why mac-worktree-gc.py can't read live state, fix the cause."}


def test_monitor_finding_becomes_a_defect_with_its_hint():
    d = sg.monitor_defect(MON, host="macbook")
    assert d["source"] == "signal" and d["kind"] == "monitor" and d["host"] == "macbook"
    assert d["fingerprint"] == sg.monitor_defect(dict(MON, body="other text"), host="mini")["fingerprint"]
    assert "Monitor's own suggested next step" in d["details"] and d["symptom"] == MON["title"]


@pytest.mark.parametrize("res,owned", [
    ({"action": "filed", "card": "t_1"}, True),
    ({"action": "skip", "why": "open: filed t_1"}, True),
    ({"action": "skip", "why": "COACH_SELFHEAL=off"}, False),
    ({"action": "would_file"}, False),
    ({"action": "error", "why": "boom"}, False),
    ({"action": "forwarded", "hub_result": {"action": "filed", "card": "t_2"}}, True),
    ({"action": "forwarded", "hub_result": {"action": "would_file"}}, False),
    ({"action": "forwarded"}, False),
])
def test_lane_owns(res, owned):
    assert sg.lane_owns(res) is owned


def test_route_monitor_live_files_once_then_dedupes(tmp_path):
    store = core.Store(tmp_path / "coach")
    created = []

    def runner(argv, capture_output=True, text=True, input=None, timeout=None):
        if "create" in argv:
            created.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"id": f"t_{len(created):08x}"}), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def filer(d):
        return sh.file_defect(d, store=store, m="live", cfg={}, runner=runner, now=NOW, budget_fn=OPEN)
    a = sg.route_monitor(MON, filer=filer)
    b = sg.route_monitor(MON, filer=filer)
    assert a["owned"] and a["result"]["action"] == "filed" and len(created) == 1
    assert b["owned"] and b["result"]["action"] == "skip"  # same finding tomorrow: still the lane's, no new card


def test_route_monitor_shadow_is_not_owned(tmp_path):
    store = core.Store(tmp_path / "coach")
    r = sg.route_monitor(MON, filer=lambda d: sh.file_defect(d, store=store, m="shadow", cfg={}, now=NOW,
                                                             budget_fn=OPEN))
    assert not r["owned"] and r["result"]["action"] == "would_file"  # shadow: the monitor still posts its card


def test_forward_parses_hub_result():
    out = json.dumps({"action": "filed", "card": "t_9"})
    runner = lambda argv, capture_output=True, text=True, input=None, timeout=None: SimpleNamespace(
        returncode=0, stdout="warning: noise\n" + out + "\n", stderr="")
    store = SimpleNamespace(root=Path("/nonexistent"), log=lambda rec: None)
    r = sh.file_defect(sg.monitor_defect(MON), store=store, m="live", cfg={"hub_ssh": "you@hub.example.com",
                                                                            "hub_host": "elsewhere"},
                       runner=runner, now=NOW)
    assert r["action"] == "forwarded" and r["hub_result"]["card"] == "t_9" and sg.lane_owns(r)
