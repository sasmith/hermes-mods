# ask-gate

Makes it mechanically hard for the agent to ask you for something without checking first.

Before a reply that asks you to do, decide or provide something reaches you, a cheap judge model reads the ask next to
what the agent actually did this turn, the live state of the cards involved, what you already said, and what the agent
can do for itself. A stale, repeated, unchecked or avoidable ask is held and the agent is nudged privately to verify or
do the work. You never see the held draft.

## Why (five whys)

One chat (a Gmail political-spam cleanup) produced four wrong asks in a row:

- **I1** "Needs you: the Cloudflare token lacks Email Routing, add it" + a blocked card. A token-broker scope could mint
  that permission itself; the card did exactly that the moment it was told to.
- **I2** "Needs you: revoke Superhuman" + a card titled "(needs David signed in)". It had been revoked long before;
  nobody checked.
- **I3** "Still waiting on you: t_…" about a card that had completed minutes earlier (the completion notice reached the
  chat late; the reply came from memory).
- **I4** "Only open item: cancel the subscription. Want me to…?" after the user had said it was handled; a worker
  handoff's "David's call" leftover was relayed as a question.

1. Why did he get four wrong asks? The agent asked before checking the premise, and answered from memory instead of live state.
2. Why? Every check lives in prose: a skill ("discover credentials, mint scoped tokens"), a skill ("never copy an unchecked premise into a card"), a memory note.
3. Why didn't the prose apply? The model has to choose to load a skill and obey it; neither skill was loaded, and under load rules get skipped.
4. Why was nothing else in the way? Nothing in the runtime sits between "the model wrote an ask" and "the user sees it", or between "the model creates a card that waits on the user" and "the card exists".
5. Root: correctness of an ask depended on the model's discipline, not on a mechanism. Fix: put the check in the runtime,
   where it runs whether or not the model remembers (this plugin), and give it the facts the model lacks (live state, ledger, capabilities).

## What it does

| Layer | Hook | Behavior |
|---|---|---|
| L1 | `pre_llm_call` | Injects a short block each turn: status of every card whose `tasks.session_id` is this chat (read straight from `kanban.db`, all boards) plus asks the user already answered (ledger). One sqlite read; silent when there is nothing to say. |
| L2 | `pre_final_gate` | If the final reply contains an ask (`core.find_asks`: "needs you", "still waiting on you", "your call", "want me to…?", "can you…", "if you'd like me to…", "up to you", a "Needs you:" heading with its list), the judge returns a verdict per ask: `ok`, `already_done`, `already_answered`, `unverified`, `agent_can_do`. Anything but `ok` holds the reply with a private nudge naming the evidence. Two nudges max, then the reply is delivered with a short "could not verify" note. Judge down or garbled → deliver, log, never break a chat. |
| L3 | `pre_tool_call` | `kanban_create` with `initial_status=blocked` or a title/body saying the user must act, and `kanban_block` (not `dependency`/`time`/`transient`): refused unless the text has a `Checked: <tool or command> — <one line of output>` line that matches a tool call made earlier in the same turn (card tools never count as evidence). The judge then reads the body too. Kanban workers: `kanban_complete` handoffs with "David's call / David should / needs David" sentences are judged; any that is not judge-ok **and** backed by a matching `Checked:` line becomes `FYI (unverified, not an ask for David): …`. |
| L4 | ledger | `~/.hermes/state/ask-ledger.sqlite` (not synced). When the user's message answers or dismisses an open ask ("done", "i keep completing it", "stop asking", "you can do it") the aux model classifies it and the topic/answer is stored. L1 shows it in every chat (cross-session); L2's judge treats a repeat as `already_answered`. A cheap regex gates the classifier call, so unrelated messages cost nothing. |

**Terminal hand-offs (L2, no model call).** Before the judge, `handoff.py` looks for a sentence that hands the user a
step a shell could do: an imperative (run / paste / type / enter / add / install…, after a sentence start, a comma,
"please", "you need to", "I'd rather you") aimed at something only a shell does (`sudo`, `ssh`, `authorized_keys`,
a key `.pub`, "terminal", `launchctl`, a `hermes update`…), or a "run this:" line followed by a shell fence, or a
"For you / still open" heading over a `sudo`/`ssh` fence. Unless this turn already put a terminal in front of the user
(`run_in_user_terminal`, or an osascript/`open -a Terminal` window), the reply is held with: *Do it yourself: call
run_in_user_terminal; the user types any password there. Hand back only truly physical steps.* Two nudges, then it
goes to the normal judge. Skipped when the user asked how to do something. Not matched: physical steps (taps, GUI and
web sign-ins, System Settings, hardware), codes pasted into the chat, approvals ("want me to run …?"), past-tense
reports, reversal how-tos ("To take it down, run …") and quoted speech. user-steps loads the same file to drop such
steps. Replay on real replies (private set): 19/19 hand-offs caught, 0/73 false alarms; 2 of ~2,000 recent replies
match, both in turns that had opened a terminal (exempt).

Deterministic first: an ask naming a card that is `done`/`archived` in the database is `already_done` without any model call.

