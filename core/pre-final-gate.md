# `pre_final_gate` hook: hold an answer silently, or append to it

Patches:

- `patches/0165-feat-agent-pre_final_gate-hook-silent-stop-gate-hold.patch`: the hook, the silent hold, the stream buffer and the config keys.
- `patches/0167-fix-agent-reset-pre_final_gate-attempts-outside-_PER.patch`: fix-up for 0165, moves one counter reset (see How it works).

What you see: a plugin can stop a final answer before it reaches the screen and make the agent try again. The user never sees the first answer, and it is not stored in the transcript. A plugin can also add a note to the end of an answer.

## Behavior

- New plugin hook `pre_final_gate(session_id, platform, model, final_response, turn_messages, attempt)`. It runs on every text stop, after verify-on-stop, `pre_verify` and the kanban worker guard.
- Return `{"hold": True, "nudge": str}`: the answer is not emitted as an interim message and not written to `state.db`. It stays only as the fallback if the iteration budget runs out. The model gets the nudge as a private synthetic user row and the turn continues.
- Return `{"append": str}`: the answer is delivered with `"\n\n" + text` added. The appended text is also streamed, so the screen matches the stored row.
- `agent.pre_final_gate_max` (default 2) caps holds per turn. At the limit a hold counts as deliver, and an `append` in the same dict still applies, which gives "deliver with the judge's note".
- A callback that raises, or runs longer than `agent.pre_final_gate_timeout` (default 30 s), is logged at WARNING and the answer is delivered unchanged.
- Callback attribute `needs_buffer = True`: assistant text deltas are buffered for the turn. They are released when a response turns out to carry tool calls, when the gate delivers, or at turn end. On a hold they are dropped. This is needed because no surface can retract streamed text: Desktop keeps any text that comes before a later tool call, and CLI and messaging streams cannot unprint. The cost is that answers appear in one chunk instead of streaming.
- Callback attribute `include_background = True`: the hook also runs in kanban workers, `delegate_task` subagents and cron. It is skipped there by default.

## How it works

- `agent/pre_final_gate.py`: finds eligible callbacks (via `iter_hook_callbacks`, with background contexts filtered out), runs each under a timeout, and returns a `GateDecision`. The first hold wins while under the limit, and appends add up.
- `agent/turn_stop_gates.py::apply_stop_gates`: on a hold, the candidate row and the nudge row are both flagged `_pre_final_gate_synthetic`. The buffer is discarded and the turn continues with the candidate as `pending_verification_response`. On deliver it flushes the buffer, and on append it rewrites the row and streams the suffix.
- `_pre_final_gate_synthetic` is listed in `session_persistence._EPHEMERAL_SCAFFOLDING_FLAGS` (never persisted), `turn_finalizer._VERIFICATION_CONTINUATION_FLAGS` (stripped from returned/live history) and `conversation_compression._SYNTHETIC_USER_FLAGS`.
- `agent/stream_delivery.py`: `_fire_stream_delta` and the scrubber-tail flush route through `_buffer_final_gate_text` while `_final_gate_buffer` is a list. `_emit_interim_assistant_message` and `turn_tool_round` flush first. `turn_context._reset_per_turn_agent_state` arms the buffer, and `finalize_turn` flushes and disarms it.
- `_pre_final_gate_attempts` is reset in `_reset_per_turn_agent_state` next to the buffer arm, not in the `_PER_TURN_RESET_STATE` tuple, which upstream edits (0167).
- `turn_final_response` now takes `final_response` from the stop-gate verdict, so an append reaches the stored row.

## Code

`agent/pre_final_gate.py`, `agent/turn_stop_gates.py`, `agent/stream_delivery.py`, `agent/turn_context.py`, `agent/turn_final_response.py`, `agent/turn_finalizer.py`, `agent/turn_tool_round.py`, `agent/session_persistence.py`, `agent/conversation_compression.py`, `hermes_cli/plugins.py` (`VALID_HOOKS`), `hermes_cli/config_defaults.py`, `website/docs/user-guide/features/hooks.md`.

## Tests

`tests/agent/test_pre_final_gate.py`, end to end through `AIAgent.run_conversation` with a scripted model that streams each answer:

- `test_hold_once_never_shows_or_persists_first_answer`: the held text never reaches the stream or interim callbacks. The second request carries the held answer and the nudge. Both hold rows are ephemeral, and the returned history is `[user, assistant(final)]`.
- `test_append_delivers_with_note`, `test_hold_limit_then_delivers_with_append` (attempts 0, 1, 2, then deliver with the note), `test_hook_exception_delivers_unchanged_and_logs`, `test_skipped_for_subagents_unless_opted_in`, `test_no_hook_streams_live`, `test_buffer_released_by_interim_and_discarded_on_hold`.
- Live: a throwaway plugin that holds once on a marker prompt, run against the Desktop backend over `/api/ws`. The first answer appeared in no `message.delta`/`message.interim`/`message.complete` event and not in the stored transcript, and the second answer did (task t_45d5a361).
- `test_slow_hook_times_out_and_delivers`: a callback past `agent.pre_final_gate_timeout` is skipped and the answer is delivered.

## Applying

Standalone on top of upstream's `pre_verify` stop gate (`agent/turn_stop_gates.py`).

## Gotchas

- Without `needs_buffer`, a hold still hides nothing that has already streamed. Use it for any hook that holds.
- Already-running sessions keep the old code until their backend restarts.

## Plugin?

No. Plugins have no surface that can stop a final answer before it is emitted and persisted, or keep streamed deltas back. The ask-gate plugin (`plugins/ask-gate`) builds on this hook.
