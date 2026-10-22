# user-steps

What each chat is waiting for **you** to do, shown where you look, with one-click answers. Replaces
`open-asks` and blocked-panel's composer panel.

## What you see (Hermes Desktop)

- **In the reply:** the paragraph that asks you something becomes an amber **For you** box with the original text
  and buttons.
- **Above the composer:** a **For you** strip with one row per open step (oldest first), plus the chat's other
  blocked Kanban cards that wait on a human (from blocked-panel's data). Hidden when there is nothing.
- **In the sidebar / Kanban:** while any step is open, the chat has ONE rolling blocked card `For you: …` whose body
  lists the steps, so the chat shows as waiting on you. It is archived when the last step resolves; a later step
  opens a new one.

| Step kind | Buttons | A click sends (visible user turn) |
|---|---|---|
| approve (the agent proposes, waits for go/no) | **Do it** · No | `Go: <step>` / `No: <step>` |
| do (you act outside the chat) | **Done** · Can't | `✓ Done: <step>` / `✗ Can't: <step>` |
| choose (2–4 named options, recommended first) | one per option | `Chose: <option> — <step>` |
| input (you type an answer) | **Reply…** | prefills the composer with `Re: <step> — ` (step resolves from your reply) |

A click resolves the step in the ledger only after the composer accepted the turn. Kanban rows keep blocked-panel's
**Done → unblock** / **Reply & unblock** (no chat turn) and **Open card**.

## How it works

| Piece | Behavior |
|---|---|
| `transform_llm_output` | Cheap pre-filter (a question, "you", or an imperative verb), then the aux model (task `user_steps`) reads the final reply, the chat's open steps and your last message, and returns `{steps:[{text, kind, verbatim_span, options}], resolved:[ids]}`. Guards drop any step whose span is not in the reply's prose (code blocks, quotes, invented text) or reads as a hypothetical ("if you ever…", "unless…"), and drop terminal-doable steps (sudo, an SSH key to add, "run this in your terminal"; detector shared with ask-gate's `handoff.py`, loaded by file path, off if ask-gate is absent): the agent runs those via `run_in_user_terminal`, so they never become a For-you card (logged as `dropped_terminal` in the trace). New steps go to the ledger; on Desktop chats (`sessions.source = desktop`) the paragraph holding each span is replaced by `::user-step{id="us_…" s="<session>" t="<paragraph>"}` (the text travels in `t`, so nothing is lost; a paragraph too long for the 1024-char attr falls back to its line, then to keeping the text and appending the directive). CLI/TUI/messaging chats keep plain text but still get the ledger, strip and card. Model failure → open-asks' regex detector. |
| `pre_llm_call` | Only when steps are open: one note listing them, so the agent knows what is pending. Also remembers your message for the extractor. |
| Ledger | `~/.hermes/user-steps/<lineage root>.json`, keyed by the first session of the compression lineage (walks `parent_session_id` while the parent ended by compression), so steps survive compression. Atomic writes under an `fcntl` lock (agent and dashboard backend are different processes). |
| REST `/api/plugins/user-steps` | `GET /ping`, `GET /steps?session=` (open steps, rolling card, own card ids), `GET /step?id=&s=`, `POST /resolve {id, session, resolution}` (also archives the rolling card after the last step). |
| Rolling card | Created in-process with the agent's own `kanban_create` (links the card to the chat; falls back to `hermes kanban create` + `notify-subscribe`), retitled/re-bodied with `hermes kanban edit`, archived with `hermes kanban archive`. |
| Desktop half | `desktop-plugins/user-steps/plugin.js`: the `user-step` transcript directive + the strip in `COMPOSER_AREAS.top`. Kanban rows come from `window.__blockedPanel` (v4: `subscribe`, `unblockCard`, `openCard`, `checklist`); blocked-panel hides its own composer panel when `window.__userSteps.ownsComposer` is set. |

Skipped: Kanban workers (`HERMES_KANBAN_TASK`), subagents, cron/webhook. At most 5 new steps per reply and 12 per
chat per hour. Off switch: `HERMES_USER_STEPS_OFF=1`.

Model: `auxiliary.user_steps.provider` / `.model` in config; unset = `anthropic` / `claude-haiku-5-5` (it runs
before the reply is shown, ~2 s). Env `USER_STEPS_PROVIDER` / `USER_STEPS_MODEL` override both.

Trace: one JSON line per extraction in `~/.hermes/cache/user-steps/trace.jsonl` (source model/regex/skip, new and
resolved ids, framed, errors).

## Limits

- The strip only knows the FOCUSED chat (composer plugins get no surface id); tiles show the focused chat's strip.
- A newly enabled agent plugin is live in a running Desktop backend only after an in-process enable
  (`plugins.manage` toggle, what the Desktop Plugins page does) or a backend restart. Changes to
  `dashboard/plugin_api.py` itself need a backend restart; `core.py` reloads by mtime.
- Cross-gateway: the strip and box call the ACTIVE connection's REST; a chat on another gateway works when that
  gateway has user-steps installed (untested across gateways).

## Tests

```
cd plugins/user-steps && uv run -q --with pytest --with fastapi --with httpx python -m pytest -q -p no:cacheprovider tests
cd desktop-plugins/user-steps && /opt/homebrew/bin/node --test --test-reporter=tap test/plugin.test.mjs
```

pytest (fake LLM, fake board + `state.db` under a temp `HERMES_HOME`): one extractor case per kind (approve / do /
choose / input); mention-only, hypothetical and code-block/quote replies produce no steps (regex fallback and guards
against a model that returns them anyway); resolve by your next message; ledger across compression lineage (and not
across subagents); rolling card create → update → archive through the REST resolve route, new card after archive;
archive failure keeps the pointer; long-paragraph framing never drops text; hook eligibility and fail-open.

node (real React + jsdom, SDK stubbed): attr decoding; buttons per kind; click → `composer.submit` THEN
`POST /resolve`, no composer → nothing resolved, Reply only drafts; strip projection (oldest first, own rolling
cards dropped, also after archive); directive render from the ledger and plain-text fallback when the ledger is
unreachable; strip shows steps + other blocked cards and hides when empty; Kanban rows unblock without a chat turn;
a strip click also flips the in-message box.

Live (real Desktop, throwaway chat, deleted in `finally`):

```
cd plugins/user-steps/tests
US_LIVE_URL=http://127.0.0.1:<desktop backend port> node live-desktop.mjs create   # reply framed, step + blocked card
kill -USR1 <Hermes main pid>; node live-desktop-gui.mjs <key from create>   # open, screenshot, click Done, restore
US_LIVE_URL=... node live-desktop.mjs check     # "✓ Done:" turn sent, step resolved, rolling card archived
US_LIVE_URL=... node live-desktop.mjs cleanup   # always: session.close + DELETE the row
```
