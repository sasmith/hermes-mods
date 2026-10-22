# Kanban timed waits: park scheduled with a wake, never as a human block

Patches:

- `patches/0113-fix-kanban-route-timed-waits-to-kanban_schedule-not-.patch`: agent/tools. Worker guidance and the
  `kanban_block` description send timed or external waits to `kanban_schedule`.
- `patches/0153-fix-kanban-wake-continued-waits-even-when-a-comment-.patch`: a due `kanban_continue` wake is no
  longer masked by a later comment/heartbeat; new `overdue_wake` diagnostic.
- `patches/0155-fix-kanban-a-block-that-waits-on-time-parks-schedule.patch`: a block that waits on time
  (`kind='time'`, a parent-less `dependency`, or a `SCHEDULED_UNTIL=` reason) parks `scheduled` with a wake time;
  `SCHEDULED_UNTIL=` on `kanban_schedule` becomes a real timer; the sidebar counts only human blocks as blocked.

What you see: a card waiting on a run cap, a soak, a nightly run or a release window goes to `scheduled`, not
`blocked`, so the chat that created it no longer turns red as if it needed you. It wakes by itself at the time it
named (or after 24h), and a comment or heartbeat landing after the park no longer leaves it asleep forever.

See also [kanban-park-invariants.md](kanban-park-invariants.md) (0169): every `kanban_schedule` park now gets a wake
(default 24h) and human asks cannot be scheduled.

## Behavior

- Guidance (0113, 0155): `KANBAN_GUIDANCE` tells workers to call
  `kanban_schedule(reason="SCHEDULED_UNTIL=<ISO8601> — …")` for timed/external waits, and goal-mode cards (which
  may not call `kanban_schedule`) `kanban_block(kind="time", reason="SCHEDULED_UNTIL=…")`. The `kanban_block`
  description and `kind` enum name `time`.
- Timed park (0155): `block_task` parks the card `scheduled` instead of `blocked` when the call is a wait on time:
  - `kind='time'`;
  - `kind='dependency'` with no open parent (upstream re-kinded this to sticky `needs_input`, painting the chat
    red);
  - any kind whose reason contains `SCHEDULED_UNTIL=<ISO8601>`.
  A call with a shared `blocker_id` is never a timed park, so a parent-less dependency behind a shared blocker
  keeps the sticky `needs_input` re-kind. A plain `needs_input` block without a marker still blocks for a human.
- Wake time: the `SCHEDULED_UNTIL=` timestamp (naive = local time), clamped to [now + 60 s, now + 14 days];
  without one, now + 24h, so a park can never be forgotten. `hermes kanban unblock` still wakes it earlier.
- `kanban_schedule` with a `SCHEDULED_UNTIL=` marker now stores `wake_at` on its `scheduled` event, so the marker
  is a real timer; without a marker it still waits for a human/cron unblock.
- Waking (0153 + 0155): `wake_due_continuations` finds the event that parked the card (latest `continued` or
  `scheduled` event) instead of the card's latest event of any kind, and promotes it once that event's `wake_at`
  has passed. Before 0153 any later comment, heartbeat, notification or edit masked a due wake indefinitely.
- Diagnostic (0153): `overdue_wake` flags a `scheduled` card still parked more than
  `overdue_wake_grace_seconds` (default 600) after the `wake_at` of its parking event (dispatcher down or a wake
  bug). Parks without a wake time are ignored.
- Sidebar counts (0155, `kanban_session_activity`): `kanban_blocked` (red) counts only human blocks
  (`needs_input`, `capability`, or no kind). `transient` and `time` blocks count as in-progress work, like
  `scheduled`.
- The `kanban_block` tool result for a timed park says it landed `scheduled`, with `wake_at`.

## How it works

- `hermes_cli/kanban_timed_park.py` (new, 0155): `scheduled_until(reason)` parses and clamps the marker;
  `is_timed_park(kind, reason, parents_satisfied=…)`; `park(conn, task_id, …)` moves `running`/`ready` →
  `scheduled`, clears the claim, ends the run with outcome `scheduled`, and appends a `scheduled` event
  `{reason, timed_park: True, requested_kind, wake_at, resume_status}`; `scheduled_payload(reason)` for
  `kanban_schedule`.
