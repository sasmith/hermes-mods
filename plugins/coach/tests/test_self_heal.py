"""self_heal: complaint (pre_llm_call) and self-admission (post_llm_call) detectors, fingerprint dedupe, modes."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sh = load("coach_self_heal_test", ROOT / "self_heal.py")
core = load("coach_core_test_sh", ROOT / "core.py")
plugin = load("coach_plugin_test_sh", ROOT / "__init__.py")


def judge(p=0.9, component="desktop", symptom="sidebar sessions reshuffle", kind="admission"):
    """Fake judge. kind 'admission' counts on both paths (the audit only files self-admissions)."""
    calls = []

    def llm(system, prompt):
        calls.append((system, prompt))
        return json.dumps({"p_defect": p, "kind": kind, "component": component, "symptom": symptom})
    return llm, calls


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("COACH_SELFHEAL", "shadow")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)


def events(tmp_path, name="selfheal_detect"):
    p = tmp_path / "coach" / "log.jsonl"
    if not p.exists():
        return []
    return [r for r in (json.loads(x) for x in p.read_text().splitlines()) if r.get("event") == name]


# ── prefilters ──
@pytest.mark.parametrize("text", [
    "i think the sort is broken",
    "why are you asking me to open a terminal?",
    "i fixed this once, but now it's broken again",
    "you said that, but i dont see any cards scheduled",
    "fix this [screenshot]",
    "[OUT-OF-BAND USER MESSAGE — x]\nYOU JUST SAID IT WAS CANCELED, WHY DO YOU KEEP ASKING ME\n[/OUT-OF-BAND USER MESSAGE]",
    "it's speaking spanish to me for some reason",
    "you buy the credits",          # a correction with no complaint words (missed by the v1 regex)
    "resume", "whats going on",     # nudges: the judge decides from the previous turn
])
def test_complaint_prefilter_hits(text):
    assert sh.prefilter(text)


@pytest.mark.parametrize("text", [
    "do it", "go", "yes, do it", "thanks!", "/goal ship it",
    "[IMPORTANT: Background process proc_1 exited (exit code 0).]",
    "✔ [default] Kanban t_1 done — shipped",
])
def test_complaint_prefilter_skips(text):
    assert not sh.prefilter(text)


def test_multimodal_message_and_trailers_are_cleaned():
    msg = [{"type": "text", "text": "fix this"}, {"type": "image_url", "image_url": {"url": "data:..."}}]
    assert sh.clean_user(msg) == "fix this [screenshot]" and sh.prefilter(msg)
    assert sh.clean_user("didnt work [screenshot] [user-steps] Steps still waiting on the user") == "didnt work [screenshot]"


def test_admission_prefilter():
    assert sh.admission_prefilter("You're right, and that was my mistake. I skipped the check.")
    assert sh.admission_prefilter("The root cause is a bug in the fallback plugin's import lock.")
    assert sh.admission_prefilter("Run this in your terminal: sudo foo")  # handoff
    assert sh.admission_prefilter("Shipped.", ["-> terminal(make)", "   <- ERROR exit 2"])  # errors in the log
    assert not sh.admission_prefilter("Shipped. Tests pass and the page is live.", ["-> terminal(make)"])


def test_machine_signals_need_no_judge():
    api = sh.machine_signal("Sorry — this message was not processed (API call failed).")
    assert api["source"] == "signal" and api["kind"] == "api_error" and api["component"] == "provider"
    assert sh.machine_signal("ok", ["-> terminal(x)", "Operation interrupted"])["kind"] == "interrupted"
    rep = ["-> terminal(npm test)", "   <- ERROR exit 1: ENOENT"] * 3
    assert sh.machine_signal("done", rep)["kind"] == "repeat_failure"
    assert sh.machine_signal("done", rep[:4]) is None  # two identical errors are not a loop
    llm, calls = judge(0.0)
    assert sh.detect_admission("Sorry, not processed.", llm)["source"] == "signal" and not calls


# ── detect_complaint / detect_admission ──
def test_detect_complaint_returns_schema_and_passes_prev_reply_and_tool_log():
    llm, calls = judge(0.92)
    hit = sh.detect_complaint("Done: sessions are sorted by creation time.", "i think the sort is broken", llm,
                              tool_log=["-> terminal(npm run build)", "   <- ERROR tsc failed"])
    assert set(hit) >= {"p_defect", "component", "symptom", "fingerprint", "source", "kind"}
    assert hit["source"] == "complaint" and hit["component"] == "desktop" and hit["p_defect"] == 0.92
    assert "sorted by creation time" in calls[0][1] and "sort is broken" in calls[0][1]
    assert "ERROR tsc failed" in calls[0][1]
    assert calls[0][0].startswith(sh.COMPLAINT_SYS)


def test_product_bug_kind_is_not_a_self_heal_defect():
    def llm(system, prompt):
        return json.dumps({"p_defect": 0.9, "kind": "external", "component": "other", "symptom": "vendor site down"})
    assert sh.detect_complaint("", "the vendor login page is down", llm) is None


def test_complaint_stage_without_an_answer_is_retried_then_left_out_not_a_zero_vote():
    """Week of 2026-10-12 replay: 'why did you think you were done? create a task to investigate and fix' scored
    [1.0, 0.0] because the strict stage's answer did not parse; counted as 0 it halved a sure catch to 0.5."""
    answers = iter([json.dumps({"p_defect": 1.0, "kind": "agent_fault", "component": "agent",
                                "symptom": "claimed done prematurely"}), "", ""])
    calls = []

    def llm(system, prompt):
        calls.append(system)
        return next(answers)
    hit = sh.detect_complaint("", "why did you think you were done? create a task to investigate and fix", llm)
    assert hit and hit["stages"] == [1.0] and hit["p_defect"] == 1.0
    assert len(calls) == 3 and calls[1].startswith(sh.COMPLAINT_STRICT_SYS)  # strict stage asked twice
    # a strict stage that ANSWERS 0 still votes
    answers2 = iter([json.dumps({"p_defect": 0.9, "kind": "hermes_bug", "component": "bb", "symptom": "x"}),
                     json.dumps({"p_defect": 0.05, "kind": "product_bug", "component": "bb", "symptom": "x"})])
    assert sh.detect_complaint("", "the camera stream is broken", lambda s, p: next(answers2)) is None


