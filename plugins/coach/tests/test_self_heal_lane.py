"""self-heal lane: every entry path reaches file_defect; dedupe, recurrence, budget, bb routing, shadow vs live,
Inbox outcomes (Do it -> fix card, Not now, Never), digest backstop, queue drain."""
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sh = load("coach_self_heal_lane_test", ROOT / "self_heal.py")
core = load("coach_core_test_lane", ROOT / "core.py")
plugin = load("coach_plugin_test_lane", ROOT / "__init__.py")

NOW = 1792660000.0  # 2026-10-22 ~09:06 UTC
HUB = {}  # no hub configured = this host files locally (the hub itself)
OPEN = lambda board=None, now=None: {"usd": 1.0, "cap": 25.0, "running": 0, "ok_usd": True, "ok_running": True,
                                     "why": ""}


class Runner:
    """Fake subprocess.run: records argv; `hermes kanban ... create` returns a new task id."""

    def __init__(self, outcomes=None, fail=False):
        self.calls, self.n, self.outcomes, self.fail = [], 0, outcomes or {}, fail

    def __call__(self, argv, capture_output=True, text=True, input=None, timeout=None):
        self.calls.append((list(argv), input))
        if self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if "create" in argv:
            self.n += 1
            return SimpleNamespace(returncode=0, stdout=json.dumps({"id": f"t_{self.n:08x}"}), stderr="")
        if argv[:3] == ["hermes", "inbox", "outcome"]:
            key = argv[3]
            o = self.outcomes.get(key)
            return SimpleNamespace(returncode=0 if o else 1, stdout=json.dumps(o or {}), stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def creates(self):
        return [a for a, _ in self.calls if "create" in a]


def opt(argv, flag):
    return argv[argv.index(flag) + 1]


def ev(store, name):
    p = Path(store.root) / "log.jsonl"
    return [r for r in (json.loads(x) for x in p.read_text().splitlines())
            if r.get("event") == name] if p.exists() else []


def defect(component="desktop", symptom="sidebar sessions reshuffle", **kw):
    d = {"component": component, "symptom": symptom, "source": "complaint", "kind": "hermes_bug", "p_defect": 0.9,
         "session_id": "20261022_090000_abcdef", "quote": "i think the sort is broken", "host": "macbook",
         "at": NOW - 60}
    d.update(kw)
    d["fingerprint"] = sh.fingerprint(d["component"], d["symptom"])
    return d


@pytest.fixture
def store(tmp_path):
    return core.Store(tmp_path / "coach")


def judge(p=0.9, component="desktop", symptom="sidebar sessions reshuffle", kind="admission"):
    def llm(system, prompt):
        return json.dumps({"p_defect": p, "kind": kind, "component": component, "symptom": symptom})
    return llm


# ── shadow vs live ──
def test_shadow_logs_would_file_with_full_card_args_and_creates_nothing(store):
    r = Runner()
    out = sh.file_defect(defect(), store=store, m="shadow", runner=r, now=NOW, cfg=HUB, budget_fn=OPEN)
    assert out["action"] == "would_file" and not r.calls
    (w,) = ev(store, "selfheal_would_file")
    a = w["args"]
    assert a["idempotency_key"] == f"selfheal:{w['fingerprint']}" and a["model"] == "claude-opus-5-5"
    assert a["max_runtime"] == 3600 and a["skills"] == ["five-whys", "hermes-mods-repo"]
    assert a["created_by"] == "self-heal" and a["board"] is None
    for part in ("What happened", "Why", "Plan", "Undo", "hermes inbox post --type selfheal", "@session:",
                 "i think the sort is broken", "LIVE", "C10"):
        assert part in a["body"], part
    assert not sh.LaneState(store.root).read()  # shadow leaves no lane state


def test_live_on_hub_creates_the_card_with_the_spec_args(store):
    r = Runner()
    d = defect()
    out = sh.file_defect(d, store=store, m="live", runner=r, now=NOW, cfg=HUB, budget_fn=OPEN)
    (argv,) = r.creates()
    assert out == {"action": "filed", "card": "t_00000001", "key": f"selfheal:{d['fingerprint']}", "scheduled": None}
    assert opt(argv, "--idempotency-key") == f"selfheal:{d['fingerprint']}"
    assert opt(argv, "--model") == "claude-opus-5-5" and opt(argv, "--max-runtime") == "3600"
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--skill"] == ["five-whys", "hermes-mods-repo"]
    assert "--board" not in argv[:4]
    (f,) = ev(store, "selfheal_filed")
    assert f["card"] == "t_00000001" and f["fingerprint"] == d["fingerprint"] and f["recurrence"] is False
    assert sh.LaneState(store.root).get(d["fingerprint"])["status"] == "filed"


def test_live_off_hub_forwards_to_the_hub_over_ssh(store):
    r = Runner()
    cfg = {"hub_ssh": "user@hub.example", "hub_host": "the-hub"}
    out = sh.file_defect(defect(), store=store, m="live", runner=r, now=NOW, cfg=cfg, budget_fn=OPEN)
    ((argv, stdin),) = r.calls
    assert out["action"] == "forwarded" and argv[0] == "ssh" and "user@hub.example" in argv
    assert argv[-1].endswith("self_heal.py file --mode live")
    assert json.loads(stdin)["symptom"] == "sidebar sessions reshuffle"
    assert not r.creates() and not sh.LaneState(store.root).read()  # the hub decides, not this host


def test_hub_ssh_pins_the_key_with_identities_only(store, tmp_path, monkeypatch):
    # launchd jobs offer different default identities than the shell, and the hub refuses them preauth.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert "-i" not in sh.hub_ssh_argv({})  # no default key on disk: plain ssh
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("x")
    argv = sh.hub_ssh_argv({})
    assert argv[argv.index("-i") + 1] == str(tmp_path / ".ssh" / "id_ed25519") and "IdentitiesOnly=yes" in argv
    argv = sh.hub_ssh_argv({"hub_ssh_key": "~/.ssh/hub_key"})
    assert argv[argv.index("-i") + 1] == str(tmp_path / ".ssh" / "hub_key")
    r = Runner()
    sh.file_defect(defect(), store=store, m="live", runner=r, now=NOW,
                   cfg={"hub_ssh": "user@hub.example", "hub_host": "the-hub"}, budget_fn=OPEN)
    ((argv, _),) = r.calls
    assert "IdentitiesOnly=yes" in argv and argv.index("-i") < argv.index("user@hub.example")


def test_hub_cli_file_reads_stdin(store, monkeypatch, capsys):
    monkeypatch.setattr(sh, "file_defect", lambda d, m=None: {"action": "would_file", "fp": d["fingerprint"],
                                                              "m": m})
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(read=lambda: json.dumps(defect())))
    assert sh.main(["file", "--mode", "live"]) == 0
    assert json.loads(capsys.readouterr().out)["m"] == "live"


