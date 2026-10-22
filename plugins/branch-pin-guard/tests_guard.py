"""Tests for branch-pin-guard. Run: python3 -m pytest -q -p no:cacheprovider tests_guard.py"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plugin = _load("branch_pin_guard_plugin", "__init__.py")
guard = plugin.guard


def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "mirror"
    repo.mkdir()
    git("init", "-q", "-b", "state", cwd=repo)
    (repo / "f.txt").write_text("x")
    git("add", ".", cwd=repo)
    git("commit", "-qm", "s", cwd=repo)
    git("branch", "main", cwd=repo)
    git("branch", "fix/ci", cwd=repo)
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", str(wt), "main", cwd=repo)
    cfg = tmp_path / "pins.json"
    cfg.write_text(json.dumps({str(repo): "state"}))
    monkeypatch.setenv("BRANCH_PIN_GUARD_CONFIG", str(cfg))
    monkeypatch.delenv("BRANCH_PIN_GUARD_DISABLE", raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    return repo, wt, tmp_path


def blocked(tool, **args):
    return plugin._pre_tool_call(tool_name=tool, args=args) is not None


def test_blocks_moving_pinned_checkout(env):
    repo, _, tmp = env
    for cmd in [f"cd {repo} && git checkout fix/ci",
                f"cd {repo}; git switch main",
                f"git -C {repo} checkout -b fix/new origin/main",
                f"git -C {repo} switch -c x",
                f"cd {repo} && git checkout --detach HEAD",
                f"cd {repo} && git checkout HEAD~0",
                f"cd {tmp} && cd mirror && GIT_TRACE=0 git checkout main"]:
        assert blocked("terminal", command=cmd), cmd
    assert blocked("terminal", command="git checkout fix/ci", workdir=str(repo))
    assert blocked("execute_code", code=f'terminal("cd {repo} && git checkout main")')
    assert blocked("execute_code", code=f'subprocess.run(["git", "-C", "{repo}", "checkout", "main"])')
    assert blocked("tool_call", calls=[{"name": "terminal", "arguments": {"command": f"git -C {repo} switch main"}}])


def test_allows_safe_git(env):
    repo, wt, tmp = env
    for cmd in [f"cd {repo} && git checkout state",
                f"cd {repo} && git switch state",
                f"cd {repo} && git checkout -- f.txt",
                f"cd {repo} && git checkout main -- f.txt",
                f"cd {repo} && git checkout f.txt",
                f"cd {repo} && git worktree add ../x fix/ci",
                f"cd {repo} && git status && git log -1",
                f"cd {wt} && git checkout fix/ci",          # another worktree of the same repo
                f"cd {tmp} && git checkout main",            # not the pinned path
                "echo checkout"]:
        assert not blocked("terminal", command=cmd), cmd


def test_message_points_at_worktree(env):
    repo, _, _ = env
    res = plugin._pre_tool_call(tool_name="terminal", args={"command": f"git -C {repo} checkout main"})
    assert res["action"] == "block"
    assert "git worktree add" in res["message"] and "`state`" in res["message"]


def test_disable_and_no_config(env, monkeypatch):
    repo, _, tmp = env
    monkeypatch.setenv("BRANCH_PIN_GUARD_DISABLE", "1")
    assert not blocked("terminal", command=f"git -C {repo} checkout main")
    monkeypatch.delenv("BRANCH_PIN_GUARD_DISABLE")
    monkeypatch.setenv("BRANCH_PIN_GUARD_CONFIG", str(tmp / "missing.json"))
    assert not blocked("terminal", command=f"git -C {repo} checkout main")


def test_drift_note_after_checkout(env):
    repo, _, _ = env
    args = {"command": "./some-script-that-runs-git-checkout"}
    assert plugin._transform_tool_result(tool_name="terminal", args=args, result="ok") is None
    git("checkout", "-q", "fix/ci", cwd=repo)
    out = plugin._transform_tool_result(tool_name="terminal", args=args, result="ok")
    assert out and "[branch-pin-guard]" in out and "checkout state" in out
    assert plugin._transform_tool_result(tool_name="terminal", args={"command": "ls"}, result="ok") is None
    assert plugin._transform_tool_result(tool_name="terminal", args=args, result=out) is None  # idempotent


def test_claude_hook(env):
    repo, _, _ = env
    def run(cmd, cwd):
        return subprocess.run([sys.executable, str(HERE / "claude_hook.py")], capture_output=True, text=True,
                              input=json.dumps({"tool_input": {"command": cmd}, "cwd": str(cwd)}))
    r = run("git checkout main", repo)
    assert r.returncode == 2 and "BRANCH PIN GUARD" in r.stderr
    assert run("git checkout state", repo).returncode == 0
    assert run("git status", repo).returncode == 0
