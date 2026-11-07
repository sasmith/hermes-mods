---
name: kanban-hands-digest
description: "Use when asked what's blocked or what do you need from me. Session-scoped by default; global digest on request."
version: 1.0.0
author: daveey
license: MIT
metadata:
  hermes:
    tags: [kanban, digest, blocked, human-in-the-loop, cron, auto-unblock]
    related_skills: [kanban-worker, kanban-orchestrator]
---

# Kanban hands-on digest

Cards that wait on a human (tap a screen, install a build, flip a setting, answer a question) each block
separately, so the human gets piecemeal asks and cards sit for hours. This skill turns every such card into
**one ordered checklist** the human can clear in one sitting, and auto-unblocks cards whose completion a
machine can verify.

## Scope: this session by default

"What's blocked?" / "what do you need from me?" means **this chat's cards** (the ones it created or is
subscribed to — its task strip), not every board. Find them from this session's own `kanban_create` results /
task strip, then `kanban_show` each and report the blocked ones. Also report two states that wait silently:
- `triage` with a human `block_kind` (`needs_input`/`capability`): block-loop detection moved a repeatedly
  re-blocked card there. It still waits on the user, and the latest `block_loop_detected` event holds the ask.
  Filtering on `status='blocked'` alone (as the digest script does) misses these.
- `scheduled` cards whose wake time has passed, or that have no wake time at all (no `wake_at` on the
  latest `continued`/`scheduled` event, or a wake cron that no longer exists): they sit unrun
  indefinitely. Re-queue with `hermes kanban unblock <id>` (it accepts scheduled cards). A `needs_input`
  card parked in scheduled is an ask of the user; report it.
Re-read each card (`kanban_show`: status, latest comments) right before presenting an ask. The user often
answers from the Desktop board (a comment by `desktop`), and the worker resumes on its own, so a question
relayed from an earlier notification may already be stale. When relaying the user's answer to a blocked card,
or when the user's step is done, post a `kanban_comment` saying what changed, re-queue the card yourself,
then read the status back. Don't wait for the card to notice. `kanban_unblock` works for blocked/scheduled
cards; for `triage` it, `kanban unblock` and `kanban promote` all refuse — move it yourself with
`_set_status_direct` (Pitfalls: moving a card out of triage), or the `specify_triage_task` move in
`kanban-wait-watchdog` (no body rewrite). Don't hand the drag to the user; they keep triage clear.
Closing a triage card has the same wall. `complete_task` only accepts running, ready, blocked or review, so
`kanban_complete`, `hermes kanban complete --force`, `kanban block` and `request-review` all refuse a triage
card, and the errors look like "unknown id". **Don't ask the user to drag it to Done**: the dashboard sends no
summary, and the empty-evidence guard rejects the drop ("completion blocked: <id> has no result or summary
evidence"). A comment doesn't count as evidence. In a normal session (not a worker), close it yourself:
move it triage→blocked, which never dispatches, then complete it with the summary in the same script. Run it the same way as in
`kanban-wait-watchdog`: a temporary module in the hermes-agent root run with `hermes --run-module`. Any
Python that can import `hermes_cli` from that dir also works; it prints plugin and SQLite warnings, which
are harmless here.
```python
from hermes_cli import kanban_db as k
from hermes_cli.kanban_db_connect import connect
conn = connect()
with k.write_txn(conn):
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=? AND status='triage'", (tid,))
k.complete_task(conn, tid, summary="<acceptance evidence>", metadata={...}, force=True)
```
Then check `hermes kanban show <id>`: status should be `done`, and the summary should sit on the new completed
run. Moving it to Ready first instead risks the dispatcher spawning a worker before you complete it. Name the
cards that wait on it so the user knows what this releases.
**Retiring a blocked card the user drops** (e.g. a live test that never ran): `kanban_complete` with the
card's `task_id` accepts `blocked` from a normal session. Comment first with what stays in place (patches,
commits) and what was skipped, then complete with a summary saying the same; no status moves needed.
Go global (the digest script below, all
boards) only when the user says so ("globally", "all boards", "everything") or the session has no cards — then
say it's a global view.