def test_off_mode_files_nothing(store):
    assert sh.file_defect(defect(), store=store, m="off", runner=Runner(), now=NOW, cfg=HUB)["action"] == "skip"


# ── dedupe ──
def test_one_filing_per_fingerprint_per_day_and_none_while_open(store):
    r = Runner()
    d = defect()
    sh.file_defect(d, store=store, m="live", runner=r, now=NOW, cfg=HUB, budget_fn=OPEN)
    again = sh.file_defect(defect(symptom="sessions in the sidebar reshuffled"), store=store, m="live", runner=r,
                           now=NOW + 60, cfg=HUB, budget_fn=OPEN)
    assert again["action"] == "skip" and "today" in again["why"]
    nextday = sh.file_defect(d, store=store, m="live", runner=r, now=NOW + 86400, cfg=HUB, budget_fn=OPEN)
    assert nextday["action"] == "skip" and nextday["why"].startswith("open: filed")
    assert len(r.creates()) == 1


# ── recurrence ──
def test_recurrence_of_a_fixed_fingerprint_says_previous_fix_didnt_hold(store):
    r = Runner()
    d = defect()
    fp = d["fingerprint"]
    sh.file_defect(d, store=store, m="live", runner=r, now=NOW, cfg=HUB, budget_fn=OPEN)
    sh.record_event("proposed", fp, "t_00000001", store=store, now=NOW + 100)
    sh.file_fix(fp, store=store, runner=r, now=NOW + 200, budget_fn=OPEN)
    sh.record_event("fixed", fp, "t_00000002", store=store, now=NOW + 300)
    out = sh.file_defect(d, store=store, m="live", runner=r, now=NOW + 3 * 86400, cfg=HUB, budget_fn=OPEN)
    argv = r.creates()[-1]
    assert out["key"] == f"selfheal:{fp}:r2" and opt(argv, "--idempotency-key") == f"selfheal:{fp}:r2"
    body = opt(argv, "--body")
    assert "previous fix t_00000002 didn't hold" in body and "detection-level guard" in body
    assert "(came back)" in argv[argv.index("create") + 1]
    (rec,) = ev(store, "selfheal_recurrence")
    assert rec["previous"] == "t_00000002"
    # its fix card gets a distinct key that coach_weekly still maps to the same fingerprint
    sh.record_event("proposed", fp, out["card"], store=store, now=NOW + 3 * 86400 + 10)
    fx = sh.file_fix(fp, store=store, runner=r, now=NOW + 3 * 86400 + 20, budget_fn=OPEN)
    assert fx["key"] == f"selfheal:{fp}:r2:fix"
    weekly = load("coach_weekly_lane_test", REPO / "skills/productivity/hermes-coach/scripts/coach_weekly.py")
    for k in (out["key"], fx["key"], f"selfheal:{fp}:fix", f"selfheal:{fp}"):
        assert weekly.selfheal_fp(k) == fp and weekly.DEFECT_FP_RX.search(weekly.selfheal_fp(k))


