# Kanban results reach chats no backend holds (+ green row for waiting results)

Patches: 0185 (delivery + reap warning), 0186 (`kanban_unseen` stripe input).

## Behavior
- Card completion/blocked/crash notices for a `platform=tui` subscription are written into that chat's transcript
  even when no backend has the chat loaded (websocket drop, parent-death watchdog, `startup_orphan_reap`). The notice
  is a `role=user` row with `display_kind="kanban_notice"` (renders as a system line), the row is marked unread and
  its last-activity bumped, so the sidebar row turns green with no typing.
- Session rows carry `kanban_unseen`: finished cards whose result no chat has been handed yet. With no running cards
  it paints the row green; running cards (`kanban_active`) keep it `background`. An orphan-reaped idle session with
  turns is `done` (green), never working/stalled.
- `startup_orphan_reap` logs a WARNING naming reaped chats that still hold open kanban subscriptions.

## How it works
- `tui_gateway/kanban_unloaded.py` runs a sweeper thread in every gateway process (30 s; first pass after 20 s).
  It skips chats this process holds, and events younger than a 30 s grace (a live poller anywhere claims within ~5 s).
  It claims with the same atomic cursor CAS as the live poller (`claim_unseen_events_for_sub`), so a range is claimed
  once; a failed or refused append rewinds the claim.
- `SessionDB.append_kanban_notice` dedupes by `notice_id` (board:task:cursor range) inside the writer txn, refuses
  while another backend holds a live turn lease, and stamps a NULL `last_read_at` just before the notice.
- `kanban_session_activity._read_unseen` counts tui subs whose cursor trails a terminal event.
- Knobs (env): `HERMES_KANBAN_UNLOADED_SWEEP_S` (0 disables), `..._GRACE_S`, `..._FIRST_S`.

## Code
`tui_gateway/kanban_unloaded.py`, `hermes_state_messages.py` (`append_kanban_notice`), `tui_gateway/session_reaper.py`,
`hermes_cli/kanban_session_activity.py`, `apps/desktop/src/store/session-dot-state.ts`
(`$kanbanUnseenSessionIds`, `withRowActivity`), `lib/chat-messages/hydration.ts`, `types/hermes.ts`.

## Tests
- `tests/tui_gateway/test_kanban_unloaded_sweep.py`: notice persisted in one sweep, exactly once, row unread;
  live poller vs sweeper never double deliver (both orders); loaded session skipped; grace respected; failed append
  rewinds and retries; unknown chat keeps cursor; dedupe; reap warning.
- `tests/hermes_cli/test_kanban_session_activity.py`: `kanban_unseen` counting.
- `session-dot-state.test.ts`: results waiting -> green; cards running -> background; orphan-reaped idle -> green.
- Live check (scratch HERMES_HOME, real sweeper thread, no typing): row `kanban_unseen=1` before, notice in
  transcript within ~1 s and `unread=True` after, nothing duplicated over further sweeps.

## Applying
`git am -3` patches 0185 then 0186 (0186 only touches the row stamp and renderer; 0185 stands alone).

## Gotchas
- The sweeper only handles chats found in a local state.db (launch store, then named profiles); a chat on another
  gateway is swept by that gateway.
- The notice is a transcript row, not an agent turn: the agent reads it when the chat is next opened.

## Plugin?
No plugin surface exists for gateway-side delivery or sidebar stripe inputs.
