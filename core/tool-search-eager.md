# Keep chosen tools out of the tool_search bridge, plugin tools included (and forward bridge calls to them)

Patches:

- `patches/0114-feat-tool_search-tools.tool_search.eager-keeps-named.patch`: tools. New config key
  `tools.tool_search.eager` lists tools that are always sent with their full schema, never deferred.
- `patches/0120-fix-tool-search-forward-tool_call-to-directly-listed.patch`: tools. New config key
  `tools.tool_search.forward_direct_calls` (default `false`): a `tool_call` that names a directly-listed tool runs
  it instead of refusing.
- `patches/0168-fix-tool_search-tool_describe-names-the-disabled-too.patch`: tools. `tool_describe` on a registered
  direct tool that is missing from this session (its toolset is off for the platform) says so and names the fix,
  instead of "directly-listed tool ... call it directly".

What you see: tools you use all the time (for example `process_manage`, or a plugin tool like `cross_session`) are
in the agent's tool list directly. Without this the agent sees only their names in the `tool_search` catalog and
has to search/describe them first, or misses them. Stock Hermes can make a core tool eager by dropping it from
`tools.tool_search.defer`, but plugin and MCP tools always defer and no setting changes that.

## Behavior

- **Default `[]`**: upstream behavior.
- A name in `eager` is never deferrable: it is not deferred even when it is a plugin/MCP tool, and it wins over a
  `defer` entry for the same name.
- An eager tool is a direct tool. With `forward_direct_calls: false` (upstream behavior) the bridge refuses it like
  any core tool (`tool_call` returns "'X' is a directly-listed tool ... The tool was NOT invoked") and it drops out
  of the session's deferrable scope.
- **`forward_direct_calls: true` (0120)**: `tool_call` naming any registered direct tool runs it. This matters
  because the deferred/direct split is read from the live config at dispatch: moving a tool from `defer` to `eager`
  mid-session (for example via a synced shared config) otherwise turns every later `tool_call` to it, the shape the
  session's own history shows working, into a refusal and a wasted round trip. Session toolset scope and the
  schema check (missing required arguments) still apply; bridge tools (`tool_search`, `tool_call`,
  `tool_describe`) and unknown names are still refused. Accepts `true/1/yes/on` and `false/0/no/off`; anything
  else → `false`.
- A non-list value logs a warning and is ignored.
- **Disabled toolset (0168)**: `tool_describe` of a registered, non-deferrable tool that is not in the session's
  tool list returns "'X' is registered but NOT enabled in this session: its toolset 'T' is off for this platform.
  Enable it with `hermes tools enable T --platform cli` ... then start a new session." The old answer ("directly-listed
  tool, call it directly") sent an agent looking for a tool it did not have: on a host whose `cli` platform never
  opted into the default-off `kanban` toolset, `kanban_create` was missing and the agent gave up on filing a card.
  A tool that IS in the session still gets the "directly-listed" answer.

## How it works

- `tools/tool_search.py`: `ToolSearchConfig.eager_tools` (parsed from `eager`); `is_deferrable_tool_name(name,
  defer_tools, eager_tools)` returns False first for eager names; `classify_tools` takes `eager_tools`; every call
  site (`_deferrable_in`, `assemble_tool_defs`, `dispatch_tool_describe`, `scoped_deferrable_names`,
  `resolve_underlying_call`) passes the config's `eager_tools`.
- `hermes_cli/config_defaults.py`: adds `tools.tool_search.eager: []` and (0120)
  `tools.tool_search.forward_direct_calls: False`, with comments.
- 0120, `tools/tool_search.py`: `ToolSearchConfig.forward_direct_calls` (parsed by `_safe_bool`).
  `resolve_underlying_call` returns `(name, args, None)` for a registered direct tool instead of the refusal, and
  `scoped_deferrable_names` admits every non-bridge tool in the session's scoped defs, so both scope gates (the
  `model_tools` bridge and the agent loop's `agent.tool_executor._unwrap_tool_search_call`) still decide whether
  the session may run it.

## Tests

`tests/tools/test_tool_search.py`, class `TestEagerTools`:

- `test_eager_config_parses_and_defaults_empty`: default empty, list parsing/stripping, scalar warned and ignored.
- `test_plugin_tool_defers_unless_eager`: a registered plugin tool defers by default (control) and not when eager.
- `test_eager_wins_over_defer_for_core_tool`: `process_manage` in both lists stays direct.
- `test_assembly_keeps_eager_plugin_tool_visible`: `assemble_tool_defs` keeps the eager plugin tool and defers the
  other one.
- `test_eager_tool_is_refused_by_tool_call_bridge`: `resolve_underlying_call` refuses it and
  `scoped_deferrable_names` excludes it.

`tests/tools/test_tool_search_forward_direct.py` (0120):

- `test_default_is_upstream_refusal_after_mid_session_eager_flip`: default off, a mid-session flip to eager refuses
  (control for the old behavior).
- `test_forward_runs_directly_listed_tool_through_bridge`: with the flag on, `handle_function_call("tool_call", ...)`
  runs the tool (model_tools entry path).
- `test_forward_unwraps_in_agent_loop_executor`: the agent-loop entry path forwards in scope and blocks with "not
  available in this session" out of scope.
- `test_forward_still_enforces_session_scope_and_schema`: out-of-scope and missing-required-argument calls still fail.
- `test_forward_never_admits_bridge_or_unknown_names`.
- `test_forward_config_parses_and_defaults_off`.

`tests/tools/test_tool_describe_gated_toolset.py` (0168):

- `test_describe_absent_from_session_names_the_toolset_and_fix`: registered eager tool, empty session defs → the
  "NOT enabled" error with the `hermes tools enable <toolset> --platform cli` command. Fails before 0168.
- `test_describe_present_in_session_still_says_call_directly`: same tool in the session's defs → "directly-listed".

Both pass, plus the 127 other tests in the tool_search/tool_call files (Oct 21, 2026 PT, `scripts/run_tests.sh`).
The `tool_call` path is unchanged (forwarding or the session-scope gate handles it): untested for this message.

All 6 pass (Oct 17, 2026 PT, `scripts/run_tests.sh`). The real symptom (a live config edit while a session runs) is
reproduced by swapping `load_config_readonly` in the test, not by editing a config file under a running session:
untested end to end.

Live check for 0114 (not an automated test): a `hermes chat -q` session run from this commit with
`eager: [process_manage, drive_preview, cross_session]` reported all of process_manage, todo_list and
cross_session as direct; on the previous core cross_session was deferred.

## Applying

0114 is standalone; it touches only `tools/tool_search.py`, its test file and `config_defaults.py`. 0120 edits the
same functions and needs 0114 (its tests use `eager`). Verified: 0001–0127 apply with `git am -3` at merge-base
`4957bbd0a0` (Oct 17, 2026 PT).

## Gotchas

- Each eager tool's schema is sent on every turn; keep the list to tools used in most sessions.
- Eager only changes deferral. The tool must still be in the session's toolsets (e.g. `drive_preview` exists only
  in Desktop sessions).
- Changing `defer`/`eager` while sessions run breaks their bridge calls unless `forward_direct_calls` is on; turn
  it on together with any `eager` list you sync between hosts.
- Hosts not on the fork core ignore both keys.

## Plugin?

No. Deferral is decided in core `is_deferrable_tool_name` and the bridge refusal in `resolve_underlying_call`; no
plugin hook can change either.