# ── budget ──
def test_over_the_dollar_cap_the_card_is_scheduled_for_the_next_utc_day(store):
    r = Runner()
    over = lambda board=None, now=None: {"usd": 25.4, "cap": 25.0, "running": 0, "ok_usd": False,
                                         "ok_running": True, "why": "selfheal lane spend today $25.40 >= $25 cap"}
    out = sh.file_defect(defect(), store=store, m="live", runner=r, now=NOW, cfg=HUB, budget_fn=over)
    sched = [a for a, _ in r.calls if "schedule" in a]
    assert out["scheduled"][0] == "2026-10-23T00:00:00Z"
    assert sched and sched[0][-2] == "t_00000001" and sched[0][-1].startswith("SCHEDULED_UNTIL=2026-10-23T00:00:00Z")


def test_three_running_cards_schedule_the_new_one_an_hour_out(store):
    r = Runner()
    busy = lambda board=None, now=None: {"usd": 3.0, "cap": 25.0, "running": 3, "ok_usd": True,
                                         "ok_running": False, "why": ""}
    out = sh.file_defect(defect(), store=store, m="live", runner=r, now=NOW, cfg=HUB, budget_fn=busy)
    assert out["scheduled"][0] == sh._iso(NOW + 3600)


def test_engine_budget_selfheal_lane_has_its_own_25_cap_outside_the_total(tmp_path):
    eb = load("engine_budget_lane_test", REPO / "skills/productivity/hermes-coach/scripts/engine_budget.py")
    assert eb.CAPS["selfheal"] == 25.0 and "selfheal" in eb.SEPARATE
    snap = {"caps": dict(eb.CAPS), "total_cap": 20.0, "lanes": {"scout": 0, "fixer": 7, "trial": 13, "bot": 0,
                                                                 "selfheal": 10}, "total": 20.0}
    assert eb.check(snap, "selfheal")[0] is True  # engine total is spent, the selfheal lane is not
    assert eb.check(snap, "fixer")[0] is False
    snap["lanes"]["selfheal"] = 25.0
    ok, why = eb.check(snap, "selfheal")
    assert not ok and "selfheal lane" in why
    fx = load("coach_fixer_lane_test", REPO / "skills/productivity/hermes-coach/scripts/coach_fixer.py")
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute("create table tasks (idempotency_key text, created_by text)")
    keys = ["selfheal:desktop-0123456789", "selfheal:desktop-0123456789:fix", "selfheal:1-detector",
            "selfheal-fix:kanban-abcdef0123", "coach-fix:x", "selfheal:agent-0123456789:r2"]
    db.executemany("insert into tasks values (?, 'self-heal')", [(k,) for k in keys])
    got = {k for (k,) in db.execute(f"select idempotency_key from tasks t where {fx.LANE_WHERE['selfheal']}")}
    assert got == set(keys) - {"selfheal:1-detector", "coach-fix:x"}
    fixer = {k for (k,) in db.execute(f"select idempotency_key from tasks t where {fx.LANE_WHERE['fixer']}")}
    assert fixer == {"coach-fix:x"}  # self-heal cards never bill the fixer lane


