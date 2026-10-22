# Top-level launches drop the leaked agent-scope env

Patches:

- `patches/0169-fix-kanban-cards-never-park-forever-without-showing-.patch` (part 5): first version, kanban worker /
  delegate-child identity env only.
- `patches/0171-fix-env-top-level-launches-strip-the-whole-agent-sco.patch`: the whole agent-scope family, one shared
  list.
- `patches/0172-fix-kanban-refusals-and-backend-startup-name-a-leake.patch`: detection (refusals and the startup
  scrub name a leaked env), plus `TERMINAL_CWD`, `HERMES_UI_SESSION_ID`, `HERMES_BROWSER_CONTROL_*` in the list.

## Behavior

A Desktop, `serve`, `dashboard`, `gui`, `desktop` or `gateway` process started from a kanban worker or delegate child
(typically a worker's `hermes update` reopening Hermes.app with `open`) drops the inherited agent-scope env. Before
this, that Hermes.app carried `HERMES_DELEGATED_CHILD_CONTEXT` + `HERMES_KANBAN_*` (and later found
`HERMES_SESSION_ID/SOURCE`, `HERMES_SINGLE_QUERY_SESSION`, `HERMES_SUPERVISED_CHILD`), so every Desktop chat was
fenced as a delegate child and its kanban tools refused, and session identity leaked into every backend.

A process that still carries a leaked marker (one started before the fix) gets a Kanban refusal that says so:
"leaked worker env: this process inherited HERMES_DELEGATED_CHILD_CONTEXT from kanban card t_… but no parent agent is
alive (its worker card t_… is done) … Quit and reopen it", instead of the bare "delegate_task child contexts cannot
mutate Kanban". The startup scrub's warning carries the same text (0172).

Stripping is gated on a worker marker: `HERMES_DELEGATED_CHILD_CONTEXT`, `HERMES_KANBAN_TASK`, `_WORKSPACE`, `_RUN_ID`
or `_CLAIM_LOCK`. Without one nothing is stripped, so a deliberate board pin (`HERMES_KANBAN_BOARD`) in an
operator's shell is untouched.

## How it works

- `agent/delegation_context.py` `strip_agent_scope_env`: removes exact keys `HERMES_DELEGATED_CHILD_CONTEXT`,
  `HERMES_SINGLE_QUERY_SESSION`, `HERMES_SPAWN`, `HERMES_SUPERVISED_CHILD`, `TERMINAL_CWD` (the dispatcher's workspace
  pin), `HERMES_UI_SESSION_ID` and the prefixes `HERMES_KANBAN_`, `HERMES_SESSION_`, `HERMES_CRON_`, `HERMES_PARENT_`,
  `HERMES_RPC_`, `HERMES_KERNEL_`, `HERMES_BROWSER_CONTROL_` (0172 added the last three groups; a test pins the list
  to every `gateway.session_context._VAR_MAP` key). Operator tunables
  (`*_TIMEOUT`, `*_SECONDS`, `*_MAX_PARALLEL`) are kept. It sits next to `scrub_kanban_env`.
- `hermes_cli/top_level_env.py` (new): applied at CLI entry for the top-level subcommands, and `clean_launch_env()`
  builds the env for `main_desktop._relaunch_stopped_desktops` (the post-update `open`). `hermes_cli/main.py` calls it.
- `hermes_cli/top_level_env.py` `leaked_worker_env_diagnosis()` (0172): reads the lineage card (`HERMES_KANBAN_TASK`,
  else the workspace dir name) from the pinned board and calls the marker leaked when that card is not `running`, its
  worker pid is gone, or this process does not descend from it. Silent when the lineage or board can't be read. The
  db fence (`kanban_db._assert_not_delegated_child_mutation`), the CLI fast-fail (`kanban.py`) and the tool refusal
  (`tools/kanban_tools._reject_delegated_child_mutation`) append it when the fence comes from env, not an in-process
  delegate child.
- `apps/desktop/electron/leaked-worker-env.ts`, run at Electron main startup (`main.ts`): the same list in
  TypeScript, logging the keys it dropped. A drift test pins it to the Python list.

## Code

`agent/delegation_context.py`, `hermes_cli/top_level_env.py`, `hermes_cli/main.py`, `hermes_cli/main_desktop.py`,
`apps/desktop/electron/leaked-worker-env.ts`, `apps/desktop/electron/main.ts`, `hermes_cli/kanban_db.py`,
`hermes_cli/kanban.py`, `tools/kanban_tools.py` (0172).

## Tests

- `tests/hermes_cli/test_agent_scope_env.py` (0171): `test_strip_agent_scope_env_removes_whole_family_keeps_tunables`,
  `test_strip_agent_scope_env_is_noop_without_worker_marker`, `test_relaunch_with_poisoned_environ_passes_clean_env`
  (Popen env), `test_backend_started_with_marker_is_not_a_delegate_child`,
  `test_chat_kanban_comment_succeeds_after_top_level_scrub` (end to end), `test_electron_mirror_matches_python_scope_list`.
- `tests/hermes_cli/test_kanban_park_invariants.py` (0169): `test_scrub_removes_leaked_worker_env`,
  `test_scrub_keeps_deliberate_board_pin_without_worker_marker`, `test_main_scrubs_for_top_level_surfaces`,
  `test_desktop_relaunch_after_update_gets_scrubbed_env`.
- `tests/hermes_cli/test_leaked_worker_env.py` (0172): every identity key pid 59331 carried is dropped;
  `test_agent_scope_covers_gateway_session_context`; the post-update relaunch Popen env; a real subprocess per
  surface (`serve`/`dashboard`/`desktop`/`gateway`/`gui`); diagnosis for a done card, a dead worker pid, a live worker
  this process doesn't descend from, silence for a real child and for unknown lineage / unreadable board; the startup
  warning; and the leak named by the db fence, the `kanban_comment` tool and `hermes kanban archive`.
- `apps/desktop/electron/leaked-worker-env.test.ts`: drops a worker identity env; drops the whole family keeping
  tunables (0171); keeps a board pin without a marker.

## Applying

Apply 0169, 0171, then 0172 (0171 rewrites the helper 0169 added). Rebuild Desktop for the Electron half.

## Gotchas

- A worker's subprocesses carry `HERMES_DELEGATED_CHILD_CONTEXT` by design (`delegated_child_subprocess_env`, so
  descendants cannot mutate the board). Any top-level launch from a worker shell therefore needs this scrub; a new
  top-level subcommand must be added to the `top_level_env` list.
- The private hermes-sync tool mirrors the list in its `desktop_launch_env`; keep it in step when the list changes.
- Already-running Desktops started before the fix keep the leaked env until restarted.

## Plugin?

No. Process-launch environment is core startup code.
