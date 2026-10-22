# `chat -c <title> --create-if-missing` honors `--source`

Patches:

- `patches/0180-fix-cli-chat-c-title-create-if-missing-honors-source.patch`: `_create_titled_session` labels the
  new row with `--source` (or an inherited automation source) instead of a hardcoded `cli`.

What you see: integration threads that a plugin or script sends into by title (for example the
slack-workspace plugin's `Mac-ops <channel> <thread>` turns, spawned with `--source tool`) no longer show up
in the Desktop sidebar as ordinary user chats.

## Behavior

- `hermes chat -c "<title>" --create-if-missing --source tool ...` creates the missing session with
  `source='tool'`. Upstream wrote `source='cli'` no matter what, so the row listed as a normal chat.
- Source choice for the created row, in order:
  1. `--source <x>` on the command line;
  2. an inherited `HERMES_SESSION_SOURCE` (automation labels such as `tool`, `kanban`, `cron` are inherited on
     purpose). A `tui`/`desktop` label leaked from a parent UI session is ignored unless
     `HERMES_SESSION_SOURCE_EXPLICIT=1`, the same rule as `agent.session_source.session_source_for`;
  3. `cli` (upstream behavior).
- Later sends to the same title resolve the existing row and keep its source: the session upsert
  (`_insert_session_row`) never overwrites an earlier writer's `source`. An explicit `--source` on a resume
  does not relabel an existing row.
- `tool`, `kanban` and `oneshot` rows are already kept out of the Desktop recents
  (`SIDEBAR_EXCLUDED_SOURCES`) and internal listings (`INTERNAL_LISTING_SOURCES`), so the label alone hides
  them; no `sessions.automated_sources` setting is needed.

## How it works

- `hermes_cli/main.py`: new `_titled_session_source(explicit)` picks the label;
  `_create_titled_session(title, source=None)` passes it to `db.create_session`. `_resolve_continue_arg` calls
  it with `getattr(args, "source", None)`, because it runs before `cmd_chat` exports `--source` into
  `HERMES_SESSION_SOURCE`.

## Code

- `hermes_cli/main.py` (`_titled_session_source`, `_create_titled_session`, `_resolve_continue_arg`).

## Tests

`tests/hermes_cli/test_chat_c_fail_loudly.py::TestCreateIfMissingSource`:

- `test_source_flag_labels_created_row`: `--source tool` → row `source='tool'`.
- `test_real_parser_source_flag_reaches_created_row`: the real `chat` parser with
  `-c ... --create-if-missing --source tool -q hi` → `tool`.
- `test_default_stays_cli`: no flag, no env → `cli`.
- `test_inherited_automation_source_is_used`: `HERMES_SESSION_SOURCE=kanban` → `kanban`.
- `test_inherited_ui_source_is_not_used`: `HERMES_SESSION_SOURCE=desktop` (not explicit) → `cli`.
- `test_resume_keeps_tool_source`: a second send resolves the same row, and a later `create_session(...,
  source='cli')` upsert leaves it `tool`.

Run from a plain clone (a `hermes-core-wt` worktree trips the real-home guard):
`scripts/run_tests.sh tests/hermes_cli/test_chat_c_fail_loudly.py`.

## Applying

`git am -3` the patch. It touches only `hermes_cli/main.py` and the test file.

## Gotchas

- Rows created before the patch keep `source='cli'`. Relabel them by hand, after a backup, e.g.
  `update sessions set source='tool' where title like 'Mac-ops %' and source='cli'` in that profile's
  `state.db`. `created_source` stays `cli`; listings read `source`.

## Plugin?

No. The label is written inside core's `-c` resolution before any plugin hook runs. The caller side is a plugin
change (`plugins/slack-workspace/ownerops.py` passes `--source tool`).