# ── routing ──
def test_bb_defects_route_to_the_bb_board_with_bb_self_fix(store):
    r = Runner()
    sh.file_defect(defect("bb", "camera enhance shows no sharpening"), store=store, m="live", runner=r, now=NOW,
                   cfg=HUB, budget_fn=OPEN)
    (argv,) = r.creates()
    assert argv[:4] == ["hermes", "kanban", "--board", "bb"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--skill"] == ["five-whys", "bb-self-fix"]
    assert sh.route({"component": "bb"}, {"bb_board": "babybear"})["board"] == "babybear"


# ── entry paths, through the real hooks / sweeps ──
def test_complaint_hook_reaches_file_defect(store, monkeypatch):
    monkeypatch.setenv("COACH_SELFHEAL", "shadow")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    got = []
    _, hooks = plugin.build(store=store, checkout=None, selfheal_sync=True, selfheal_llm=judge(0.9))
    sh_mod = _hooked_self_heal(plugin)
    monkeypatch.setattr(sh_mod, "file_defect", lambda d, store=None, m=None: got.append((d, m)))
    hooks["pre_llm_call"](session_id="s1", user_message="i think the sort is broken")
    hooks["pre_llm_call"](session_id="s2", user_message="the sidebar keeps reshuffling, still broken")
    assert len(got) == 1  # once per fingerprint per day
    d, m = got[0]
    assert m == "shadow" and d["source"] == "complaint" and d["session_id"] == "s1"
    assert d["quote"] == "i think the sort is broken" and d["fingerprint"].startswith("desktop-")


def _hooked_self_heal(plugin_mod):
    """The self_heal module object the plugin's hooks use (loaded by the plugin's own loader)."""
    return plugin_mod._load("self_heal")


def test_admission_hook_reaches_file_defect(store, monkeypatch):
    monkeypatch.setenv("COACH_SELFHEAL", "shadow")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    got = []
    _, hooks = plugin.build(store=store, checkout=None, selfheal_sync=True,
                            selfheal_llm=judge(0.95, "agent", "claimed cards scheduled when none were"))
    monkeypatch.setattr(_hooked_self_heal(plugin), "file_defect", lambda d, store=None, m=None: got.append(d))
    hooks["post_llm_call"](session_id="s1", assistant_response="You're right, that was my mistake: no cards were "
                                                               "scheduled.")
    assert len(got) == 1 and got[0]["source"] == "admission" and got[0]["component"] == "agent"


def test_machine_signal_reaches_file_defect_without_a_judge(store, monkeypatch):
    monkeypatch.setenv("COACH_SELFHEAL", "shadow")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    calls, got = [], []

    def llm(system, prompt):
        calls.append(1)
        return "{}"
    _, hooks = plugin.build(store=store, checkout=None, selfheal_sync=True, selfheal_llm=llm)
    monkeypatch.setattr(_hooked_self_heal(plugin), "file_defect", lambda d, store=None, m=None: got.append(d))
    hooks["post_llm_call"](session_id="s1", assistant_response="Sorry — this message was not processed (API call "
                                                               "failed).")
    assert not calls and len(got) == 1 and got[0]["source"] == "signal" and got[0]["kind"] == "api_error"


def test_direct_machine_signal_caller_files_with_details(store):
    """Card 3's sweeper calls file_defect directly with its own fingerprint and details."""
    r = Runner()
    d = {"component": "cron", "symptom": "cron job x failed 3 runs", "source": "signal", "kind": "cron_failed",
         "details": "exit 1: Traceback ... KeyError 'foo'", "host": "hub", "at": NOW}
    out = sh.file_defect(d, store=store, m="live", runner=r, now=NOW, cfg=HUB, budget_fn=OPEN)
    body = opt(r.creates()[0], "--body")
    assert out["action"] == "filed" and "KeyError 'foo'" in body and "hermes cron list" in body
    assert sh.FP_RE.match(out["key"].split(":", 1)[1])


def write_digest(data, host, sid, turns, source="desktop", **kw):
    p = data / host / "default" / f"{sid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"id": sid, "host": host, "profile": "default", "source": source,
                             "user_turns": [{"t": t, "text": x} for t, x in turns], **kw}))
    return p


def test_digest_backstop_scans_only_new_turns_of_interactive_sessions(store, tmp_path):
    data = tmp_path / "data"
    now = time.time()
    write_digest(data, "macbook", "s1", [(now - 600, "add a login for the room"),
                                         (now - 300, "i think the sort is broken")])
    write_digest(data, "macbook", "old", [(now - 10 * 3600, "still broken!!")])  # before the first-run lookback
    write_digest(data, "macbook", "cron_1", [(now - 100, "still broken")], source="cron")
    write_digest(data, "reports", "x", [(now - 100, "still broken")])
    got = []
    filer = lambda d, store=None, m=None: got.append(d) or {"action": "would_file", "fingerprint": d["fingerprint"]}
    out = sh.backstop(data, store=store, judge=judge(0.9), now=now, filer=filer, m="shadow")
    assert [d["session_id"] for d in got] == ["s1"] and got[0]["source"] == "backstop"
    assert got[0]["quote"] == "add a login for the room" and got[0]["host"] == "macbook" and len(out) == 1
    assert [e["new"] for e in ev(store, "selfheal_detect")] == [True, False]  # same fingerprint: filed once
    assert ev(store, "selfheal_detect")[0]["source"] == "backstop"
    # second pass: nothing new; a new turn in s1 is scanned once
    assert sh.backstop(data, store=store, judge=judge(0.9), now=now + 60, filer=filer, m="shadow") == []
    write_digest(data, "macbook", "s1", [(now - 600, "add a login for the room"),
                                         (now - 300, "i think the sort is broken"),
                                         (now + 30, "the draft got lost again, broken")])
    sh.backstop(data, store=store, judge=judge(0.9, symptom="draft lost on new chat"), now=now + 90, filer=filer,
                m="shadow")
    assert len(got) == 2 and "draft" in got[1]["quote"]