**This user expects the session to track the cards it filed and tell them when one blocks, crashes or gives
up**, not to discover stalls later. After every `kanban_create`, confirm a `tui` subscription (pitfall below).
When the user returns to the project or asks for state, check every card this session filed first and report
any that sat blocked/crashed, with how long; time a card spent waiting unnoticed is the delay they notice.
A decision the user keeps putting off (e.g. onboarding another person) stays on the list: this user wants
to be re-asked. Repeat it briefly at the end of status updates, as a table of the decisions with a
suggested default for each, so the whole answer can be one line ("defaults, email is X"), and ask whether
something specific is holding it up that a card could fix first.

## Global digest

```
python3 <skill_dir>/scripts/hands_digest.py --config <your config.json> --run-checks --no-unblock
```

Paste the output as-is (plain text). `--run-checks` runs the auto-checks so already-done steps move to the
"Already done" section; `--no-unblock` is right inside a Kanban worker or delegate child, where the
`hermes kanban unblock` CLI is refused anyway. Drop `--no-unblock` in a normal session to let passing
checks unblock their cards. Other flags: `--json`, `--max-chars N` (shrinks item text to fit a chat post),
`--checks-only` (silent unless it unblocked something; for a frequent cron), `--dry-run`, `--home`.

## What it reads

Every board (`$HERMES_HOME/kanban.db` + `kanban/boards/*/kanban.db`, `_archived` skipped), read-only.
A card is included when `status='blocked'` and its latest `blocked` event has kind `needs_input` or
`capability`, or it was created blocked (`initial_status`) — then the body's structured keys or its
`Blocked until:` line are the reason. `transient`/dependency blocks are not human work.

## Block-reason format workers should write

```
ACTION: <one imperative line the human can do, with the device>
DEVICE: <group name from config, e.g. iPhone>
UNBLOCKS-WHEN: <observable condition>
CHECK: <check name> [args]          (optional; must exist in config `checks`)
DEADLINE: 2026-10-18T19:55-07:00    (optional; hard deadline, e.g. an invite expiry)
```

**Creating a card parked (`initial_status='blocked'`):** `kanban_create` takes no block reason, so the board shows
none and the human can't tell what's wanted. Right after creating it, post a `kanban_comment` in this format
(ACTION + UNBLOCKS-WHEN at minimum). Or create it ready and put the questions in a comment.

**Diagnosing "why is X blocked?":** `kanban_show` it and read `events` and `runs`. A `blocked` event with reason
`initial_status` and no runs means it was parked at creation, and no worker ever ran. The real asks are then in
the body ("Open items", questions). A user comment alone never unblocks a card. Tell them the exact answers needed,
then comment + `kanban_unblock` for them. The blocked-panel Desktop plugin's *Reply & unblock* does both in one step.

Keys may be on separate lines or run together in one line; `ACTION:` may repeat. Free-text reasons still
work through heuristics (imperative sentences kept, shipping/status chatter dropped, `expires <date>` and
`before/after HH:MM PT` picked up), but the structured form gives a clean one-liner.

## Config (JSON; keep the personal one outside this repo)

`references/config-example.json` shows every key:
- `groups`: ordered `{name, keywords, weight}`; a card goes to the group with the highest weighted keyword
  count. Display order = list order, after the TIME-SENSITIVE block (deadline within
  `deadline_horizon_hours`).
