"""Keep "pinned" checkouts on their branch: refuse `git checkout|switch` that would move one off it.

Some checkouts are infrastructure, not workspaces: a sync mirror that a launchd job runs scripts from, a deploy
checkout a service serves. An agent that switches one to another branch silently breaks whatever runs from it.
Pins live in ``$HERMES_HOME/branch-pins.json`` ({"<path>": "<branch>"}, ``~`` allowed). Shared by the Hermes
pre_tool_call / transform_tool_result hooks (__init__.py) and the Claude Code PreToolUse hook (claude_hook.py).

Allowed in a pinned checkout: switching TO the pinned branch, file checkouts (`git checkout -- f`, `git checkout
<ref> -- f`, `-p`), and anything outside the pinned path (other worktrees of the same repo included).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

SEPARATORS = re.compile(r"&&|\|\||;|\n|\|")


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def load_pins() -> dict[str, str]:
    """{resolved path: branch}. Missing/broken config → no pins (guard is a no-op)."""
    f = Path(os.environ.get("BRANCH_PIN_GUARD_CONFIG") or hermes_home() / "branch-pins.json")
    try:
        raw = json.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for path, branch in (raw.items() if isinstance(raw, dict) else []):
        if isinstance(path, str) and isinstance(branch, str) and branch:
            out[_resolve(path)] = branch
    return out


def _resolve(p: str, base: Optional[str] = None) -> str:
    p = os.path.expanduser(os.path.expandvars(p))
    if base and not os.path.isabs(p):
        p = os.path.join(base, p)
    return os.path.realpath(p)


def _pinned(dir_: str, pins: dict[str, str]) -> Optional[tuple[str, str]]:
    """(pinned path, branch) if dir_ is the pinned checkout or inside it (but not a nested worktree)."""
    for root, branch in pins.items():
        if dir_ == root or dir_.startswith(root + os.sep):
            top = _toplevel(dir_)
            if top is None or top == root:
                return root, branch
    return None


def _toplevel(d: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def _is_commitish(repo: str, ref: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return True
        # `git checkout foo` also creates foo from a unique origin/foo (DWIM)
        r = subprocess.run(["git", "-C", repo, "for-each-ref", "--format=%(refname)", f"refs/remotes/*/{ref}"],
                           capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return True  # can't tell: assume a branch (fail closed for pinned checkouts only)


def _target(sub: str, args: list[str], repo: str) -> Optional[str]:
    """The branch/commit `git checkout|switch <args>` would move HEAD to; None for a file-only checkout."""
    if sub == "checkout" and ("--" in args or "-p" in args or "--patch" in args):
        return None
    new = {"-b", "-B", "-c", "-C", "--orphan", "--create", "--force-create"}
    for i, a in enumerate(args):
        if a in new:
            return args[i + 1] if i + 1 < len(args) else "<new branch>"
        if a.startswith(("--orphan=", "--create=", "--force-create=")):
            return a.split("=", 1)[1]
    detach = "--detach" in args or "-d" in args and sub == "switch"
    pos = [a for a in args if not a.startswith("-")]
    if not pos:
        return "<detached HEAD>" if detach else None
    ref = pos[0]
    if detach:
        return f"{ref} (detached)"
    if sub == "switch" or ref == "-" or _is_commitish(repo, ref):
        return ref
    return None  # `git checkout <path>`: restores files, HEAD stays


def _git_calls(cmd: str, cwd: str):
    """Yield (repo dir, subcommand, args) for each git invocation, following `cd` and `git -C`."""
    for seg in SEPARATORS.split(cmd or ""):
        try:
            words = shlex.split(seg, comments=True)
        except ValueError:
            words = seg.split()
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):  # FOO=bar git ...
            words = words[1:]
        while words and words[0] in ("sudo", "env", "command", "exec", "time", "nice"):
            words = words[1:]
        if not words:
            continue
        if words[0] in ("cd", "pushd") and len(words) > 1:
            cwd = _resolve(words[1], cwd)
            continue
        if os.path.basename(words[0]) != "git":
            continue
        repo, i = cwd, 1
        while i < len(words) and words[i].startswith("-"):
            if words[i] == "-C" and i + 1 < len(words):
                repo = _resolve(words[i + 1], repo)
                i += 2
            elif words[i] in ("-c", "--git-dir", "--work-tree") and i + 1 < len(words):
                i += 2
            else:
                i += 1
        if i < len(words):
            yield repo, words[i], words[i + 1:]


def check_shell(cmd: str, cwd: Optional[str] = None, pins: Optional[dict[str, str]] = None) -> Optional[str]:
    pins = load_pins() if pins is None else pins
    if not pins or "git" not in (cmd or ""):
        return None
    cwd = _resolve(cwd or os.getcwd())
    for repo, sub, args in _git_calls(cmd, cwd):
        if sub not in ("checkout", "switch"):
            continue
        hit = _pinned(repo, pins)
        if not hit:
            continue
        root, branch = hit
        target = _target(sub, args, root)
        if target is None or target == branch:
            continue
        return f"`git {sub} {' '.join(args)[:60]}` would move {_display(root)} off `{branch}` (to {target})"
    return None


def check_python(code: str, cwd: Optional[str] = None, pins: Optional[dict[str, str]] = None) -> Optional[str]:
    """Shell strings inside Python (os.system / subprocess.run(\"...\", shell=True) / terminal(\"...\"))."""
    pins = load_pins() if pins is None else pins
    if not pins:
        return None
    for lit in re.findall(r"""(?:"([^"\n]*\bgit\b[^"\n]*)"|'([^'\n]*\bgit\b[^'\n]*)')""", code or ""):
        why = check_shell(lit[0] or lit[1], cwd, pins)
        if why:
            return why
    # argv lists: ["git", "-C", "/x", "checkout", "b"]
    for m in re.finditer(r"\[\s*[\"']git[\"'](?:\s*,\s*[\"'][^\"']*[\"'])+\s*\]", code or ""):
        words = re.findall(r"[\"']([^\"']*)[\"']", m.group(0))
        why = check_shell(shlex.join(words), cwd, pins)
        if why:
            return why
    return None


def off_pin(pins: Optional[dict[str, str]] = None) -> list[tuple[str, str, str]]:
    """[(path, pinned branch, current)] for pinned checkouts that are NOT on their branch right now."""
    pins = load_pins() if pins is None else pins
    out = []
    for root, branch in pins.items():
        if not os.path.isdir(root):
            continue
        try:
            r = subprocess.run(["git", "-C", root, "symbolic-ref", "-q", "--short", "HEAD"], capture_output=True,
                               text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        cur = r.stdout.strip() or "detached HEAD"
        if cur != branch:
            out.append((root, branch, cur))
    return out


def _display(p: str) -> str:
    return p.replace(str(Path.home()), "~", 1)


def message(why: str) -> str:
    return (
        f"BRANCH PIN GUARD: blocked: {why}. That checkout is pinned (infrastructure runs from it; see "
        f"{_display(str(hermes_home() / 'branch-pins.json'))}). Do branch work in a separate worktree instead: "
        "`git worktree add <dir> <branch>` (or `git worktree add -b <new> <dir> <base>`), then cd there. "
        "Do not work around this guard."
    )


def drift_note(items: list[tuple[str, str, str]]) -> str:
    lines = [f"{_display(p)} is on `{cur}`, not its pinned branch `{b}`" for p, b, cur in items]
    fix = "; ".join(f"`git -C {_display(p)} checkout {b}`" for p, b, _ in items)
    return ("\n\n[branch-pin-guard] " + "; ".join(lines) + ". Something running from it is now broken. "
            f"If you moved it, commit/stash your work, move it back ({fix}) and continue in a worktree "
            "(`git worktree add <dir> <branch>`).")


def enabled() -> bool:
    return os.environ.get("BRANCH_PIN_GUARD_DISABLE") != "1"