def test_backstop_gives_the_judge_the_prev_reply_and_session_context(store, tmp_path):
    """Digest v6 `prev` (the agent reply before the turn) and the title + earlier user turns reach the judge; without
    them the strict stage read 'why didnt this work [screenshot]' as a new issue (week of 2026-10-12 replay)."""
    data = tmp_path / "data"
    now = time.time()
    p = data / "macbook" / "default" / "s9.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"id": "s9", "host": "macbook", "profile": "default", "source": "desktop",
                             "title": "Fix sidebar drag", "user_turns": [
                                 {"t": now - 600, "text": "dragging sessions into projects does nothing"},
                                 {"t": now - 300, "text": "why didnt this work [screenshot]",
                                  "prev": "Fixed: drag now moves the session. Reload Desktop to pick it up."}]}))
    prompts = []

    def llm(system, prompt):
        prompts.append(prompt)
        return json.dumps({"p_defect": 0.9, "kind": "agent_fault", "component": "desktop", "symptom": "drag fix failed"})
    sh.backstop(data, store=store, judge=llm, now=now, filer=lambda d, store=None, m=None: {"action": "would_file"},
                m="shadow")
    last = [x for x in prompts if "why didnt this work" in x][0]
    assert "Fixed: drag now moves the session" in last and "Session title: Fix sidebar drag" in last
    assert "dragging sessions into projects does nothing" in last
    first = [x for x in prompts if "USER'S NEW MESSAGE:\ndragging" in x][0]
    assert "first user message of the session" in first


def test_queue_drain_files_each_queued_detection_once(store):
    q = Path(store.root) / "selfheal_queue.jsonl"
    q.parent.mkdir(parents=True, exist_ok=True)
    d = defect()
    q.write_text(json.dumps({"event": "selfheal_detect", "fingerprint": d["fingerprint"], "component": "desktop",
                             "symptom": d["symptom"], "source": "complaint", "session_id": "s1", "evidence": "x",
                             "p": 0.9, "ts": NOW}) + "\n")
    got = []
    filer = lambda d, store=None, m=None: got.append(d) or {"action": "filed"}
    sh.drain(store, filer=filer)
    sh.drain(store, filer=filer)
    assert len(got) == 1 and got[0]["fingerprint"] == d["fingerprint"]


# ── Inbox: Do it -> fix card, Not now, Never ──
def proposed(store, r, d=None):
    d = d or defect()
    sh.file_defect(d, store=store, m="live", runner=r, now=NOW, cfg=HUB, budget_fn=OPEN)
    prop = Path(store.root) / "selfheal" / d["fingerprint"] / "proposal.md"
    prop.parent.mkdir(parents=True, exist_ok=True)
    prop.write_text("**What happened** x\n**Why** y\n**Plan** add test_sort_stable; guard\n**Undo** git revert")
    sh.record_event("proposed", d["fingerprint"], "t_00000001", store=store, now=NOW + 100)
    return d["fingerprint"]


def test_do_it_creates_the_fix_card_with_red_green_acceptance(store, monkeypatch):
    r = Runner()
    fp = proposed(store, r)
    monkeypatch.setattr(sh, "coach_root", lambda: Path(store.root))
    out = sh.file_fix(fp, store=store, runner=r, now=NOW + 200, budget_fn=OPEN)
    argv = r.creates()[-1]
    assert out["action"] == "filed" and opt(argv, "--idempotency-key") == f"selfheal:{fp}:fix"
    assert opt(argv, "--model") == "claude-opus-5-5"
    body = opt(argv, "--body")
    for part in ("add test_sort_stable", "FAILING", "PASSING", "Verify live", "--type selfheal-result",
                 f"--key selfheal:{fp}", "Fixed: ", "event fixed"):
        assert part in body, part
    (a,) = ev(store, "selfheal_approved")
    assert a["card"] == out["card"] and a["trigger"] == "do_it"
    # idempotent: a second Do it (or the sync backstop) does not file a second fix card
    assert sh.file_fix(fp, store=store, runner=r, now=NOW + 300, budget_fn=OPEN)["action"] == "skip"
    assert len(r.creates()) == 2


def test_inbox_type_do_it_is_a_resolve_the_hub_cron_acts_on():
    """Do it must not be a Kanban `task` action: a worker is fenced from creating cards via the CLI, so the hub
    cron's sync (outside any worker) files the fix card from the resolved outcome."""
    import yaml
    t = yaml.safe_load((REPO / "skills/productivity/hermes-coach/inbox-types/selfheal.yaml").read_text())
    acts = {a["id"]: a for a in t["actions"]}
    assert set(acts) == {"do_it", "not_now", "never"}
    assert {a["kind"] for a in acts.values()} == {"resolve"}
    r = yaml.safe_load((REPO / "skills/productivity/hermes-coach/inbox-types/selfheal-result.yaml").read_text())
    assert {a["id"] for a in r["actions"]} == {"ok", "undo"}