- `shared_steps`: dedupe one step shared by several cards. `sentence` regex picks the clause (only the
  matching clause is removed from each card's own action), `value` regex extracts a number, `agg`
  (`max`/`min`) combines them, `label`/`check` format with `{max}`.
- `rules`: `{match, group, check, action, unblocks_when}` applied by regex to the reason.
- `cards`: per-card overrides by id.
- `checks`: `{name: {cmd, unblock, timeout}}`. `cmd` is a shell command formatted with the check's args
  (`{0}`, `{args}`). Exit 0 = satisfied, 1 = not yet, else error; the first stdout line is shown. With
  `unblock: true`, a passing check bound to a card runs `hermes kanban [--board B] unblock --reason
  "auto-check ... passed: <line>" <id>`. Checks bound to a shared step only mark the step done.

## Scheduling (Hermes cron, no-agent)

Two no-agent jobs pointing at a small wrapper in `~/.hermes/scripts/` that passes your config:
a morning digest (`0 8 * * *`, `--run-checks --max-chars 3400`) and a frequent auto-unblock pass
(`every 15m`, `--checks-only`, empty stdout = silent). Prove delivery once with `hermes cron run <id>`.
From inside a Kanban worker, `cronjob_manage run` / `hermes cron run` execute the script in the worker's
own context, so its `hermes kanban` calls hit the worker fence and fail; prove a board-mutating cron by
its next scheduled (gateway) fire instead. `deliver=local` stores the output without a chat post; a
script-made job has no origin, so reach the desktop chat through the cards' `local`/`tui`
subscriptions (subscribe before the event you want delivered: new subscriptions start caught up).

## Tests

```
uv run --with pytest python -m pytest -q -p no:cacheprovider skills/devops/kanban-hands-digest/tests
```

Covers board discovery/filtering, structured + free-text parsing, shared-step dedupe, deadline/urgency
ordering, grouping, check runner + auto-unblock through a fake `hermes` binary (incl. `--board`),
`--max-chars` shrinking, and the CLI's `--json` / silent `--checks-only`.

## Pitfalls

- Desktop/TUI chat sessions may not be pinged when a card is escalated to triage: if
  `tui_gateway/session_notifications.py` `_KANBAN_NOTIFY_KINDS` lacks `block_loop_detected` (and
  `review_requested`/`changes_requested`, which the gateway notifier has), a worker's second identical
  `needs_input` block goes silent and the card sits in triage. Check `grep -n "_KANBAN_NOTIFY_KINDS ="` in the
  install; if the kinds are missing, the fix is adding them plus formatters in `_KANBAN_EVENT_FORMATTERS`,
  committed on the user's local patches branch (restart the desktop app to load it). Until then, check this
  session's cards for `triage` whenever the user asks about progress. For alerts off the laptop, offer a
  messaging-platform subscription or a no-agent cron running the digest with `--checks-only`-style change
  detection.
- Cards from `kanban_create` are subscribed as `platform=local` (mode notify+wake). The gateway delivers
  those to the desktop chat through its local-session adapter, so a `local` sub alone is normally enough;
  the desktop/TUI poller only claims `platform=tui`. Adding a `tui` sub as well is a harmless belt and
  braces: `hermes kanban notify-subscribe <id> --platform tui --chat-id "$HERMES_SESSION_KEY"`. Use the full
  session key (`agent:main:local:dm:<session id>`), not the bare session id; the poller matches
  `chat_id == session_key`. The key can change when the desktop app restarts, so re-check after one.
- **When no card events reach the chat at all,** even with correct subscriptions, check the gateway side
  before re-subscribing. If the gateway's local-session adapter has no `gateway_runner` bound, every
  `local` ping is a silent no-op and every wake is quietly rewound. Check `gateway_state` for the running
  code sha after any local fix, and treat a `[kanban] ...` message arriving in the chat as the only proof
  it works.
- **Never promise chat alerts you haven't seen delivered.** A subscription is necessary but not proof:
  compare `kanban_notify_subs.last_event_id` for the card with its latest `task_events.id`. If a notify-kind
  event (blocked/completed/crashed) sits past every sub's cursor, delivery is broken for this session. Then
  tell the user plainly, check all this session's cards on every user turn (status + latest
  blocked/completed event), and file a card to debug delivery rather than re-subscribing again.
- **Alex's install has the durable fixes applied** (branch `local/patches-v0.22.5`): upstream #128677
  (local commit 864765308e) and #125207 (0b5bfb9845), plus a local sleep-aware dispatch patch
  (2de0b2a3ff: no spawn/reap in a DarkWake, sleep-spanning runs requeued as `interrupted_by_sleep`
  without spending a retry, turn-lease renewal after sleep). With them, a re-block whose reason changed escalates
  only at 4 recurrences instead of 2 (`block_recurrences` still counts up; a comment or heartbeat is not
  progress, only a review cycle resets the streak), and `decompose_task` refuses a `block_loop_detected`
  triage card, so auto-decompose leaves it for the human. A same-reason re-block still goes to triage, so
  the triage moves above still apply. The child clean-up in the next bullet is a fallback for when
  `decomposed` children show up anyway: the patches got dropped by `hermes update` before upstream merged
  them, or it's another install. Check with `git -C ~/.hermes/hermes-agent log --oneline -30` for the two
  `local: apply upstream` commits, or whether the PRs merged.
