# run_in_user_terminal from a gateway with no screen

## Behavior

On a Linux/cloud gateway (no `osascript`, no cmux) `run_in_user_terminal` is still registered with the same
schema. Calling it opens the usual window on the owner's Mac, runs the command there (right away by default; after `y` when
`user_terminal.confirm` is `remote` or `all`), and
returns status / exit code / output to the remote agent, with the same `pending` + `job_id` resume semantics.

Invariants (each has a test in `tests_user_terminal.py` or `../session-mentions/tests/user-terminal.test.mjs`):

1. With `user_terminal.confirm: remote|all`, nothing runs without the owner typing `y` in the Mac window
   (default `off`: it runs at once, by the owner's choice). The relayed path only ever calls
   `create_job` + `open_window`; `runner.sh` declines on Enter / `n`.
2. Only the owner's own sessions may call it: every identity the turn has (`HERMES_PLATFORM`,
   session platform/source, the session row's `source`) must be one of `desktop`, `api_server`, `tui`, `cli`,
   `local`. Slack (a shared bot such as a team profile), other messaging platforms, cron and "unknown" are refused
   with a message telling the agent to say it needs the owner, not to hand the command over. The Desktop courier
   checks it a second time.
3. The window says where the request came from: `From another machine: Gateway <gw> · profile <p> · session <id>
   ("<title>")`, and the window title is `Hermes (<gw>/<p>) wants to run a command`. The gateway name is the
   courier's own name for the connection it drained, not a value the remote agent chose. Control characters in the
   command, reason, cwd and origin are shown as `^[`-style text, so a relayed command cannot redraw the window.
   The refusal is by surface, not by person, and stays for the owner's own Slack DM too (arbitrary shell still needs
   the y-window). For that one case, detected from Slack-verified session fields (`owner.slack_user_ids` +
   DM), the refusal text changes: it tells the agent to call `mac_ops_handoff`, which passes a machine problem to
   the owner-only `mac-ops` profile (plugins `slack-workspace` `ownerops.py` + `mac-ops`), and never to hand over
   commands, paste-it steps or keyboard shortcuts. Channels and other users get the old text. Test:
   `test_owner_dm_refusal_routes_to_ops_not_paste_it`.
4. No Desktop courier for that Mac online -> the tool errors at once (no outbox write). A live courier that does
   not claim the request within 15 s -> error and the request is withdrawn.

## How it works

```
remote agent ── run_in_user_terminal ──> <root>/session_mentions/outbox/<id>.json   {action: user_terminal, route: <mac>}
Mac Desktop courier (session-mentions plugin.js) drains it (only the courier whose device == route may claim)
   └─> its OWN backend: POST /api/plugins/user-terminal/start  {command, reason, cwd, origin}
                        GET  /api/plugins/user-terminal/status/<job>?gateway=<gw>&wait_s<=25
   └─> POST result back to the remote gateway's /api/plugins/session-mentions/result
remote agent polls results/<id>.json; one start request, then status requests of ≤20 s until done or wait_s.
```

- Target Mac: config `user_terminal.device` (or env `HERMES_USER_TERMINAL_DEVICE`): the Mac's
  `session_mentions.device_name` or any alias (hostname). Unset -> the only live courier; several -> error asking to
  set it.
- A relayed job is readable only with the gateway name that started it, and local (non-relayed) jobs never.
- The Mac's backend mounts the new `dashboard/` routes on first request (hot mount) on a fork core; an older core
  needs one backend restart.

## Setup on a gateway

1. Install + enable `user-terminal` (and `session-mentions`, whose dashboard holds the outbox) on the gateway, and in
   every profile that should have the tool (`plugins.enabled`, plus the `user_terminal` toolset in that profile's
   `platform_toolsets` for the platforms its owner chats on).
2. `hermes config set user_terminal.device <mac device name>` (root, and each such profile).
3. The Mac runs Hermes Desktop with the session-mentions desktop plugin, connected to that gateway.

## Tests

```
cd plugins/user-terminal && uv run --with pytest --with fastapi --with httpx python -m pytest -q -p no:cacheprovider tests_user_terminal.py
cd plugins/slack-workspace && uv run --with pytest python -m pytest -q -p no:cacheprovider tests_ownerops.py
cd plugins/mac-ops && uv run --with pytest python -m pytest -q -p no:cacheprovider tests_mac_ops.py
cd plugins/session-mentions && node --test --test-reporter=tap tests/user-terminal.test.mjs
```

## Why (five whys)

```
Problem: a team bot (profile alpha on a cloud gateway) told the owner to run `token_broker_client.py revoke`
  himself and to put a Slack token into AWS Secrets Manager by hand, because its sandbox has no AWS.
Why 1: the bot had no way to run a command on the owner's Mac (AWS SSO, gh, keychain live there).
Why 2: run_in_user_terminal, the confirm-gated way to do exactly that, was not registered on the gateway
  — register() returned early when there is no osascript/cmux (user-terminal/__init__.py:334).
Why 3: the tool assumed the agent runs on the machine whose screen shows the window; nothing carried a
  job from a screenless gateway to the owner's Mac, although the session-mentions courier already
  relays requests between gateways and the owner's Desktop.
Root (happened): no remote transport for owner-confirmed commands; the fallback was "paste this yourself".
Root (not caught): nothing flagged a remote agent handing commands to the owner; the "Commands That Need Me"
  rule existed only in the Mac's AGENTS.md, not in the cloud bot's instructions.
Recurrence: same shape as the sudo "use SUDO_PASSWORD" steer that transform_terminal_output already fixes
  locally: the agent falls back to handing over the command when the tool is missing.
Countermeasures:
- symptom: remote mode of run_in_user_terminal via the courier — proof: test_round_trip_remote_to_mac_and_back.
- root: owner-only gate, origin shown in the window, immediate error without a courier — proof: test_owner_gate,
  test_window_shows_origin_and_neutralises_escapes, test_no_courier_errors_immediately.
- detection: the bot's SOUL/instructions carry the "owner-only or Mac-only commands go through
  run_in_user_terminal, never 'run this yourself'" rule.
```
