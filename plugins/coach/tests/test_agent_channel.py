"""Agent channel: each rule fires on its trigger, stays quiet otherwise, and reaches the model via the hooks."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rules = load("coach_rules_test", ROOT / "agent_rules.py")
core = load("coach_core_test_a", ROOT / "core.py")
plugin = load("coach_plugin_test", ROOT / "__init__.py")

FAIL = json.dumps({"output": "boom", "exit_code": 1})
OK = json.dumps({"output": "fine", "exit_code": 0})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def coach(tmp_path, checkout=None, clock=None):
    return rules.AgentCoach(checkout, signals=core.Store(tmp_path / "coach"), clock=clock or Clock())


def term(c, cmd, result=OK, sid="s1", **args):
    c.observe(sid, "terminal", {"command": cmd, **args}, result)


# ── repeat_failure ──
def test_repeat_failure_fires_on_third_identical_failure(tmp_path):
    c = coach(tmp_path)
    term(c, "pytest -q tests", FAIL)
    term(c, "pytest  -q tests", FAIL)  # whitespace-normalized: same command
    assert c.take("s1") is None
    term(c, "pytest -q tests", FAIL)
    hint = c.take("s1")
    assert "failed 3 times" in hint and "hypothesis" in hint and "pytest -q tests" in hint


def test_repeat_failure_resets_on_success_and_fires_once(tmp_path):
    c = coach(tmp_path)
    term(c, "make", FAIL)
    term(c, "make", FAIL)
    term(c, "make", OK)
    term(c, "make", FAIL)
    assert c.take("s1") is None
    for _ in range(5):
        term(c, "make", FAIL)
    assert "failed" in c.take("s1")
    term(c, "make", FAIL)
    assert c.take("s1") is None  # once per key per session


def test_execute_code_and_status_error_count_as_failures(tmp_path):
    c = coach(tmp_path)
    for _ in range(3):
        c.observe("s1", "execute_code", {"code": "print(1/0)"}, json.dumps({"error": "ZeroDivisionError"}))
    assert "failed 3 times" in c.take("s1")


# ── poll_loop ──
def test_sleep_loop_fires_immediately(tmp_path):
    c = coach(tmp_path)
    term(c, "while ! curl -s localhost:8080; do sleep 5; done")
    assert "background=true" in c.take("s1")


def test_three_sleep_polls_in_window_fire(tmp_path):
    c = coach(tmp_path)
    term(c, "sleep 30 && tail log")
    term(c, "ls")
    c.observe("s1", "process_manage", {"action": "poll", "session_id": "p1"}, "{}")
    assert c.take("s1") is None
    term(c, "sleep 60; cat status")
    assert "polling" in c.take("s1")


def test_short_sleep_is_not_polling(tmp_path):
    c = coach(tmp_path)
    for _ in range(5):
        term(c, "sleep 1 && echo hi")
    assert c.take("s1") is None


# ── no_checkpoint ──
def test_forty_tool_calls_in_one_turn(tmp_path):
    c = coach(tmp_path)
    c.turn_start("s1")
    for i in range(39):
        c.observe("s1", "read_file", {"path": f"/x/{i}"}, "{}")
    assert c.take("s1") is None
    c.observe("s1", "read_file", {"path": "/x/40"}, "{}")
    assert "40 tool calls" in c.take("s1")
    c.turn_start("s1")  # a new user turn resets the counter
    for i in range(39):
        c.observe("s1", "read_file", {"path": f"/y/{i}"}, "{}")
    assert c.take("s1") is None


# ── live_checkout ──
def test_live_checkout_edit_and_git_commit(tmp_path):
    live = tmp_path / "hermes-agent"
    (live / "agent").mkdir(parents=True)
    c = coach(tmp_path, checkout=live.resolve())
    c.observe("s1", "patch", {"path": str(tmp_path / "elsewhere.py")}, "{}")
    c.observe("s1", "read_file", {"path": str(live / "agent" / "x.py")}, "{}")
    assert c.take("s1") is None
    c.observe("s1", "write_file", {"path": str(live / "agent" / "x.py")}, "{}")
    assert "hermes-core-wt" in c.take("s1")
    c2 = coach(tmp_path, checkout=live.resolve())
    term(c2, f"cd {live} && git status")
    assert c2.take("s1") is None
    term(c2, f"git -C {live} commit -am wip")
    assert "hermes-core-wt" in c2.take("s1")


def test_live_checkout_hint_ignores_merge_base(tmp_path):
    live = (tmp_path / "hermes-agent"); live.mkdir(); live = live.resolve()
    assert not rules.touches_live_checkout("terminal", {"command": f"cd {live} && git merge-base --is-ancestor abc HEAD"}, live)


def test_live_checkout_ignores_read_only_redirects(tmp_path):
    tlc = rules.touches_live_checkout
    live = (tmp_path / "hermes-agent")
    live.mkdir()
    live = live.resolve()
    scratch = tmp_path / "scratch"
    for cmd in (f"cd {live} && grep -rn foo . 2>/dev/null | head",
                f"cd {live} && git log --oneline -5 2>&1",
                f"cd {live} && git status && echo ok >&2",
                f"cd {live} && git diff > {scratch}/x.diff",
                f"cd {live} && git diff >> {scratch}/x.diff 2>&1",
                f"cd {live} && ls &>/dev/null",
                f"cat {live}/run_agent.py > {scratch}/copy.py"):
        assert tlc("terminal", {"command": cmd}, live) is None, cmd
    # no cd and no workdir: relative redirect target has unknown cwd
    assert tlc("terminal", {"command": f"grep x {live}/a.py > out.txt"}, live) is None


def test_live_checkout_in_tree_writes_still_fire(tmp_path):
    tlc = rules.touches_live_checkout
    live = (tmp_path / "hermes-agent")
    live.mkdir()
    live = live.resolve()
    for cmd in (f"cd {live} && echo x > run_agent.py",
                f"cd {live} && echo x >> agent/notes.txt 2>/dev/null",
                f"echo x > {live}/run_agent.py",
                f"cd /tmp && echo x > {live}/a.py",
                f"cd {live} && sed -i '' s/a/b/ run_agent.py",
                f"cd {live} && git checkout -- run_agent.py",
                f"cd {live} && git commit -am wip 2>&1"):
        assert tlc("terminal", {"command": cmd}, live) == str(live), cmd
    assert tlc("terminal", {"command": "echo x > run_agent.py", "workdir": str(live)}, live) == str(live)
    assert tlc("terminal", {"command": "git diff > x.diff 2>&1", "workdir": str(live)}, live) == str(live)
    assert tlc("terminal", {"command": f"cd {tmp_path} && echo x > y.txt", "workdir": str(live)}, live) is None


def test_live_checkout_ignores_quoted_and_heredoc_text(tmp_path):
    tlc = rules.touches_live_checkout
    live = (tmp_path / "hermes-agent")
    live.mkdir()
    live = live.resolve()
    for cmd in (f"python - <<'EOF'\ncases={{'cd {live} && git checkout -- .':True}}\nEOF",
                f"cat > /tmp/t.py <<EOF\ncd {live} && git commit -am x\necho x > {live}/a.py\nEOF\npython /tmp/t.py",
                f"python -c \"print('cd {live} && git checkout -- .')\"",
                f"echo 'cd {live} && git pull' > /tmp/notes.txt",
                f"git commit -m 'stop touching {live} with git reset'"):
        assert tlc("terminal", {"command": cmd}, live) is None, cmd


def test_live_checkout_attributes_mutation_to_its_own_directory(tmp_path, monkeypatch):
    """2026-10-18 false positive: `git pull` ran in another repo, and the live checkout was only READ later in
    the same command (grep of a file in it). Mutation and live path must belong to the same command."""
    tlc = rules.touches_live_checkout
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    live = home / ".hermes" / "hermes-agent"
    (live / "tools").mkdir(parents=True)
    (home / "code" / "hermes-coach-data").mkdir(parents=True)
    real = ("cd ~/code/hermes-coach-data && git pull -q 2>&1|tail -2; ls research reports | head -50; "
            "python3 - <<'E'\nimport json\nprint(1)\nE\n"
            "grep -nE 'XAI_API_KEY|def check' ~/.hermes/hermes-agent/tools/x_search_tool.py | head -20")
    for cmd in (real,
                f"cd /tmp && git stash; cat {live}/run_agent.py",
                f"git -C {home}/code/hermes-coach-data pull; ls {live}",
                f"cd {home}/code/hermes-coach-data && sed -i '' s/a/b/ notes.md; grep x {live}/a.py",
                f"ls {live}; cd /tmp && npm run build",
                f"cd {live} && git show HEAD:a.ts > $TMPDIR/a.ts",
                f"# the last {live} build ran hermes desktop --build-only\necho done"):
        assert tlc("terminal", {"command": cmd}, live) is None, cmd
    for cmd in (f"cd /tmp && git status; cd {live} && git pull",
                f"cat {live}/a.py; git -C {live} stash",
                f"cd /tmp; sed -i '' s/a/b/ {live}/run_agent.py",
                f"cd {live}/apps/desktop && npm run build",
                f"git -C ~/.hermes/hermes-agent pull"):
        assert tlc("terminal", {"command": cmd}, live) == str(live), cmd


def test_live_checkout_hint_lands_on_the_call_that_caused_it(tmp_path):
    """Core's sequential path runs transform_tool_result BEFORE post_tool_call, so a hint fired from observe()
    was attached to the NEXT tool result (a read-only one). The transform hook must check the call itself."""
    live = (tmp_path / "hermes-agent"); live.mkdir(); live = live.resolve()
    _, hooks = plugin.build(store=core.Store(tmp_path / "coach"), checkout=live)
    bad = {"command": f"cd {live} && git stash"}
    out = hooks["transform_tool_result"](tool_name="terminal", args=bad, result='{"output": ""}', session_id="s1")
    assert out and "LIVE Hermes checkout" in out
    hooks["post_tool_call"](tool_name="terminal", args=bad, result='{"output": ""}', session_id="s1")
    ok = {"command": "ls /tmp"}
    assert hooks["transform_tool_result"](tool_name="terminal", args=ok, result='{"output": ""}', session_id="s1") is None


def test_live_checkout_shell_scripts_and_home_var_still_fire(tmp_path, monkeypatch):
    tlc = rules.touches_live_checkout
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    live = home / ".hermes" / "hermes-agent"
    live.mkdir(parents=True)
    for cmd in ('cd "$HOME/.hermes/hermes-agent" && git checkout -- .',
                "cd ${HOME}/.hermes/hermes-agent && echo x > run_agent.py",
                "cd ~/.hermes/hermes-agent && git pull",
                f"bash -c 'cd {live} && git pull'",
                f"bash <<EOF\ncd {live}\ngit reset --hard\nEOF",
                f"python - <<'EOF'\nprint(1)\nEOF\ncd {live} && git stash"):
        assert tlc("terminal", {"command": cmd}, live) == str(live), cmd
    assert tlc("terminal", {"command": 'cd "$HOME/.hermes/hermes-agent" && git status'}, live) is None


# ── signals for the user channel ──
def test_subagents_long_jobs_and_bug_signatures_are_recorded(tmp_path):
    clk = Clock()
    c = coach(tmp_path, clock=clk)
    st = c.signals
    c.observe("s1", "delegate_task", {"tasks": [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}]}, "{}")
    c.observe("s1", "delegate_task", {"goal": "d"}, "{}")
    assert st.session("s1")["subagents"] == 4
    term(c, "./train.sh", result=json.dumps({"session_id": "proc_1"}), background=True)
    c.observe("s1", "process_manage", {"action": "poll", "session_id": "proc_1"}, "{}")
    assert not st.session("s1").get("long_job")
    clk.t += 601
    c.observe("s1", "process_manage", {"action": "wait", "session_id": "proc_1"}, "{}")
    assert "background job for 10 min" in st.session("s1")["long_job"]
    c.observe("s2", "terminal", {"command": "make all", "timeout": 1800}, OK, duration_ms=3000)
    assert not st.session("s2").get("long_job")  # a big timeout cap alone is not a long job
    c.observe("s2", "terminal", {"command": "make all", "timeout": 1800}, OK, duration_ms=700_000)
    assert "terminal ran 11 min" in st.session("s2")["long_job"]
    c.observe("s3", "cross_session", {"action": "send"}, json.dumps({"error": "chat is open in a window 42"}),
              status="error", error_message="chat is open in a window 42")
    c.observe("s3", "read_file", {"path": "/nope"}, json.dumps({"error": "not found"}), status="error",
              error_message="not found")  # agent mistake, not a Hermes bug
    bugs = st.bugs(0)
    assert len(bugs) == 1 and bugs[0]["sig"] == "cross_session: chat is open in a window <n>"


def test_traceback_from_hermes_code_is_a_bug_signature():
    tb = ('Traceback (most recent call last):\n  File "/Users/me/.hermes/hermes-agent/tools/x.py", line 3, in f\n'
          'KeyError: \'abc\'')
    assert rules.bug_signature("terminal", tb, "ok", None, None) == "terminal: KeyError: 'abc'"
    assert rules.bug_signature("terminal", "Traceback ... File \"/tmp/mine.py\"\nValueError", "error", None, None) is None


def test_memory_guided_refusals_are_agent_mistakes_not_bug_signatures(tmp_path):
    # Real over-budget refusal (session 20261021_103941_fc2813): the agent's next batch call recovered.
    over = {"success": False, "error": "Memory at 1,343/1,375 chars; adding this entry (33 chars) would exceed the "
            "limit by 4 chars. Retry as ONE 'operations' batch that removes or shortens (patch) stale entries ...",
            "current_entries": ["a", "b"], "usage": "1,343/1,375"}
    batch = {"success": False, "error": "Applying all 2 operations would put memory at 1,376/1,375 chars. "
             "No operations were applied (batch is all-or-nothing).", "usage": "1,343/1,375"}
    nomatch = {"success": False, "error": "No entry matched 'Vast idle-box audit'.", "closest_entries": ["x"]}
    scan = {"success": False, "error": "Blocked: content contains invisible unicode character U+200B"}
    for d in (over, batch, nomatch, scan):
        assert rules.bug_signature("memory", json.dumps(d), "error", None, d["error"]) is None, d["error"]
    # Hermes may append repeat-failure guidance after the JSON (seen in state.db); still a guided refusal.
    hinted = json.dumps(nomatch) + "\n\n[This tool call failed the same way twice. Try a different approach.]"
    assert rules.bug_signature("memory", hinted, "error", None, nomatch["error"]) is None
    # Hermes-side memory failures still file: unreadable file, drift, and the exhausted per-turn retry cap.
    unreadable = {"success": False, "error": "Refusing to write MEMORY.md: the file exists on disk but could not "
                  "be read right now (temporarily locked ...)."}
    drift = {"success": False, "error": "Refusing to write MEMORY.md: it holds an entry larger ...",
             "drift_backup": "/h/memories/MEMORY.md.bak"}
    cap = {"success": False, "done": True, "error": "Memory consolidation failed 4 times this turn. Stop retrying"}
    for d in (unreadable, drift, cap):
        assert rules.bug_signature("memory", json.dumps(d), "error", None, d["error"]), d["error"]
    # Other infra tools keep the old rule, and the end-to-end observer records nothing for the refusal.
    assert rules.bug_signature("cross_session", json.dumps(over), "error", None, over["error"])
    c = coach(tmp_path)
    c.observe("s1", "memory", {"action": "add", "content": "x"}, json.dumps(over), status="error",
              error_message=over["error"])
    assert c.signals.bugs(0) == []


# ── delivery through the real hook callbacks ──
def test_hooks_attach_hint_to_tool_result_and_count_turns(tmp_path):
    store = core.Store(tmp_path / "coach")
    _, hooks = plugin.build(store=store, checkout=None)
    assert hooks["pre_llm_call"](session_id="s1", user_message="still not working") is None
    for _ in range(3):
        hooks["post_tool_call"](tool_name="terminal", args={"command": "npm test"}, result=FAIL, session_id="s1",
                                status="error")
        out = hooks["transform_tool_result"](tool_name="terminal", result=FAIL, session_id="s1")
    assert json.loads(out)["coach_hint"].startswith("The same command has now failed 3 times")
    assert hooks["transform_tool_result"](tool_name="terminal", result=FAIL, session_id="s1") is None
    plain = rules.attach_hint("plain text", "do X")
    assert plain.endswith("[coach] do X")
    s = store.session("s1")
    assert s["turns"] == 1 and s["still_broken"] == 1
    log = [json.loads(line) for line in (tmp_path / "coach" / "log.jsonl").read_text().splitlines()]
    assert log[-1]["event"] == "agent_hint" and log[-1]["rule"] == "repeat_failure"


def test_pending_hint_rides_the_next_turn_context(tmp_path):
    _, hooks = plugin.build(store=core.Store(tmp_path / "coach"), checkout=None)
    hooks["post_tool_call"](tool_name="terminal", args={"command": "while true; do sleep 9; done"}, result=OK,
                            session_id="s1")
    ctx = hooks["pre_llm_call"](session_id="s1", user_message="ok")
    assert ctx and ctx["context"].startswith("[coach] You are polling")


def test_still_broken_matcher():
    yes = ["still not working", "same error as before", "it didn't work", "nope, still broken", "doesn't load",
           "didnt work  [Image attached at: /x.png]",
           "[OUT-OF-BAND USER MESSAGE — from the user]\nstill broken\n[/OUT-OF-BAND USER MESSAGE]"]
    no = ["please add a test for login", "works now, thanks", "[IMPORTANT: Background process done] still failing",
          "[System note: Your previous turn was interrupted mid-run] not working", 
          "make it not crash on empty input"]
    assert all(core.is_still_broken(t) for t in yes)
    assert not any(core.is_still_broken(t) for t in no)


def test_plugin_reload_picks_up_edited_rules():
    import os
    path = ROOT / "agent_rules.py"
    a = plugin._load("agent_rules")
    assert plugin._load("agent_rules") is a  # unchanged file: cached
    st = path.stat()
    try:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert plugin._load("agent_rules") is not a  # edited file: re-exec on reload
    finally:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


# ── live_checkout block (pre_tool_call) ──
def test_live_checkout_blocks_head_moves_and_repacks(tmp_path):
    blk = rules.blocks_live_checkout
    live = (tmp_path / "hermes-agent")
    (live / "apps" / "desktop").mkdir(parents=True)
    live = live.resolve()
    for cmd in (f"cd {live} && git commit -am wip",
                f"git -C {live} merge fix/x",
                f"cd {live} && git pull --ff-only",
                f"cd {live} && git cherry-pick abc123",
                f"cd {live}/apps/desktop && npm run pack",
                "hermes update --yes"):
        assert blk("terminal", {"command": cmd}, live), cmd
    assert blk("terminal", {"command": "git commit -m x", "workdir": str(live)}, live)


def test_live_checkout_block_allows_reads_worktrees_and_escape(tmp_path):
    blk = rules.blocks_live_checkout
    live = (tmp_path / "hermes-agent")
    live.mkdir()
    live = live.resolve()
    wt = tmp_path / "_wt" / "slug"
    for cmd in (f"cd {live} && git status && git log -3",
                f"cd {live} && git diff > /tmp/x.diff",
                f"cd {wt} && git commit -am wip",
                f"cd {live} && echo 'git commit is blocked here' ",
                f"cd {live} && cat <<'EOF'\ngit commit -am x\nEOF",
                f"COACH_ALLOW_LIVE=1 git -C {live} pull --ff-only",
                "echo hermes updates are deferred",
                "hermes update --help | head",
                f"cd {live} && git checkout -- hermes_cli/x.py && git status",
                f"cd {live} && git merge-base --is-ancestor abc HEAD && git log -1",
                f"cd {tmp_path}/scratch/pp && U=$(git -C {live} rev-parse upstream/main); git checkout -q -f $U",
                f"cd {live} && WT={tmp_path}/wt && git worktree add -q --detach $WT upstream/main && cd $WT && git am x.patch"):
        assert not blk("terminal", {"command": cmd}, live), cmd
    assert not blk("write_file", {"path": str(live / "x.py")}, live)  # edits stay a post-hoc hint


def test_pre_tool_call_hook_returns_block(tmp_path):
    import importlib.util, sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("coach_pkg_init", Path(__file__).resolve().parents[1] / "__init__.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    core = mod._load("core")
    live = (tmp_path / "hermes-agent"); live.mkdir(); live = live.resolve()
    store = core.Store(root=tmp_path / "coach")
    _, hooks = mod.build(store=store, checkout=live)
    out = hooks["pre_tool_call"](tool_name="terminal", args={"command": f"cd {live} && git commit -am x"})
    assert out and out["action"] == "block" and "hermes-core-wt" in out["message"]
    assert hooks["pre_tool_call"](tool_name="terminal", args={"command": f"cd {live} && git status"}) is None