- With `kanban.auto_decompose: true`, a card that lands in `triage` from a block loop gets split by the
  decomposer about a minute later into child cards that gate it and start re-doing finished work. After a
  block loop on a human ask, immediately `reclaim` any running child, then `unlink` + `archive` every
  `decomposed` child, and park the card with `_set_status_direct` triage→todo→scheduled in one script (no
  wake time; scheduled is never decomposed or dispatched) until the human answers; then `hermes kanban
  unblock` it. `kanban_block` on a card that already block-looped sends it straight back to triage. Tell
  workers in the relay comment to block once with the exact ask, never twice in a row. Before archiving
  the children, check `git log` in the card's repo: a child may already have committed useful work (a
  tightened policy, a helper script). Keep those commits, and name them in the parent's relay comment.
  When a block-loop notification arrives for a card whose human step is already done (or nearly), move
  it triage→ready with `_set_status_direct` straight away, before the decomposer's next tick, and put
  "finish now; don't block again waiting for confirmation" in the comment.
  When the user asks why this keeps happening: the loop counter ignores progress between blocks, and
  the decomposer treats a block-loop escalation as an undecomposed card. The durable fixes are a local
  Hermes patch (reset the counter on progress; exclude escalations from auto-decompose) plus writing
  cards with one human gate each. Offer those before suggesting auto-decompose be turned off, since
  that setting is board-wide.
- **Delivery fixes that need a gateway restart:** the terminal tool refuses `hermes gateway restart` from
  inside a gateway-hosted session, so ask the user to run it in Terminal. Restarting kills running
  workers, so first check their worktrees are committed, and check no worker is mid external operation
  (`ps` for `cdk deploy`, long uploads). If one is, tell the user to hold the restart and name the moment
  to do it (when that card blocks or completes); a deploy killed halfway leaves the stack rolling back.
- **Cards that patch the live Hermes install** (`~/.hermes/hermes-agent`): say in the body to apply and
  resolve in a git worktree, then fast-forward the live branch only once `python -c "import
  hermes_cli.kanban_db"` passes there. Conflict markers left in the live checkout break every `hermes`
  CLI call and every new worker spawn (SyntaxError on import); only the already-running gateway keeps
  working. If you find markers mid-run, comment on the card to finish and commit now, and don't run
  `hermes kanban` mutations until the import passes again. Before the restart, fast-forward the stuck
  cursors so a backlog doesn't burst into the chat (when the user prefers that):
  `UPDATE kanban_notify_subs SET last_event_id=(SELECT coalesce(max(id),last_event_id) FROM task_events e
  WHERE e.task_id=kanban_notify_subs.task_id) WHERE platform IN ('local','tui')` in `~/.hermes/kanban.db`.
- **A `crashed` card whose worker output ends in a question to the user** means the worker wrote its ask as
  plain text and exited instead of calling `kanban_block`; the dispatcher just respawns it. Read the
  `worker_output` of the `crashed` event, then check the real side effects yourself (stack status, the
  site/URL, commits, the deploy log in scratch) before reporting: often the step succeeded and only the
  handoff failed. Relay the verified outcome, let the respawned run re-block with the exact command, and
  don't reconstruct the ask from truncated output.
- **A `crashed`/`gave_up` card whose output ends `Operation interrupted: waiting for model response
  (<N>s elapsed)`** usually means the laptop slept, not a model or card problem. Confirm with
  `pmset -g log | grep -E " (Sleep|Wake|DarkWake) "` around the crash time; committed or
  worktree work survives. Tell the user it was sleep instead of re-briefing the card.
  - **Why sleep burns retries:** with Power Nap on (`pmset -g | grep powernap` → 1) the Mac keeps
    half-waking (DarkWake) for a minute or so. The gateway runs then, records the frozen worker as
    crashed and respawns it, and the Mac sleeps again before the new worker's model call returns.
    Each DarkWake costs one retry, so a card gives up overnight (default limit 2) without making
    progress. Crash timestamps line up with DarkWake lines in the log.
  - **Raise the limit early, before the next DarkWake:** `UPDATE tasks SET max_retries=8 WHERE id IN
    (...)` in `~/.hermes/kanban.db` (no CLI flag sets it on an existing card). A raise after
    `gave_up` doesn't help that run: `hermes kanban unblock` it, which also resets
    `consecutive_failures` to 0, then read back `status,max_retries,consecutive_failures`.
  - **Don't unblock a given-up card while the Mac keeps sleeping** (user away, lid closed); it just
    burns retries again. Park it and resume when the user is back.
  - **A second cause of exit 130 after sleep is the session turn lease:** its wall-clock deadline
    passes while the Mac sleeps, and on wake the refresher hard-interrupts the worker's own turn
    (agent.log: `session turn lease lost` → `interrupted_by_system`). The sleep-aware patch renews
    once when sleep explains the expiry. If sleep crashes reappear, check `gateway_state.json`
    `code_sha` still includes it (a `hermes update` can drop it).
  - **Don't recommend turning Power Nap off:** the user relies on it for other things, and it only
    hides the dispatcher's mistake. The half-wake is detectable: `pmset -g systemstate` lists
    `CPU Graphics Audio Network` when fully awake and drops Graphics/Audio in a DarkWake. The
    durable fix is a sleep-aware dispatcher patch on the local Hermes branch: don't spawn or reap
    workers in a DarkWake (notifications, crons and watchdogs still run), record a run that spanned
    a sleep as `interrupted_by_sleep` without spending a retry, and have the worker retry a model
    call cut off by sleep instead of exiting. Check the local-patch branch log for it before
    offering it again. Until it's live: raise `max_retries` on long cards, and suggest keeping
    the Mac awake and plugged in (`caffeinate -dimsu &`; a closed lid still sleeps without an
    external display).
