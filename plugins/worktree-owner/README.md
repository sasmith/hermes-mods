# worktree-owner

Whoever creates a git worktree owns removing it.

An audit of one Mac found 630 linked worktrees across 20 repos (median age 15 days). Most were made by an agent
(`git worktree add ~/code/<repo>-<slug>`) that finished without `git worktree remove`. Kanban workers were one of
the main sources: the card ended, the dispatcher cleaned only the workspace it had created itself, and nothing knew
the worker's own worktrees belonged to the card.

## What it does

Two `post_tool_call` hooks. Neither one blocks or changes a tool call, and every error is swallowed and logged.

1. **Registration.** After a `terminal` or `execute_code` call whose text contains `worktree add`, the hook finds
   every per-worktree gitdir (`<repo>/.git/worktrees/<name>`) that was born during that call. It looks in repos
   under `~/code` (depth 1 and 2), `~/.hermes/hermes-agent` and the call's own repo. Each one gets a
   `hermes-owner.json` holding the Kanban card id (`HERMES_KANBAN_TASK`), the session id, the profile and the time.
   The marker lives inside git's own per-worktree dir, so it is never committed and it goes away with the worktree.
   Because the hook reads git's state rather than parsing the shell, `for b in ...; do git worktree add ...; done`
   fan-outs and trees nested inside a card's scratch workspace are covered. Attribution guard: a tree is claimed
   only if its directory name appears in the command or code, or, for `terminal` only, a shell loop (`for`, `while`,
   `xargs`, `parallel`) builds the paths from `$` variables. A tree that another agent creates in the same seconds is
   not claimed.
2. **Teardown.** When a Kanban worker calls `kanban_complete` for its own card, the hook goes through every
   worktree registered to that card. It also checks the card's own `dir:` workspace, when that is a linked
   worktree no other open card uses. Each one is removed with `git worktree remove` when it is clean, its HEAD is
   on a remote and no live process has its cwd inside it. Nothing can be lost that way, and the branch is kept.
   Dirty or unpushed trees are left alone, so a backstop GC (for example `mac-worktree-gc --all-repos`) can archive
   and then delete them after 7 idle days. The exception is a dirty tree nested inside the card's own scratch
   workspace (`…/kanban/workspaces/<id>/`), which the dispatcher deletes right after completion. That tree is
   archived first to `~/.hermes/archive/worktrees/<name>-<date>/` (`unpushed.bundle` + `commits/*.patch`,
   `uncommitted.diff`, `untracked.tar.gz`, `meta.json`). Then it is force-removed, so the repo keeps no stale entry.

Log: `~/.hermes/logs/worktree-owner.log` (`REGISTER`, `REMOVED`, `KEEP`, `SKIP` lines).

## Claude Code and Codex (`agent_hook.py`)

The same rule for worktrees made by Claude Code or Codex sessions (and their subagents), as command hooks:

- `PreToolUse` / `PostToolUse` on Bash: a command that mentions `worktree add` gets the same `hermes-owner.json`
  marker, with `agent` (`claude` / `codex`), `agent_session_id`, `cwd` and, inside a Kanban card,
  `task_id` (so the card's `kanban_complete` teardown also applies). The pre-hook stamps the call's start time, so
  only gitdirs born during the call are claimed, with the same attribution guard. Repos searched: `~/code` depth
  1-2, `~/.hermes/hermes-agent`, the session cwd and any `cd <dir>` / `git -C <dir>` in the command.
- `Stop` (at most every 5 min per session) and `SessionEnd`: a detached child removes this session's marked trees
  that are clean, have no live process cwd inside, and are merged: HEAD is an ancestor of `origin/<default>` after a
  fetch, or a merged GitHub PR (`gh pr list --head <branch> --state merged`) has exactly HEAD as its head (squash
  merges, auto-deleted branches). The branch is kept. Unmerged, dirty or busy trees are left to the backstop GC.

`~/.claude/settings.json`:

```json
"PreToolUse":  [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/.hermes/plugins/worktree-owner/agent_hook.py claude", "timeout": 10}]}],
"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/.hermes/plugins/worktree-owner/agent_hook.py claude", "timeout": 30}]}],
"Stop":        [{"hooks": [{"type": "command", "command": "python3 ~/.hermes/plugins/worktree-owner/agent_hook.py claude", "timeout": 10}]}],
"SessionEnd":  [{"hooks": [{"type": "command", "command": "python3 ~/.hermes/plugins/worktree-owner/agent_hook.py claude", "timeout": 10}]}]
```

Codex: the same entries in `~/.codex/hooks.json` with `agent_hook.py codex` (proven with `codex exec`). Codex runs a
new hook only once it is trusted (it records a `trusted_hash` per hook in `config.toml`); until then only `codex exec
--dangerously-bypass-hook-trust` runs it.

Not covered: worktrees made by plain shells or other tools. Those need the backstop GC plus agent instructions
("remove the worktree you made once its branch is merged or abandoned").

## Install

```
ln -s ~/code/hermes-mods/plugins/worktree-owner ~/.hermes/plugins/worktree-owner
hermes plugins enable worktree-owner
```

New sessions and Kanban workers load it. Sessions that are already open keep running without it.

## Tests

```
python3 -B -m unittest discover -s plugins/worktree-owner/tests
```

Stdlib only. The tests use temp git repos and a temp bare remote.