The judge sees: the ask sentences, this turn's tool calls and results, the last 30 user messages, live status of every
card id mentioned, the ledger, and a **capability sheet generated at runtime** (token-broker scope names and tiers —
no values —, ssh hosts from `~/.ssh/config`, signed-in Chrome profile emails from Chrome's Local State, browser vault
entry names). The sheet is built in memory, cached 10 minutes, and never written to disk or committed.

## Needs core hook `pre_final_gate`

L2 uses a core hook `pre_final_gate(session_id, platform, model, final_response, turn_messages, attempt) -> None |
{"hold": True, "nudge": str} | {"append": str}` (fork card t_45d5a361) so a held draft is neither shown nor saved. On
a core without it the plugin still registers (L1/L3/L4 work); L2 is simply never called.

## open-asks interplay

`open-asks` files a blocked card for an ask left in a reply. That would publish an unverified premise as a card. It now
checks `cache/ask-gate/verdicts.json` and files nothing when ask-gate held or could not verify the ask; with no record
(ask-gate absent) it behaves as before. Cards `open-asks` files itself are exempt from the L3 `Checked:` rule.

## Model and config

Default judge: `claude-haiku-5-5` on the `anthropic` provider through Hermes' auxiliary client (task `ask_gate`), so
it rides the rotating Claude pool and needs no new key. Override with `auxiliary.ask_gate` in `config.yaml`, or
`ASK_GATE_PROVIDER` / `ASK_GATE_MODEL`. Typical latency 1-3 s per held-or-judged reply (the first call after a cold
start can take ~20 s); replies with no ask-shaped sentence never touch the model.

Off switch: `ASK_GATE_OFF=1`. Log of every verdict, hold and gate failure: `~/.hermes/logs/ask-gate.jsonl`.
Skipped: Kanban workers (for L1/L2), subagents, webhook platforms, and cron runs, except cron runs of a bot profile:
`ask_gate.bot_profiles` in `config.yaml` (default `[bb-tuner, coach]`; env `ASK_GATE_BOT_PROFILES=a,b`) names the
profiles whose scheduled reports go to the user, and L2 judges those like a chat reply. The hook registers with
`include_background = True` (otherwise core never calls it for cron), and `_gate_eligible` then lets through only
`platform == 'cron'` + a listed profile + not a Kanban worker + not a delegate_task child.

## Ask shapes (what `find_asks` catches)

Beyond "Want me to...?" and "Needs you:": (a) an imperative to the reader: `please <verb>` always, and a bare leading verb only as a fallback/conditional
step ("In that case, check ...") or a chain of steps; plain imperatives that merely mention "you" are not asks (they
held 4.2% of replies). A line/sentence that starts
with open, check, tap, click, press, sign in, log in, go to, change, enable, disable, raise, switch/turn on/off,
install, run, delete (after an optional "Label:" and a leading "On the phone," clause), or `please <verb>`; skipped
when the user's last message asked how to do something (steps are then the answer) and capped at 8 per reply;
(b) a heading that names the reader (Your checklist, What I need from you, To confirm, To clear it, @user, For you)
makes every item under it its own ask; (c) incapability claims ("couldn't" is not matched: past-tense reports describe an attempt) (`I can't|cannot change|check|do|open|fix|reach`,
`Limitation:`, `would need ... doesn't have`), which stand only if this turn's tool log shows the attempt failing,
otherwise the verdict is `unverified` with no model call. An ungrounded `agent_can_do` on an imperative or "I can't"
ask is downgraded to `unverified` rather than `ok`, with a lower confidence floor (0.6).

Why (five whys, week of 2026-10-12: 6 corrections, 6 of the 8 causing replies had no detected ask): the user saw asks
that were not question-shaped; the detector keyed on question/"needs you" wording; that wording was copied from the first
incidents; nothing measured misses against the corrections the coach counted; so each new phrasing escaped. The 8 replies
are now fixtures (`tests/fixtures/week_1012.json`) and `scripts/replay.py` fails loudly if one stops being caught.

## Tests

```
cd plugins/ask-gate && uv run -q --with pytest python -m pytest -q -p no:cacheprovider tests
cd ../open-asks && uv run -q --with pytest python -m pytest -q -p no:cacheprovider tests
```

`tests/fixtures/session_20261021_103941.json` holds the four incident replies (I0 is the first summary, I1+I2 the
card message, I3, I4) and the good replies of the same session, scrubbed of personal names, addresses and domains.
Unit tests use a stub judge; `scripts/replay.py` runs the same fixtures through the real aux model (needs the Hermes
runtime interpreter, command in the script header) and `scripts/fp_report.py` runs the detector + judge over the last N
days of Desktop sessions in `state.db` to measure how many final replies would be held.

False-positive run (2026-10-21, last 14 days of Desktop sessions: 7,092 final replies, 1,388 with an ask-shaped
sentence): the gate would hold 93 replies = **1.31%** (target < 3%). Eyeballed: 21 clear catches, 40 debatable
(offers the agent could just do), 32 false positives (0.45% of all replies). Every hold is listed in
`~/.hermes/kanban/evidence/ask-gate/fp-report.md` (private: it quotes real chats). The judge is a model and
varies a little run to run; the deterministic backstops (live card status, ledger word match, the `Checked:` rule)
do not.

Known borderline: "Steps 3 and 5 need you to approve broader access once in Chrome." is judged `agent_can_do` when a
signed-in Chrome profile exists. The hold only asks the agent to verify or try first.
