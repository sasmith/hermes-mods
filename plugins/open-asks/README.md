# open-asks

> **Retired: replaced by [user-steps](../user-steps/).** user-steps finds asks with a model instead of a regex
> (this plugin's regex is its fallback), frames them in Desktop replies with one-click buttons, lists them in a
> "For you" strip above the composer, and keeps ONE rolling blocked card per chat instead of one per ask.
> `install.sh` skips this plugin (`retired:` in `plugin.yaml`); disable it where it is enabled:
> `hermes plugins disable open-asks`. The code stays for reference.

Every question a chat reply leaves for the user becomes a blocked Kanban card linked to that chat.

## Why (five whys)

Problem: a coordinator chat ended a turn with "Want me to set up the passwordless rule on the mini?" and made
no card. The chat didn't show as blocked, the user's blocked-cards view was empty, and the ask sat unanswered
behind later completion notices until the user asked why the session wasn't marked blocked.

- Why 1: the session's "waiting on you" state comes only from Kanban: the sidebar paints a session red when
  a card linked to it is `blocked` with a human kind (`hermes_cli/kanban_session_activity.py`,
  `HUMAN_BLOCK_KINDS`). A question in reply text changes no state.
- Why 2: nothing turns an ask into a card. Workers have `kanban_block(kind="needs_input")`, and
  follow-through covers "when X finishes, do Y", but a chat's own questions had no tracked path.
- Why 3: the convention lived only in prose: the `kanban-orchestrator` skill. That skill wasn't loaded in the
  session that failed, and prose rules get skipped under load anyway.
- Root (happened): "waiting on the user" was stored in two places (cards and chat text), and only one is
  observable.
- Root (not caught): no check runs on a final reply to see whether it asks something untracked.

## What it does

| Hook | Behavior |
|---|---|
| `transform_llm_output` | If the final reply asks the user for a decision or action ("Want me to…?", "needs your yes or no", "reply yes", "let me know which…") and this turn didn't already file or block a card, it files ONE blocked card linked to the chat. The card is titled `ASK: …` and has a fixed body: answer in chat, unblock = "yes, go with the recommended option", archive = no. It appends one line to the reply naming the card. |
| `transform_tool_result` | Notes that the turn already did `kanban_create(initial_status=blocked)` or `kanban_block`, so the plugin doesn't file a duplicate. |
| `pre_llm_call` | On the chat's next turn, lists its still-blocked ask cards and tells the agent to archive one once the user's message answers it. It drops cards already closed. |

Skipped: Kanban workers (`HERMES_KANBAN_TASK`), subagents, cron/webhook platforms, asks that name a card already
`blocked`, hypotheticals ("if you ever want…", "no action needed"), code blocks and quotes. At most 4 cards per
chat per hour. If the `ask-gate` plugin held or could not verify the ask (`cache/ask-gate/verdicts.json`, under 5 minutes old), no card is filed: a card would publish the unverified premise. Off switch: `HERMES_OPEN_ASKS_OFF=1`.

The card is created through the agent's own `kanban_create` tool in-process, which links it to the chat. If that
tool isn't loaded, it falls back to `hermes kanban create` + `notify-subscribe --platform tui`.

## Tests

```
cd plugins/open-asks && uv run -q --with pytest python -m pytest -q -p no:cacheprovider tests
```

Fakes only: a throwaway SQLite board under a temp `HERMES_HOME`, and a stubbed card creator. The live path was
checked with `hermes chat -q` (see the commit message).
