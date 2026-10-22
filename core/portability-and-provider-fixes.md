# Small fork fixes: commandcode key, Windows-safe subprocess/pid, contributor map

Patches:

- `patches/0174-chore-map-fork-owner-contributor-email.patch`: maps the fork owner's commit email for the contributor check.
- `patches/0175-fix-providers-commandcode-overlay-keeps-COMMANDCODE_.patch`: commandcode keeps `COMMANDCODE_API_KEY`.
- `patches/0178-fix-subprocess-text-True-needs-encoding-utf-8-errors.patch`: utf-8 subprocess decoding and a psutil pid check.
- `patches/0187-fix-windows-clear-remaining-footguns-utf-8-sig-read-.patch`: more Windows footguns.

## Behavior

- 0174: adds `contributors/emails/<email>` (author handle + comment) so `check-attribution` accepts the fork owner's
  commits. The mirror scrubs the address to `you@example.com`; add your own.
- 0175: models.dev lists the `commandcode` provider with `CMD_API_KEY`, but Hermes' plugin profile reads
  `COMMANDCODE_API_KEY`. A `HERMES_OVERLAYS["commandcode"]` entry (`extra_env_vars=("COMMANDCODE_API_KEY",)`, base URL
  `https://api.commandcode.ai/provider/v1`, `COMMANDCODE_BASE_URL`) keeps the key recognised.
- 0178: subprocess calls that used `text=True` now pass `encoding="utf-8", errors="replace"` (Windows would decode with
  the ANSI codepage and raise on non-ASCII): `kanban_host_identity.boot_prefix`, `review_session_activity._fetch_pr`,
  `top_level_env._parent_pid`, `update_cmd_git._synthesize_upstream_merge`. `top_level_env._pid_alive` uses
  `psutil.pid_exists` when available, falling back to `os.kill(pid, 0)` (marked `# windows-footgun: ok`).
- 0187: `cron_session_activity._read_owners` reads `jobs.json` as `utf-8-sig` (a BOM no longer breaks it),
  `tools/process_registry_checkpoint.py` opens its flock file in binary mode, and the dead duplicate `return` in
  `_pid_alive` is gone.

## How it works

Config/overlay data and keyword arguments only.

## Code

`hermes_cli/providers.py`, `hermes_cli/kanban_host_identity.py`, `hermes_cli/review_session_activity.py`,
`hermes_cli/top_level_env.py`, `hermes_cli/update_cmd_git.py`, `contributors/emails/`.

## Tests

No new tests. 0178 is guarded by the repo's windows-footgun scan; 0175 untested; 0174 exercised by the contributor
check in CI.

## Applying

`git am -3`. Small hunks; if one fails, reapply the described change by hand.

## Gotchas

- 0187 removes the stray duplicate `return True` 0178 left in `_pid_alive`.

## Plugin?

No. Core provider table and helpers.