def test_cron_syncs_every_pass_and_backstops_at_most_every_25_minutes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("COACH_SELFHEAL", "shadow")
    synced, scans = [], []
    monkeypatch.setattr(sh, "sync", lambda store=None, now=None, **kw: synced.append(now) or [("fp", "do_it -> t_1")])
    bfn = lambda data, store=None: scans.append(1) or [{"action": "would_file", "fingerprint": "agent-0123456789"}]
    out = sh.cron(tmp_path, store, now=NOW, backstop_fn=bfn)
    assert out == ["inbox: fp do_it -> t_1", "backstop: would_file agent-0123456789"]
    sh.cron(tmp_path, store, now=NOW + 300, backstop_fn=bfn)
    sh.cron(tmp_path, store, now=NOW + 25 * 60, backstop_fn=bfn)
    assert len(synced) == 3 and len(scans) == 2
    monkeypatch.setenv("COACH_SELFHEAL", "off")
    assert sh.cron(tmp_path, store, now=NOW + 3600, backstop_fn=bfn) == [] and len(synced) == 3


def test_sync_do_it_backstop_not_now_and_never(store):
    r = Runner()
    a = proposed(store, r)
    b = proposed(store, r, defect("kanban", "card parked without reason"))
    c = proposed(store, r, defect("agent", "asked user to run terminal command"))
    r.outcomes = {f"selfheal:{a}": {"state": "resolved", "outcome": "do_it"},
                  f"selfheal:{b}": {"state": "resolved", "outcome": "not_now"},
                  f"selfheal:{c}": {"state": "resolved", "outcome": "never"}}
    fixed = []
    done = dict(sh.sync(store, runner=r, now=NOW + 500,
                        fixer=lambda fp, **kw: fixed.append(fp) or {**sh.file_fix(fp, budget_fn=OPEN, **kw),
                                                                     "card": "t_fix"}))
    assert fixed == [a] and done[a] == "do_it -> t_fix" and done[b] == "not_now" and done[c] == "never"
    assert {e["how"] for e in ev(store, "selfheal_declined")} == {"not_now", "never"}
    # Never suppresses the fingerprint for good; Not now snoozes it 14 days
    r2 = Runner()
    later = NOW + 30 * 86400
    assert sh.file_defect(defect("agent", "asked user to run terminal command"), store=store, m="live", runner=r2,
                          now=later, cfg=HUB, budget_fn=OPEN)["why"] == "suppressed (Never)"
    snoozed = sh.file_defect(defect("kanban", "card parked without reason"), store=store, m="live", runner=r2,
                             now=NOW + 86400, cfg=HUB, budget_fn=OPEN)
    assert snoozed["action"] == "skip" and "snoozed" in snoozed["why"]
    back = sh.file_defect(defect("kanban", "card parked without reason"), store=store, m="live", runner=r2,
                          now=later, cfg=HUB, budget_fn=OPEN)
    assert back["action"] == "filed" and back["key"].endswith(":r2")
    # acted once: a second sync pass does nothing
    assert sh.sync(store, runner=r, now=NOW + 600, fixer=lambda fp, **kw: fixed.append(fp) or {}) == []


def test_not_a_defect_closes_without_inbox_and_is_not_refiled_for_a_week(store):
    r = Runner()
    d = defect()
    sh.file_defect(d, store=store, m="live", runner=r, now=NOW, cfg=HUB, budget_fn=OPEN)
    sh.record_event("not_defect", d["fingerprint"], "t_00000001", note="user setting", store=store, now=NOW + 50)
    assert ev(store, "selfheal_not_defect")[0]["note"] == "user setting"
    out = sh.file_defect(d, store=store, m="live", runner=r, now=NOW + 2 * 86400, cfg=HUB, budget_fn=OPEN)
    assert out["action"] == "skip" and "not a defect" in out["why"]


def test_file_errors_are_logged_and_fail_open(store):
    out = sh.file_defect(defect(), store=store, m="live", runner=Runner(fail=True), now=NOW, cfg=HUB,
                         budget_fn=OPEN)
    assert out["action"] == "error" and ev(store, "selfheal_file_error")
    assert not sh.LaneState(store.root).read()


def test_tests_never_exec_for_real(store):
    with pytest.raises(RuntimeError):
        sh._run(["hermes", "kanban", "create", "x"])


def test_mode_from_env_then_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sh, "coach_root", lambda: tmp_path / "coach")
    monkeypatch.delenv("COACH_SELFHEAL", raising=False)
    assert sh.mode() == "shadow"
    (tmp_path / "coach").mkdir()
    (tmp_path / "coach" / "selfheal-config.json").write_text(json.dumps({"mode": "live"}))
    assert sh.mode() == "live"
    monkeypatch.setenv("COACH_SELFHEAL", "off")
    assert sh.mode() == "off"