def test_complaint_prompt_carries_backstop_context():
    p = sh.complaint_prompt("", "why didnt this work [screenshot]", context="Session title: Fix sidebar drag")
    assert p.startswith("SESSION CONTEXT") and "Fix sidebar drag" in p and "(not available)" in p
    assert "SESSION CONTEXT" not in sh.complaint_prompt("", "x") and "(none: first turn)" in sh.complaint_prompt("", "x")


def test_audit_files_only_self_admissions():
    llm, _ = judge(0.95, kind="needless_handoff")
    assert sh.detect_admission("Please run this in your terminal: make", llm) is None
    llm2, _ = judge(0.95, kind="admission")
    assert sh.detect_admission("That was my mistake, I skipped the test.", llm2)["kind"] == "admission"


def test_complaint_cascade_gate_second_opinion_and_confirm():
    first, c1 = judge(0.8, kind="agent_fault")
    no, c2 = judge(0.2, kind="none")
    # >= GATE: a second flash opinion (strict prompt) and the confirm model; p = mean(0.8, 0.8, 0) < 0.6
    assert sh.detect_complaint("Done.", "that's not what i asked", first, confirm=no) is None
    assert len(c1) == 2 and c1[1][0].startswith(sh.COMPLAINT_STRICT_SYS) and len(c2) == 1
    assert c2[0] == c1[0]  # the confirm model sees the identical prompt
    yes, c3 = judge(0.85, kind="agent_fault", symptom="ignored instruction")
    hit = sh.detect_complaint("Done.", "that's not what i asked", first, confirm=yes)
    assert hit["p_defect"] == round((0.8 + 0.8 + 0.85) / 3, 3) and hit["symptom"] == "ignored instruction"
    assert hit["stages"] == [0.8, 0.8, 0.85]
    low, c4 = judge(0.1, kind="none")
    assert sh.detect_complaint("Done.", "add a toggle", low, confirm=yes) is None
    assert len(c4) == 1 and len(c3) == 1  # below GATE: one cheap call, no escalation
    adm, _ = judge(0.95, kind="admission")
    assert sh.detect_admission("my mistake, I skipped it", adm, confirm=no) is None  # confirm overrules the audit
    assert sh.detect_admission("my mistake, I skipped it", adm, confirm=yes) is None  # confirm kind not admission