- **Reading a worker's transcript** to see what a crashed run actually did: the `crashed` event's
  output names its session (`Session: local-<hash>`). Query `~/.hermes/state.db`: `SELECT
  datetime(timestamp,'unixepoch','localtime'), role, substr(coalesce(nullif(content,''),tool_calls),1,800)
  FROM messages WHERE session_id='local-<hash>' ORDER BY id DESC LIMIT 10`. Assistant turns keep
  their commands in `tool_calls`, with `content` empty. Then verify side effects (stack status, URLs,
  provider state such as SES review status) before writing the relay comment with the remaining steps.
- **A worker that can't mutate the board** (its shell carries `HERMES_DELEGATED_CHILD_CONTEXT`, so the
  kernel fence refuses `hermes kanban` and direct `kanban_db` writes) sometimes blocks asking the user to
  run a board script, e.g. a live block/unblock check on TEST cards. Read the script, and if it only
  touches its named test cards, run it yourself from this session (no fence here; check `env | grep
  DELEGATED` is empty), post the output as the relay comment, and unblock. Don't hand it to the user.
- **Throwaway TEST cards still notify the chat:** every block, escalation and crash on them arrives as
  an alert, and archiving doesn't cancel events already emitted. Delete their subscriptions as soon as
  the test ends (`DELETE FROM kanban_notify_subs WHERE task_id IN (...)`), and tell the user in one
  line that the remaining TEST alerts can be ignored.
- **After a gateway restart, alerts for events you already handled can arrive late** (a `gave_up` or
  `crashed` you already diagnosed). Before acting, list the card's events after the last one you
  handled; if there's nothing new, say it's the delayed copy and restate the one pending ask.
- **Reference a card id only after `kanban_create` returns it.** Writing "card t_… will do X" in a
  comment before creating that card puts an invented id on the board; create first, then comment.
- **Workers sometimes offer to take secrets pasted in chat** ("send me the key and secret"). Don't
  relay that offer. Tell the user to add the values to the secret store themselves, and add "never
  ask for secrets in chat" to the relay comment.
- Approval-gated sessions expire: an MFA/SSO session the user refreshed for a worker (assume-role ~1 h)
  can lapse while the worker does other steps first, causing a repeat block. Verify the session yourself
  (`aws sts get-caller-identity --profile <p> < /dev/null`, no prompt) before unblocking, and tell the
  worker to run the admin-gated batch first.
- Moving a card out of `triage`: `kanban_unblock`, `hermes kanban promote` and `complete` all refuse it, and
  the dashboard API needs auth. From `~/.hermes/hermes-agent`, the Hermes runtime venv python with
  `PYTHONPATH=.` can call `plugins.kanban.dashboard.plugin_api._set_status_direct(kanban_db_connect.connect(),
  <id>, "todo"|"ready")` (the allowed triage moves). A card with open parents lands in `todo`; `ready`
  dispatches at once.
- A `mode=ro` sqlite open can fail transiently while a writer truncates the WAL; the reader retries,
  then falls back to `query_only`.
- Checks that read a remote DB must not `.backup` a big live database per call (tens of seconds); open
  it `-readonly` with a busy timeout.
- Inside a Kanban worker the `hermes kanban` CLI refuses mutations, so auto-unblock only works from cron or
  a normal session; run on-demand with `--no-unblock` there.
