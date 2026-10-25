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
cards; for `triage` it, `kanban unblock` and `kanban promote` all refuse — use the `specify_triage_task`
move in `kanban-wait-watchdog` (no body rewrite), or ask the user to drag the card to Ready.
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
Go global (the digest script below, all
boards) only when the user says so ("globally", "all boards", "everything") or the session has no cards — then
say it's a global view.

**This user expects the session to track the cards it filed and tell them when one blocks, crashes or gives
up**, not to discover stalls later. After every `kanban_create`, confirm a `tui` subscription (pitfall below).
When the user returns to the project or asks for state, check every card this session filed first and report
any that sat blocked/crashed, with how long; time a card spent waiting unnoticed is the delay they notice.

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
- Cards from `kanban_create` may be subscribed as `platform=local` (mode notify+wake), which the desktop/TUI poller
  ignores (it only claims `platform=tui`), so their blocks never reach the chat. After creating a card, check
  `hermes kanban notify-list <id>` and add `hermes kanban notify-subscribe <id> --platform tui --chat-id <session id>`
  if only a `local` sub exists.
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
