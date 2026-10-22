#!/usr/bin/env python3
"""worktree-owner for Claude Code and Codex: the same owner marker, plus session-end removal of merged trees.

Hook command (stdin = the agent's hook JSON; never blocks, always exits 0):

    python3 <this file> claude     # or: codex

Events:
- PreToolUse (Bash/shell, command mentions `worktree add`): stamp the call's start time.
- PostToolUse (same): every gitdir born since the stamp that the command names (attribution guard of the Hermes
  plugin) gets ``<gitdir>/hermes-owner.json`` with agent, agent_session_id, cwd (+ HERMES_KANBAN_TASK if set).
- Stop (at most every STOP_EVERY_S per session) and SessionEnd: a detached child removes this session's marked
  worktrees that are clean, have no live process cwd inside, and are merged: HEAD is an ancestor of the remote
  default branch (after a fetch), or a merged GitHub PR's head is exactly HEAD (squash merges, deleted branches).
  The branch is kept. Everything else is left for mac-worktree-gc.

Log: ~/.hermes/logs/worktree-owner.log (lines tagged ``agent:<name>``).
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("worktree_owner_base", _HERE / "__init__.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

STATE = Path.home() / ".hermes" / "cache" / "worktree-owner" / "agent-hook"
SLACK_S = 5.0
FALLBACK_WINDOW_S = 600.0  # no PreToolUse stamp (hook added mid-session): look back this far
STOP_EVERY_S = 300.0
END_DELAY_S = 3.0  # let the agent process exit before checking live cwds


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s or "none")[:120]


def _command(payload: dict) -> str:
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return ""
    c = ti.get("command", ti.get("cmd", ""))
    if isinstance(c, list):
        c = " ".join(str(x) for x in c)
    return str(c or "")


def _dirs_named(cmd: str, cwd: str) -> list[str]:
    out = [cwd]
    for m in re.finditer(r"(?:\bcd|\bgit\s+-C)\s+(['\"]?)([^\s;&|'\"]+)\1", cmd):
        p = os.path.expanduser(m.group(2))
        out.append(p if os.path.isabs(p) else os.path.join(cwd or "/", p))
    return out


def _stamp_path(sid: str, tuid: str) -> Path:
    return STATE / f"pre-{_safe(sid)}-{_safe(tuid)}"


def register(payload: dict, agent: str, env) -> list[dict]:
    cmd = _command(payload)
    if not base._mentions_worktree_add(cmd):
        return []
    sid = str(payload.get("session_id") or "")
    stamp = _stamp_path(sid, str(payload.get("tool_use_id") or ""))
    if payload.get("hook_event_name") == "PreToolUse":
        STATE.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()))
        return []
    try:
        since = float(stamp.read_text()) - SLACK_S
        stamp.unlink()
    except (OSError, ValueError):
        since = time.time() - FALLBACK_WINDOW_S
    cwd = str(payload.get("cwd") or "")
    owner = dict(base.owner_from_env(env), agent=agent, agent_session_id=sid or None, cwd=cwd or None)
    owner = {k: v for k, v in owner.items() if v}
    recs = base.register_markers(since, env, _dirs_named(cmd, cwd), cmd, "terminal", owner)
    for r in recs:
        base._log(f"agent:{agent} REGISTER {r['path']} session={sid}")
    return recs


# --- teardown --------------------------------------------------------------------------------------------------------

def merged_pr_heads(path: str, branch: str) -> list[str]:
    """Head oids of merged GitHub PRs from *branch* (empty when gh is missing or the remote isn't GitHub)."""
    gh = shutil.which("gh") or ("/opt/homebrew/bin/gh" if os.path.exists("/opt/homebrew/bin/gh") else None)
    if not gh:
        return []
    try:
        r = subprocess.run([gh, "pr", "list", "--head", branch, "--state", "merged", "--json", "headRefOid",
                            "--limit", "10"], cwd=path, capture_output=True, text=True, timeout=30)
        return [p.get("headRefOid") for p in json.loads(r.stdout or "[]")] if r.returncode == 0 else []
    except Exception:  # noqa: BLE001
        return []


_fetched: dict[str, str | None] = {}


def _default_ref(main: str) -> str | None:
    """origin/<default>, freshly fetched once per repo per run."""
    if main in _fetched:
        return _fetched[main]
    ref = base._git(main, "rev-parse", "--abbrev-ref", "origin/HEAD", timeout=20).stdout.strip()
    if not ref or ref == "origin/HEAD":
        ref = next((c for c in ("origin/main", "origin/master")
                    if base._git(main, "rev-parse", "-q", "--verify", c, timeout=20).returncode == 0), None)
    if ref:
        base._git(main, "fetch", "-q", "origin", ref.split("/", 1)[1], timeout=120)
    _fetched[main] = ref
    return ref


def not_merged_reason(path: str, main: str) -> str | None:
    st = base._git(path, "--no-optional-locks", "status", "--porcelain", timeout=300)
    if st.returncode != 0:
        return "git status failed"
    if st.stdout.strip():
        return "uncommitted/untracked changes"
    head = base._git(path, "rev-parse", "HEAD").stdout.strip()
    ref = _default_ref(main)
    if ref and base._git(main, "merge-base", "--is-ancestor", head, ref).returncode == 0:
        return None
    branch = base._git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch and branch != "HEAD" and head in merged_pr_heads(path, branch):
        return None
    return f"not merged into {ref or 'the default branch'} and no merged PR at HEAD"


def remove_if_merged(path: str, why: str) -> dict:
    rp = os.path.realpath(path)
    if any(c == rp or c.startswith(rp + "/") or c == path or c.startswith(path + "/") for c in base.live_cwds()):
        base._log(f"KEEP {path} ({why}): a live process works in it")
        return dict(path=path, action="kept: live process")
    common = base._git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    main = os.path.dirname(common) if common else path
    keep = not_merged_reason(path, main)
    if keep:
        base._log(f"KEEP {path} ({why}): {keep}; mac-worktree-gc handles it after 7 idle days")
        return dict(path=path, action=f"kept: {keep}")
    r = base._git(main, "worktree", "remove", path, timeout=600)
    if r.returncode != 0:
        base._log(f"WARN remove {path} failed: {r.stderr.strip()[:200]}")
        return dict(path=path, action=f"kept: remove failed: {r.stderr.strip()[:120]}")
    base._log(f"REMOVED {path} ({why})")
    return dict(path=path, action="removed")


def teardown(sid: str, agent: str, cwd: str = "") -> list[dict]:
    """Serialised per session (Stop and SessionEnd children can overlap) via an flock."""
    import fcntl
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / f"lock-{_safe(agent)}-{_safe(sid)}", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _teardown(sid, agent, cwd)


def _teardown(sid: str, agent: str, cwd: str = "") -> list[dict]:
    _fetched.clear()
    out = []
    for common in base._common_dirs([cwd]):
        for gd in glob.glob(os.path.join(common, "worktrees", "*")):
            try:
                rec = json.loads(Path(gd, base.MARKER).read_text())
            except (OSError, ValueError):
                continue
            path = rec.get("path") or base._wt_path(gd)
            if rec.get("agent") == agent and rec.get("agent_session_id") == sid and path and os.path.isdir(path):
                out.append(remove_if_merged(path, f"made by {agent} session {sid}"))
    return out


def end(payload: dict, agent: str, detach: bool = True) -> list[dict]:
    sid = str(payload.get("session_id") or "")
    if not sid:
        return []
    if payload.get("hook_event_name") == "Stop":
        mark = STATE / f"stop-{_safe(agent)}-{_safe(sid)}"
        try:
            if time.time() - mark.stat().st_mtime < STOP_EVERY_S:
                return []
        except OSError:
            pass
        STATE.mkdir(parents=True, exist_ok=True)
        mark.touch()
    cwd = str(payload.get("cwd") or "")
    if detach:  # never hold the agent up (and outlive its exit at SessionEnd)
        delay = END_DELAY_S if payload.get("hook_event_name") == "SessionEnd" else 0
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--teardown", agent, sid, cwd, str(delay)],
                         cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return []
    return teardown(sid, agent, cwd)


def handle(payload, agent: str, env=None, detach: bool = True) -> list[dict]:
    env = os.environ if env is None else env
    try:
        if not isinstance(payload, dict):
            return []
        ev = payload.get("hook_event_name")
        if ev in ("PreToolUse", "PostToolUse"):
            return register(payload, agent, env)
        if ev in ("Stop", "SessionEnd"):
            return end(payload, agent, detach)
    except Exception as e:  # noqa: BLE001 - a hook must never break the agent
        base._log(f"agent:{agent} WARN hook failed: {type(e).__name__}: {e}")
    return []


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[1] == "--teardown":
        _, _, agent, sid, cwd, *rest = argv
        time.sleep(float(rest[0]) if rest else 0)
        try:
            res = teardown(sid, agent, cwd)
            base._log(f"agent:{agent} teardown session={sid}: {len(res)} marked tree(s), "
                      f"{sum(r['action'] == 'removed' for r in res)} removed")
        except Exception as e:  # noqa: BLE001
            base._log(f"agent:{agent} WARN teardown failed: {type(e).__name__}: {e}")
        return 0
    agent = argv[1] if len(argv) > 1 else "claude"
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0
    handle(payload, agent)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