def test_confirm_judge_from_env(monkeypatch):
    monkeypatch.delenv("COACH_DEFECT_CONFIRM_MODEL", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)  # no Hermes config importable -> off
    assert sh.confirm_judge() is None
    monkeypatch.setenv("COACH_DEFECT_CONFIRM_MODEL", "m")
    assert callable(sh.confirm_judge())


def test_profile_overlay_is_appended(tmp_path, monkeypatch):
    prof = tmp_path / "reply_profile.md"
    prof.write_text("- Never ask me to run a shell command myself.")
    monkeypatch.setenv("COACH_REPLY_PROFILE", str(prof))
    llm, calls = judge(0.9)
    sh.detect_complaint("Run: sudo x", "you have ssh, do it", llm)
    assert "Never ask me to run a shell command" in calls[0][0]
    monkeypatch.setenv("COACH_REPLY_PROFILE", str(tmp_path / "missing.md"))
    assert sh._with_profile("SYS") == "SYS"


def test_detect_complaint_below_threshold_prefilter_miss_and_judge_error():
    llm, calls = judge(0.4)
    assert sh.detect_complaint("prev", "why did you stop?", llm) is None
    llm2, calls2 = judge(0.99)
    assert sh.detect_complaint("prev", "do it", llm2) is None and not calls2

    def boom(system, prompt):
        raise RuntimeError("provider down")
    assert sh.detect_complaint("prev", "still broken", boom) is None  # fail-open
    assert sh.detect_complaint("prev", "still broken", lambda s, p: "not json") is None


def test_detect_admission():
    llm, calls = judge(0.95, "agent", "asked user for a finished step")
    hit = sh.detect_admission("That was my mistake: I asked you to do something already done.", llm,
                              tool_log=["-> terminal(ls)"], ask="clean up the cards")
    assert hit["source"] == "admission" and hit["component"] == "agent"
    assert calls[0][0].startswith(sh.AUDIT_SYS) and "clean up the cards" in calls[0][1]
    llm2, calls2 = judge(0.99)
    assert sh.detect_admission("All green, deployed.", llm2) is None and not calls2


def test_unknown_component_maps_to_other():
    llm, _ = judge(0.9, component="spaceship")
    assert sh.detect_complaint("", "it crashed again", llm)["component"] == "other"


# ── fingerprint + dedupe ──
def test_fingerprint_stable_across_rewording_and_distinct_otherwise():
    a = sh.fingerprint("desktop", "Sidebar sessions reshuffle")
    b = sh.fingerprint("desktop", "session sidebar reshuffles")
    c = sh.fingerprint("desktop", "draft text lost on new session")
    d = sh.fingerprint("plugin", "sidebar sessions reshuffle")
    assert a == b and a != c and a != d and a.startswith("desktop-")