def test_investigation_brief_carries_live_replay_lessons():
    """The live-investigator replay (42 incidents) missed roots by stopping at the surface that showed the bad state,
    by trusting the detector's trigger over the user's complaint, and by not naming what the incident host's state
    it could not read. The brief must keep those rules, and the C11/C12/C10 checklists the writer-side checks."""
    d = {"fingerprint": "fp1", "component": "gateway", "symptom": "status chip stuck", "session_id": "s1", "at": NOW}
    b = sh.investigation_brief(d)
    assert "name 2-3 candidate roots" in b
    assert "not the surface that displayed it" in b
    assert "only readable ON that host" in b
    assert "Only after you\n   checked the user's complaint itself" in b
    assert "HERMES_DELEGATED_CHILD_CONTEXT" in b and "reapers" in b
    assert "kanban_notify_subs" in sh.investigation_brief({**d, "component": "kanban"})
    assert "code path that WROTE it" in sh.investigation_brief({**d, "component": "desktop"})


# ── card keys across lifecycles (review round 1) ──
class Board(Runner):
    """Fake board with kanban_db.create_task's idempotency rule: a create whose key matches ANY non-archived card
    (done included) returns that card. `show --json` reports each card's status."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.keys, self.status = {}, {}

    def __call__(self, argv, capture_output=True, text=True, input=None, timeout=None):
        argv = list(argv)
        if "create" in argv:
            self.calls.append((argv, input))
            k = opt(argv, "--idempotency-key")
            tid = self.keys.get(k)
            if not tid or self.status.get(tid) == "archived":
                self.n += 1
                tid = self.keys[k] = f"t_{self.n:08x}"
                self.status[tid] = "ready"
            return SimpleNamespace(returncode=0, stdout=json.dumps({"id": tid}), stderr="")
        if "show" in argv:
            self.calls.append((argv, input))
            tid = argv[argv.index("show") + 1]
            if tid not in self.status:
                return SimpleNamespace(returncode=1, stdout="", stderr=f"no such task: {tid}")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"task": {"id": tid, "status": self.status[tid]}}),
                                   stderr="")
        return super().__call__(argv, capture_output, text, input, timeout)

    def finish(self, tid, status="done"):
        self.status[tid] = status


def lifecycle(store, board, steps, d=None):
    """Run file / fix / <event> steps 9 days apart (past every dedupe/snooze window); finish each card when its
    lifecycle step is reported. Returns the card id of every file/fix step."""
    d = d or defect()
    fp, t, ids = d["fingerprint"], NOW, []
    for s in steps:
        t += 9 * 86400 if s == "file" else 600
        if s == "file":
            r = sh.file_defect(d, store=store, m="live", runner=board, now=t, cfg=HUB, budget_fn=OPEN)
        elif s == "fix":
            r = sh.file_fix(fp, store=store, runner=board, now=t, budget_fn=OPEN)
        else:
            st = sh.LaneState(store.root).get(fp)
            sh.record_event(s, fp, store=store, now=t)
            board.finish(st["fix_card"] if s in ("fixed", "reverted") else st["card"])
            continue
        assert r["action"] == "filed", (s, r)
        ids.append(r["card"])
    return fp, ids


def test_refile_after_a_reverted_fix_gets_a_new_fix_card_on_do_it(store):
    """file -> proposed -> Do it -> reverted -> refile -> proposed -> Do it used to get the DONE first fix card back
    from `selfheal:<fp>:fix`, log selfheal_approved, and run no fix."""
    b = Board()
    fp, ids = lifecycle(store, b, ["file", "proposed", "fix", "reverted", "file", "proposed", "fix"])
    assert len(set(ids)) == 4 and len(b.creates()) == 4, ids
    keys = [opt(a, "--idempotency-key") for a in b.creates()]
    assert keys == [f"selfheal:{fp}", f"selfheal:{fp}:fix", f"selfheal:{fp}:r2", f"selfheal:{fp}:r2:fix"]
    assert sh.LaneState(store.root).get(fp)["fix_card"] == ids[-1]


def test_recurrence_after_a_refile_runs_a_new_previous_fix_didnt_hold_investigation(store):
    """file -> declined -> refile (:r2) -> Do it -> fixed -> recurrence used to reuse `:r2`, get the done refile card
    back, and leave the fingerprint stuck in `filed`."""
    b = Board()
    fp, ids = lifecycle(store, b, ["file", "proposed", "declined", "file", "proposed", "fix", "fixed", "file"])
    assert len(set(ids)) == 4, ids
    last = b.creates()[-1]
    assert opt(last, "--idempotency-key") == f"selfheal:{fp}:r3"
    assert f"previous fix {ids[2]} didn't hold" in opt(last, "--body")
    st = sh.LaneState(store.root).get(fp)
    assert st["status"] == "filed" and st["card"] == ids[3] and st["gen"] == 3
    # and its own Do it gets a fresh fix card too
    sh.record_event("proposed", fp, store=store, now=NOW + 40 * 86400)
    fx = sh.file_fix(fp, store=store, runner=b, now=NOW + 40 * 86400 + 10, budget_fn=OPEN)
    assert fx["key"] == f"selfheal:{fp}:r3:fix" and fx["card"] not in ids
    weekly = load("coach_weekly_gen_test", REPO / "skills/productivity/hermes-coach/scripts/coach_weekly.py")
    assert {weekly.selfheal_fp(opt(a, "--idempotency-key")) for a in b.creates()} == {fp}


def test_a_key_that_returns_an_earlier_card_moves_to_the_next_generation(store):
    """Lane files from before `gen` (or any drift) can name a key whose card exists: never accept it as new."""
    b = Board()
    d = defect()
    fp = d["fingerprint"]
    b.keys[f"selfheal:{fp}:r2"] = "t_0000beef"
    b.status["t_0000beef"] = "done"
    sh.LaneState(store.root).update(fp, status="declined", card="t_0000beef", closed_at=NOW - 30 * 86400)
    out = sh.file_defect(d, store=store, m="live", runner=b, now=NOW, cfg=HUB, budget_fn=OPEN)
    assert out["action"] == "filed" and out["card"] != "t_0000beef" and out["key"] == f"selfheal:{fp}:r3"
    assert ev(store, "selfheal_key_collision")[0]["card"] == "t_0000beef"
    # a fix key that hands back a known card is an error, never an approval
    sh.record_event("proposed", fp, store=store, now=NOW + 10)
    b.keys[f"selfheal:{fp}:r3:fix"] = "t_0000beef"
    fx = sh.file_fix(fp, store=store, runner=b, now=NOW + 20, budget_fn=OPEN)
    assert fx["action"] == "error" and not ev(store, "selfheal_approved")
    assert sh.LaneState(store.root).get(fp)["status"] == "proposed"


def test_sync_ignores_an_earlier_generations_decision_on_the_same_inbox_key(store):
    b = Board()
    fp, ids = lifecycle(store, b, ["file", "proposed", "declined", "file", "proposed"])
    b.outcomes = {f"selfheal:{fp}": {"state": "resolved", "outcome": "do_it", "at": NOW + 600}}  # gen 1's card
    assert sh.sync(store, runner=b, now=NOW + 30 * 86400) == []
    assert len(b.creates()) == 2


# ── a card that ends without reporting (review round 1) ──
@pytest.mark.parametrize("ending", ["done", "archived", "missing"])
def test_sync_marks_a_card_that_ended_without_posting_stale_and_the_next_detection_refiles(store, ending):
    b = Board()
    d = defect()
    fp = d["fingerprint"]
    first = sh.file_defect(d, store=store, m="live", runner=b, now=NOW, cfg=HUB, budget_fn=OPEN)["card"]
    assert sh.sync(store, runner=b, now=NOW + 300) == []  # still running: left alone
    if ending == "missing":
        del b.status[first]
    else:
        b.finish(first, ending)
    done = dict(sh.sync(store, runner=b, now=NOW + 600))
    assert done[fp].startswith("stale (filed")
    (s,) = ev(store, "selfheal_stale")
    assert s["card"] == first and s["was"] == "filed" and s["kanban"] == ending
    again = sh.file_defect(d, store=store, m="live", runner=b, now=NOW + 86400, cfg=HUB, budget_fn=OPEN)
    assert again["action"] == "filed" and again["card"] != first and again["key"] == f"selfheal:{fp}:r2"


def test_an_approved_fix_card_that_ended_without_fixed_goes_stale_and_refiles(store):
    b = Board()
    fp, ids = lifecycle(store, b, ["file", "proposed", "fix"])
    b.finish(ids[1], "archived")
    assert dict(sh.sync(store, runner=b, now=NOW + 20 * 86400))[fp].startswith("stale (approved")
    out = sh.file_defect(defect(), store=store, m="live", runner=b, now=NOW + 21 * 86400, cfg=HUB, budget_fn=OPEN)
    assert out["action"] == "filed" and out["card"] not in ids


def test_unreadable_card_status_never_marks_stale(store):
    r = Runner()  # `show` output is not JSON: status unknown
    sh.file_defect(defect(), store=store, m="live", runner=r, now=NOW, cfg=HUB, budget_fn=OPEN)
    assert sh.reconcile(store, runner=r, now=NOW + 600) == []
    assert sh.reconcile(store, runner=Runner(fail=True), now=NOW + 600) == []
