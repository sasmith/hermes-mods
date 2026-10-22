# Kanban park invariants: a card never sits parked without showing who it waits on

Patches:

- `patches/0169-fix-kanban-cards-never-park-forever-without-showing-.patch`: parts 1-4 below (part 5 is the env scrub,
  documented in [top-level-env-scrub.md](top-level-env-scrub.md)).

Root cause: a board with cards "not running, not blocked". Three separate holes let a card park with nothing on the
board saying why: the respawn guard held `ready` cards forever, a closed-unmerged PR binding could never be replaced,
and `scheduled` parks could carry no wake time (or hide a question for a human).

## Behavior

1. **Respawn-guard hold cap.** A card the guard holds in `ready` never runs, so `consecutive_failures` never grows and
   the failure breaker cannot trip. Now the dispatcher blocks the card with the guard reason once the hold exceeds a cap:
   - `blocker_auth`: 30 min, blocked as `capability`;
   - every other guard reason: 6 h, blocked as `needs_input`;
   - `shared_blocker`: exempt (a declared wall another card is probing).
   The hold clock is the unbroken run of `respawn_guarded` events since the newest event of any other kind (claim,
   block, unblock, comment), so a real run restarts it. Tunables: `HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS`,
   `HERMES_KANBAN_GUARD_HOLD_CAP_LONG_SECONDS`.
   A completion-gate refusal (`last_failure_error` starting `PR acceptance `) is never `blocker_auth`, whatever words
   its detail holds; a real provider auth failure still guards.
2. **PR acceptance.** A PR that is closed without merging gets classification `closed_unmerged` and a detail that
   names the way out (complete with `metadata.published_pr=<new PR>`). The catch-all `infra` detail names the failed
   phase and exception type instead of "check gh authentication" (that wording used to match the guard's auth regex).
   A binding to a closed-unmerged PR may be replaced by a new `published_pr` in the same repo (`pr_rebound` event);
   rebinding happens only on GitHub's positive "closed, not merged" answer, and never while the bound PR is open.
3. **Scheduled wake invariant.** Every `kanban_schedule` park carries `wake_at` (the `SCHEDULED_UNTIL=` marker, else
   now + 24 h). A card awaiting a human answer (`needs_input`/`capability` with an unanswered block) cannot be
   scheduled; the tool says it stays blocked. For legacy wake-less parks the dispatcher wakes the card at its implied
   time (marker in the reason, else park + 24 h) and moves human-kind ones straight to `blocked`
   (event field `wake_invariant`). New diagnostic `scheduled_without_wake` (error when human-kind or overdue).
4. **Escalations count as waiting on you.** A block-loop escalation in `triage` keeps its human `block_kind`: new
   diagnostics `escalated_waiting_on_user` (error) and `waiting_behind_user` (a `todo` child whose parent needs the
   user); `board_stats` / `hermes kanban stats` report `waiting_on_user` and `waiting_on_user_ids`. A plain triage
   card awaiting a specifier is not counted.

## How it works

- `hermes_cli/kanban_db_dispatch.py`: `guard_hold_cap_seconds`, `_guard_hold_started_at`, `_is_completion_gate_error`,
  `_block_if_guard_hold_capped` (called from `_dispatch_lane_task` before the `respawn_guarded` event is appended;
  the card lands in `result.auto_blocked`).
- `hermes_cli/kanban_pr_acceptance.py`: `phase` tracking, `closed_unmerged`, `pr_closed_unmerged(url, assignee)`;
  `kanban_pr_acceptance_store.py` does the rebind.
- `hermes_cli/kanban_timed_park.py`: `scheduled_payload` always has `wake_at`; `implied_wake`.
  `hermes_cli/kanban_continuation.py`: `missing_wake`, `_enforce_wake_invariant`, called from `wake_due_continuations`.
  `hermes_cli/kanban_db.py`: `schedule_task` refuses via `_awaiting_human_answer`; `board_stats` adds the waiting
  fields; `task_graph_contexts` returns each neighbor's `block_kind`.
- `hermes_cli/kanban_diagnostics.py`: `_rule_scheduled_without_wake`, `_rule_escalated_waiting_on_user`,
  `_rule_waiting_behind_user`. `hermes_cli/kanban.py`: "Waiting on you" line in `kanban stats`.
  `tools/kanban_tools.py`: the `kanban_schedule` refusal text.

## Code

`hermes_cli/kanban_db_dispatch.py`, `hermes_cli/kanban_pr_acceptance.py`, `hermes_cli/kanban_pr_acceptance_store.py`,
`hermes_cli/kanban_timed_park.py`, `hermes_cli/kanban_continuation.py`, `hermes_cli/kanban_db.py`,
`hermes_cli/kanban_diagnostics.py`, `hermes_cli/kanban.py`, `tools/kanban_tools.py`.

## Tests

`tests/hermes_cli/test_kanban_park_invariants.py` (new): `test_completion_gate_refusal_is_never_blocker_auth`,
`test_provider_auth_failure_still_guards`, `test_guard_hold_is_capped_and_auto_blocks_with_reason`,
`test_guard_hold_clock_restarts_after_a_real_run`, `test_closed_unmerged_pr_gets_its_own_classification`,
`test_catch_all_failure_names_the_phase_not_gh_auth`, `test_rebind_allowed_when_bound_pr_closed_unmerged`,
`test_rebind_refused_while_bound_pr_open`, `test_schedule_without_marker_still_gets_a_wake`,
`test_human_wait_cannot_park_into_scheduled`, `test_dispatcher_wakes_legacy_scheduled_card_without_wake`,
`test_dispatcher_honours_marker_in_legacy_reason`, `test_human_kind_scheduled_without_wake_becomes_blocked`,
`test_diagnostic_flags_scheduled_card_without_wake`, `test_block_loop_escalation_counts_as_waiting_on_user`,
`test_plain_triage_card_is_not_waiting_on_user`. Adjusted expectations in `test_kanban_continuation.py`,
`test_kanban_db.py`, `test_kanban_diagnostics.py`, `test_kanban_timed_park.py`.
Untested: the `waiting_behind_user` diagnostic and the `kanban stats` "Waiting on you" line.

## Applying

Apply in series after [kanban-timed-waits.md](kanban-timed-waits.md) (0113 0153 0155), whose `pending_wake`,
`kanban_timed_park` and `wake_due_continuations` it extends, and after [kanban-block-loop-escalation.md](kanban-block-loop-escalation.md)
and [kanban-escalated-triage-red.md](kanban-escalated-triage-red.md) (0161). Needs the PR-acceptance gate
(`completion_contract`).

## Gotchas

- A `scheduled` park no longer waits for a cron/human forever: with no marker it wakes after 24 h and the run re-checks.
- Hold caps block with the guard reason; after fixing the cause (credentials, quota, PR binding) `unblock` the card.
- `kanban_schedule` now fails on a card waiting on a human answer; use `kanban_block`.

## Plugin?

No. Dispatcher, completion gate and diagnostics are kanban kernel code with no plugin hook.