- `hermes_cli/kanban_db.py`: `VALID_BLOCK_KINDS` gains `time`; `block_task` routes timed parks to
  `kanban_timed_park.park` before the old re-kind logic.
- `hermes_cli/kanban_continuation.py`: `SCHEDULED_ENTRY_EVENTS = ("continued", "scheduled")`;
  `pending_wake(conn, task_id)` reads the latest such event and returns its payload when it carries `wake_at`;
  `wake_due_continuations` uses it.
- `hermes_cli/kanban_diagnostics.py`: `_rule_overdue_wake`.
- `hermes_cli/kanban_session_activity.py`: `HUMAN_BLOCK_KINDS` and `_status_filter` split `blocked` by
  `block_kind` (boards without that column are unchanged).
- `tools/kanban_tools.py`: goal-mode cards may block with `time`; timed-park result note.
  `tools/kanban_tools_schemas.py`, `agent/prompt_builder.py`: wording.

## Code

`hermes_cli/kanban_timed_park.py`, `hermes_cli/kanban_db.py`, `hermes_cli/kanban_continuation.py`,
`hermes_cli/kanban_diagnostics.py`, `hermes_cli/kanban_session_activity.py`, `tools/kanban_tools.py`,
`tools/kanban_tools_schemas.py`, `agent/prompt_builder.py`.

## Tests

- `tests/agent/test_system_prompt.py`: `test_kanban_guidance_routes_timed_waits_to_schedule_not_block` (guidance
  names `kanban_schedule(` and `SCHEDULED_UNTIL=`; updated by 0155).
- `tests/hermes_cli/test_kanban_continuation.py` (0153): `test_due_wake_is_not_masked_by_later_annotation`
  (parametrized over annotation kinds), `test_kanban_schedule_after_wait_and_comment_still_stays_put`.
- `tests/hermes_cli/test_kanban_diagnostics.py` (0153): `test_overdue_wake_flags_scheduled_card_past_wake_at`,
  `test_overdue_wake_ignores_kanban_schedule_parks`.
- `tests/hermes_cli/test_kanban_timed_park.py` (new, 0155): parent-less dependency parks scheduled;
  `SCHEDULED_UNTIL` sets the wake and the dispatcher wakes it; `time` kind parks and unblock still wakes early;
  `kanban_schedule` marker becomes a real timer; `needs_input` without a marker still blocks; shared-blocker
  dependency keeps sticky `needs_input`; `kanban_block` tool with `time` on a goal-mode card; sidebar counts only
  human blocks.
- `tests/hermes_cli/test_kanban_block_kinds.py` (0155): `test_dependency_block_with_terminal_parents_is_a_timed_park`.
- `tests/tools/test_kanban_tools.py` (0155): `test_block_dependency_without_open_parent_is_a_timed_park`.
- The exact `kanban_block` description wording has no test (untested).

## Applying

Apply the series in order. 0155 builds on 0153's `pending_wake` / `SCHEDULED_ENTRY_EVENTS`, and edits code from
[kanban-shared-blockers.md](kanban-shared-blockers.md) (`blocker_id`) and
[kanban-blocked-sidebar.md](kanban-blocked-sidebar.md) (session counts), which come earlier in the series. Needs
the upstream `kanban_schedule` / `kanban_continue` tools and the dispatcher's continuation wake pass.

## Gotchas

- This changes upstream behavior: a parent-less `dependency` block used to become a sticky human block. A wait
  that really needs a person should use `needs_input`.
- Default wake is 24h, so a parked card re-runs daily until its wait clears; the run re-checks and parks again.
- `SCHEDULED_UNTIL` beyond 14 days is clamped to 14 days.

## Plugin?

No. Block routing, the wake pass and the sidebar counts are kanban kernel code with no plugin hook.