def test_fingerprints_dedupe_per_utc_day(tmp_path):
    t = [86400 * 100 + 10.0]
    fps = sh.Fingerprints(tmp_path, clock=lambda: t[0])
    hit = {"fingerprint": "desktop-abc", "component": "desktop", "symptom": "x"}
    assert fps.record(hit, "s1") is True
    assert fps.record(hit, "s2") is False  # same day, another session
    t[0] += 86400
    assert fps.record(hit, "s3") is True  # next UTC day files again
    d = json.loads((tmp_path / "selfheal.json").read_text())["desktop-abc"]
    assert d["count"] == 3 and d["sessions"] == ["s1", "s2", "s3"]


# ── through the real hooks (both entry paths) ──
def test_pre_llm_call_complaint_path_logs_shadow_and_dedupes(tmp_path, on):
    llm, calls = judge(0.9)
    store = core.Store(tmp_path / "coach")
    _, hooks = plugin.build(store=store, checkout=None, selfheal_sync=True, selfheal_llm=llm)
    hist = [{"role": "user", "content": "sort by creation"}, {"role": "assistant", "content": "Sorted by creation time."}]
    hooks["pre_llm_call"](session_id="s1", user_message="i think the sort is broken", conversation_history=hist)
    hooks["pre_llm_call"](session_id="s2", user_message="the sidebar keeps reshuffling, still broken")
    ev = events(tmp_path)
    assert [e["source"] for e in ev] == ["complaint", "complaint"]
    assert ev[0]["mode"] == "shadow" and ev[0]["new"] is True and ev[1]["new"] is False
    assert ev[0]["fingerprint"] == ev[1]["fingerprint"]
    assert "Sorted by creation time." in calls[0][1]  # prev reply from conversation_history
    assert not (tmp_path / "coach" / "selfheal_queue.jsonl").exists()  # shadow never enqueues


def test_post_llm_call_admission_path(tmp_path, on):
    llm, calls = judge(0.95, "agent", "claimed cards scheduled when none were")
    store = core.Store(tmp_path / "coach")
    _, hooks = plugin.build(store=store, checkout=None, selfheal_sync=True, selfheal_llm=llm)
    hooks["post_llm_call"](session_id="s1", assistant_response="You're right, that was my mistake: no cards were scheduled.")
    hooks["post_llm_call"](session_id="s1", assistant_response="Done, deployed and verified.")
    ev = events(tmp_path)
    assert len(ev) == 1 and ev[0]["source"] == "admission" and ev[0]["component"] == "agent"


def test_live_mode_enqueues_once_per_fingerprint(tmp_path, on, monkeypatch):
    monkeypatch.setenv("COACH_SELFHEAL", "live")
    llm, _ = judge(0.9)
    _, hooks = plugin.build(store=core.Store(tmp_path / "coach"), checkout=None, selfheal_sync=True, selfheal_llm=llm)
    hooks["pre_llm_call"](session_id="s1", user_message="still broken")
    hooks["pre_llm_call"](session_id="s1", user_message="still broken!!")
    q = (tmp_path / "coach" / "selfheal_queue.jsonl").read_text().splitlines()
    assert len(q) == 1 and json.loads(q[0])["mode"] == "live"


def test_off_mode_subagents_cron_and_kanban_workers_are_skipped(tmp_path, on, monkeypatch):
    llm, calls = judge(0.9)
    _, hooks = plugin.build(store=core.Store(tmp_path / "coach"), checkout=None, selfheal_sync=True, selfheal_llm=llm)
    hooks["pre_llm_call"](session_id="s1", user_message="still broken", platform="cron")
    hooks["pre_llm_call"](session_id="s1", user_message="still broken", parent_session_id="p1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    hooks["pre_llm_call"](session_id="s1", user_message="Fix the dispatcher crash")
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.setenv("COACH_SELFHEAL", "off")
    hooks["pre_llm_call"](session_id="s1", user_message="still broken")
    hooks["post_llm_call"](session_id="s1", assistant_response="that was my mistake")
    assert not calls and events(tmp_path) == []


def test_tool_log_flows_through_hooks_to_both_paths(tmp_path, on):
    llm, calls = judge(0.9)
    store = core.Store(tmp_path / "coach")
    _, hooks = plugin.build(store=store, checkout=None, selfheal_sync=True, selfheal_llm=llm)
    hooks["pre_llm_call"](session_id="s1", user_message="deploy the site")
    hooks["post_tool_call"](tool_name="terminal", args={"command": "wrangler deploy"}, result='{"exit_code": 1}',
                            session_id="s1", status="error", error_message="auth failed")
    n = len(calls)
    hooks["post_llm_call"](session_id="s1", assistant_response="Deployed, the site is live.")
    assert len(calls) == n + 1 and "wrangler deploy" in calls[-1][1] and "deploy the site" in calls[-1][1]
    hooks["pre_llm_call"](session_id="s1", user_message="its not live, you said it was")
    assert "wrangler deploy" in calls[-1][1] and "Deployed, the site is live." in calls[-1][1]
    assert {e["source"] for e in events(tmp_path)} == {"admission", "complaint"}


def test_hook_judge_crash_is_fail_open(tmp_path, on):
    def boom(system, prompt):
        raise RuntimeError("x")
    _, hooks = plugin.build(store=core.Store(tmp_path / "coach"), checkout=None, selfheal_sync=True, selfheal_llm=boom)
    assert hooks["pre_llm_call"](session_id="s1", user_message="still broken") is None
    hooks["post_llm_call"](session_id="s1", assistant_response="my mistake")
    assert events(tmp_path) == []


# ── replay eval (synthetic fixture; the real labeled set is private) ──
def test_incident_replay_scores_like_score3(tmp_path):
    """Episode E1: flagged turns 1-2, explicit fix ask at turn 2 -> only a complaint at turn 1 or an audit before it
    counts unasked. E2: no explicit ask. E3: not a real incident (its turns are negatives). Turn x/0 is a normal turn."""
    ir = load("selfheal_incident_replay_test", ROOT / "eval" / "selfheal_incident_replay.py")
    d = tmp_path / "data"
    d.mkdir()
    turns = [("a", 0, "build it", ""), ("a", 1, "that's not what i asked", "Built X."), ("a", 2, "fix your process", "ok"),
             ("b", 0, "resume", "(error)"), ("c", 0, "why did you do that", "Did Y."), ("x", 0, "add a toggle", "Hi")]
    (d / "turns.jsonl").write_text("".join(json.dumps({"sid": s, "idx": i, "text": t, "prev_reply": p}) + "\n"
                                           for s, i, t, p in turns))
    (d / "turns2.jsonl").write_text("".join(json.dumps({"sid": s, "idx": i, "prev_log": []}) + "\n"
                                            for s, i, _, _ in turns))
    eps = [{"eid": "E1", "sid": "a", "first": 1, "last": 2, "turns": [{"sid": "a", "idx": 1}, {"sid": "a", "idx": 2}]},
           {"eid": "E2", "sid": "b", "first": 0, "last": 0, "turns": [{"sid": "b", "idx": 0}]},
           {"eid": "E3", "sid": "c", "first": 0, "last": 0, "turns": [{"sid": "c", "idx": 0}]}]
    (d / "episodes.json").write_text(json.dumps(eps))
    (d / "fivewhys.jsonl").write_text(json.dumps({"eid": "E1", "real_incident": True, "explicit_fix_ask_turn": 2}) + "\n"
                                      + json.dumps({"eid": "E2", "real_incident": True, "explicit_fix_ask_turn": None})
                                      + "\n" + json.dumps({"eid": "E3", "real_incident": False}) + "\n")
    (d / "fpcheck.jsonl").write_text(json.dumps({"sid": "c", "idx": 0, "real_incident": True}) + "\n")
    hot = {"that's not what i asked": 0.9, "fix your process": 0.95, "why did you do that": 0.8, "add a toggle": 0.7}

    def llm(system, prompt):
        msg = prompt.split("USER'S NEW MESSAGE:\n", 1)[-1]
        p = next((v for k, v in hot.items() if k in msg), 0.1)
        return json.dumps({"p_defect": p, "kind": "agent_fault", "component": "agent", "symptom": msg[:20]})
    out = tmp_path / "out"
    ir.run(type("A", (), {"hermes_root": "", "data": str(d), "out": str(out), "limit": 0, "workers": 2,
                          "tag": "t"})(), llm=llm)
    v = ir.jl(out / "verdicts.jsonl")
    res = ir.score(d, v, 0.65, 0.65, False, relabel=True)
    by = {r["eid"]: r for r in res["episodes"]}
    assert by["E1"]["mode"] == "reactive" and by["E1"]["cut"] == 1  # caught before the fix ask
    assert by["E2"]["mode"] is None and by["E2"]["mode_with_ask"] is None  # "resume" judged 0.1
    assert res["n"] == 2 and res["caught"] == 1 and res["neg_turns"] == 3
    fps = {(f["sid"], f["idx"]): f for f in res["fps"]}
    assert set(fps) == {("c", 0), ("x", 0)} and res["fp_relabeled_real"] == 1 and res["fp_true"] == 1
    # with the fix ask counted, E1 still counts once; an ask-only episode counts only in with_ask
    hot.pop("that's not what i asked")
    ir.run(type("A", (), {"hermes_root": "", "data": str(d), "out": str(tmp_path / "o2"), "limit": 0,
                          "workers": 2, "tag": "t2"})(), llm=llm)
    r2 = ir.score(d, ir.jl(tmp_path / "o2" / "verdicts.jsonl"), 0.65, 0.65, False, relabel=True)
    e1 = next(r for r in r2["episodes"] if r["eid"] == "E1")
    assert e1["mode"] is None and e1["mode_with_ask"] == "fix_ask" and r2["caught_with_ask"] == 1
    ir.write_report(res, d, tmp_path / "r.md", "| baseline | x | y | z |")
    md = (tmp_path / "r.md").read_text()
    assert "Misses (1)" in md and '"resume"' in md and "fpcheck=real" in md and "| baseline |" in md


def test_replay_reports_catch_rate_precision_misses_and_fps(tmp_path):
    replay = load("selfheal_replay_test", ROOT / "eval" / "selfheal_replay.py")
    defects = {"i think the sort is broken": 0.9, "why are you asking me to open a terminal": 0.95,
               "the menu should be shorter": 0.7, "my alarm is not set": 0.2}

    def llm(system, prompt):
        msg = prompt.split("USER'S NEW MESSAGE:\n", 1)[-1]
        p = next((v for k, v in defects.items() if k in msg), 0.1)
        return json.dumps({"p_defect": p, "component": "desktop", "symptom": "x"})
    out = tmp_path / "out"
    replay.main(["--data", str(ROOT / "tests" / "fixtures" / "selfheal_sample.jsonl"), "--out", str(out),
                 "--workers", "2"], llm=llm)
    rep = json.loads((out / "report.json").read_text())
    a = rep["all"]
    assert (a["pos"], a["tp"], a["fn"], a["fp"]) == (4, 2, 2, 1)
    assert a["catch_rate"] == 0.5 and a["precision"] == round(2 / 3, 3)
    assert {m["id"] for m in rep["misses"]} == {"c", "d"} and [f["id"] for f in rep["false_positives"]] == ["g"]
    assert rep["prefilter_only"]["fn"] == 0  # every defect reaches the judge; "do it" never does
    md = (out / "report.md").read_text()
    assert "my alarm is not set" in md and "False positives (1)" in md
    # cached verdicts: a rerun makes no judge calls
    calls = []
    replay.main(["--data", str(ROOT / "tests" / "fixtures" / "selfheal_sample.jsonl"), "--out", str(out)],
                llm=lambda s, p: calls.append(1) or "{}")
    assert not calls
