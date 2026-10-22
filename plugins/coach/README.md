# coach

One nudge bus for Hermes, so coaching panels never pile up. It has two channels.

## Agent channel (invisible to you)

The plugin watches the agent's own tool calls. When it spots a known anti-pattern, it attaches a one-line hint to the
tool result the model reads next: a `coach_hint` key on JSON results, otherwise a `[coach] …` line. A hint still
pending at the end of a turn rides the next turn's `pre_llm_call` context. Each rule fires once per key per session.

| Rule | Trigger | Hint |
|---|---|---|
| `repeat_failure` | the same (whitespace-normalized) command or code fails 3× | stop retrying, reproduce, form one hypothesis, test it (systematic-debugging) |
| `poll_loop` | `sleep` inside a `while/until/for` loop, or ≥3 sleep/poll calls in the last 10 | `terminal(background=true, notify=true)` or a Kanban card, then end the turn |
| `no_checkpoint` | 40 tool calls in one user turn | write a state summary before more tool calls |
| `live_checkout` | `write_file`/`patch` under the running Hermes checkout, or `git commit/pull/reset/…`, `sed -i`, `npm run pack` there | use `hermes-core-wt new/ship` |

The live checkout is the tree that `hermes_cli` is imported from. Override it with `COACH_LIVE_CHECKOUT`.
The `live_checkout` hint attributes each mutation to its own directory (`git -C`, the last `cd` including one after a
newline or inside `bash -c '…'`, or `workdir`) or to a live path in the same simple command, so `cd other-repo && git
pull; grep x ~/.hermes/hermes-agent/f.py` no longer fires; `$VAR` redirect targets and whole-line `#` comments don't
count. The hint is checked in `transform_tool_result` too, because core's sequential path runs that hook before
`post_tool_call` and the hint used to land on the NEXT (often read-only) call. Backtest over 102k past terminal calls:
hints 296 → 131 (0 new), blocks unchanged at 61 (one false block gone, one newline-`cd` live merge now caught).

**Live-checkout guard (`pre_tool_call`, blocks before running).** A terminal command that would move the live
checkout's HEAD (`git commit/pull/reset/checkout/switch/merge/rebase/am/cherry-pick/revert`, attributed per
command through `git -C` or the last `cd`), repack the running app (`npm run pack`, `hermes desktop --build`) or run
`hermes update` is refused, and the message points at `hermes-core-wt`. Why: each HEAD move makes every backend
report code skew, Desktop restarts, and in-flight turns die in every chat. On 2026-10-13 the MacBook Desktop quit
about 10 times this way. `git checkout -- <file>` (discarding live edits), read-only git, worktrees and scratch
clones pass. A backtest over 37.6k past Hermes terminal calls blocks 43, and every sampled one was a real live-tree
HEAD move or repack. Escape hatch for a change the user explicitly asked for: start the command with
`COACH_ALLOW_LIVE=1`. Blocks are logged as `agent_block` in `log.jsonl`.

### Reply check (`pre_verify`, before the turn ends)

When the agent stops with its final message, `reply_check.py` looks at it once per turn. A regex prefilter
finds candidates. The aux task `coach_reply` then judges them, given the user's ask, the turn's tool log and a
private environment profile (`<HERMES_HOME>/coach/reply_profile.md`: which credential stores, hosts and browsers
this user's agents can reach). A hit keeps the turn going with a nudge the user never sees.

| Rule | Fires when | Nudge |
|---|---|---|
| `handoff` | the reply hands the user a step the agent could do itself (log in, click, run, paste...) | try it first with shell/ssh/browser/vault/token-broker; hand off only what needs hands or judgment, and say why |
| `confirm` | the reply ends with "Want me to…?" about a reversible, in-scope step | proceed, verify, report; ask only before irreversible or external actions |
| `unwatched` | the reply promises to continue after outside work finishes ("I'll write the report once the runs finish", "waiting on CI") and `wake_sources()` finds nothing armed: no live background process for the session or its compression ancestors, no open kanban card, no active goal, and no delegate/kanban/cron call this turn. A row like this gets painted done and its follow-up never runs | arm one `terminal(background=true, notify=true)` watcher on the real completion signal with a time cap (a kanban card for multi-hour work), or say plainly that the user owns the follow-up |
| `unverified` (off by default) | "done/fixed" with no check on the user's real path | verify on the user's path or say what's unchecked. With `diff_review` on it is also judged on turns that changed a git repo, and a hit there runs an Opus review of the diff (see Second opinions) |

Measured on 360 real replies against hindsight labels (the labeler also saw the user's next message). At
threshold 0.7: `handoff` precision 0.91, recall 0.33; `confirm` precision 1.0 on 4 fires, recall 0.11. Held-out
samples are small (n=120), so treat these numbers as early. `unverified` showed no usable signal.
`unwatched` checks wake sources deterministically before the judge runs, so a reply with a live watcher costs no LLM call. Of 3,260 recent Desktop replies, 299 matched the prefilter. On three real cases the judge scored the orphaned "once the runs finish, I'll…" reply 0.85 and scored the user-owned next step and the reply that armed a watcher below threshold. Watchers die when their backend restarts, so a promise made before a restart can become orphaned later; the rule only catches this at turn end.
Settings: `COACH_REPLY_RULES` (comma list or `none`) and `COACH_REPLY_THRESHOLD`. Needs core
`agent.pre_verify_every_turn: true` (fork commit `4c93d9c632`), because `pre_verify` otherwise fires only after
file edits. Nudges are logged as `reply_nudge` in `log.jsonl`; a turn the check ran on without a hit logs `reply_checked` (with the prefilter candidates).

### Fix loops across turns (`pre_llm_call` + `post_llm_call`)

`post_llm_call` remembers each turn's final answer and tool log. On the next user message, `fix_loop.py`
prefilters it (still / again / I don't see it / a bare screenshot ...), and the aux task `coach_fixloop` judges
whether the user is reporting that the agent's claimed fix failed. Confirmed failures count within 8 user turns:

- 1st: hint to reproduce on the user's actual path, root-cause, and verify it the way the user will see it.
- 2nd and later: hint to stop patching, list what was tried plus hypotheses, check the distinguishing evidence,
  consider a read-only second opinion or a clean-restart brief.

The user-channel `still_broken` panel now counts these judge-confirmed failures (2 = Anthropic's "after two
failed corrections") in place of 3 regex hits. Set `COACH_FIXLOOP=0` to go back to the regex.
Backtest on 313 real (reply → user) pairs against hindsight labels (the labeler also saw the next turn), at
0.8: precision 0.84, recall 0.35. The old regex scored 0.47 / 0.15. Held-out fired only 3 times, so its 0.33
says little; most of the disagreements were cases the hindsight labeler excused, like "it turned out to be the
old GIF".

### Second opinions that run (`second_opinion.py`)

The two checkpoints above used to be advice only ("consider a second opinion", "verify it"). With these settings on,
each one now starts a strong-model review itself. Both are off by default, which keeps the old behaviour; the shared
config turns them on.

- **Fix loop → review card.** On the 2nd judge-confirmed failed fix inside one window (8 user turns), the plugin opens
  ONE Kanban card through the `kanban_create` tool handler. That is the path that stamps this session as the card's
  origin and subscribes the chat, so the card shows in the chat's task strip. The card is pinned to
  `model: claude-opus-5-5`, has a 30-minute runtime cap, and is assigned to `default`. Its body holds the original
  ask, each "fixed" claim the agent made and the user's reply to it, the files touched in those turns (from the tool
  log), and the instruction **"trace and report what you observe; do not edit"**. The agent's hint names the card and
  says not to patch again until it has read the handoff (`kanban_show`). Idempotent: the key is
  `coach-fixloop:<session>:<first failure time in the window>`, the card id is cached in the loop state, and a 3rd
  failure in the same window reuses the card. The loop state now persists in `sessions/<id>.json`, so a window
  survives backend restarts and `hermes chat -q --resume` (one process per turn). A refused create, for example from a
  Kanban worker's fenced subprocess, is logged as `fixloop_card_error` and leaves the hint as it was. Logged as
  `fixloop_card`.
- **Unverified + repo diff → diff review.** At `pre_verify`, if this turn changed a git repo (core's
  `changed_paths`, `patch`/`write_file` in the tool log including V4A multi-file headers, or a `git commit` under
  `git -C X`/`cd X`), `unverified` joins the judged rules for that turn only. If the judge then flags it (threshold
  0.7), one review runs on the strong model (aux task `coach_diff_review`). The reviewer sees the ask, the final
  message, the tool log, and the turn's diff: commits made since the turn started, the uncommitted diff, and new
  untracked files the turn wrote. It is prompted neutrally: trace the diff against the claims and report what it
  observes. Its findings come back through the existing `pre_verify` continue path, so the agent keeps working.
  Bounds: at most one review per turn, the diff capped at 60k chars, and a 120 s call timeout. If the review fails
  or the diff is empty, the turn falls back to the next real rule over threshold, or nothing. Logged as `diff_review`
  (repos, findings, diff size, seconds).

Settings (env wins, then config `plugins.entries.coach.settings.<key>`): `fixloop_review` / `COACH_FIXLOOP_REVIEW`,
`fixloop_review_model` (default `claude-opus-5-5`), `fixloop_review_provider`, `fixloop_review_assignee` (default
`default`), `diff_review` / `COACH_DIFF_REVIEW`, `diff_review_max_chars` (60000), `diff_review_timeout_s` (120), and
`diff_review_threshold` (default: the reply threshold). The reviewer model is `auxiliary.coach_diff_review.{provider,model}`
(shared config: anthropic / claude-opus-5-5). Without that key, `fixloop_review_model` is passed.

**Cost, measured (MacBook `state.db`, 14 days to 2026-10-19, 6,717 non-cron turns in 2,484 sessions).** Method:
`eval/second_opinion_replay.py --judge` rebuilds every turn and replays the shipped detectors, including the real
aux judges.
- Fix-loop cards: the prefilter hit 429 user messages. The judge confirmed 16 failed fixes, and **2** of them were a
  2nd failure in a window, so **2 Opus review cards in 14 days**.
- Diff reviews: 579 turns had a "done/fixed" reply and changed a git repo (desktop 249, kanban 328, other 2). Repos
  are checked on today's filesystem, so deleted scratch workspaces don't count, and this undercounts Kanban work. The
  judge scored `unverified` ≥ 0.7 on **1** of them, so **1 Opus diff review in 14 days**. The larger cost is the
  judge itself: up to 579 extra Sonnet `coach_reply` calls (~41/day). Some of those turns already reached the judge
  through `handoff`/`confirm`/`unwatched`.
- The replay eval doesn't regress: `coach_eval.py detector` with the new modules against HEAD gives identical
  numbers on every rule (replyset handoff P 0.91 / R 0.33, confirm 1.00 / 0.11; fixloop 0.84 / 0.35). Its verdict is
  "REJECT: no gain", because these changes add actions, not detection.

### Lessons from corrections (`lessons.py`)

Every real user message is judged by the aux task `coach_lessons`. Bare acks ("go", "do it", "ok"), slash
commands and image-only sends are skipped. The judge runs in a background thread, so the turn never waits, and its
hint arrives with the next tool result. A keyword gate caught only 51% of the 138 corrections in 700 mined
exchanges. The skip list misses none of them and judges ~85% of messages. When a message is a correction, the judge
extracts a standing rule (`rule`, `slug`, `scope`), or nothing if it was a one-off. It sees the known rules, so a
restated rule reuses the existing slug instead of starting a new candidate. Each rule lands in
`<HERMES_HOME>/coach/lessons.json` as a **candidate**. When its slug recurs in a 2nd session it is **promoted**.
A rejected slug stays rejected. A `saved` rule (already in memory) only produces a reminder. The agent gets the rule as a hint right away. Once a rule is promoted, the hint
also tells it to save the rule with the memory tool unless memory already has it. `COACH_LESSONS=0` turns this off.

### Self-heal detectors (`self_heal.py`, shadow by default)

Spots an agent/Hermes defect as early as possible, so a fix can be filed without you asking.
Two entry paths, one result `{p_defect, kind, component, symptom, fingerprint, source}`:

- **Complaint** (`pre_llm_call`): every real message except bare acks, slash commands and Hermes-written notices
  goes to the aux judge `coach_defect`, with the agent's previous reply and that turn's tool log. It counts
  corrections ("you buy the credits"), failed fixes ("nope, still broken"), needless handoffs ("you have ssh"),
  frustration, Hermes or product misbehaviour, and nudges ("resume", "what's going on") after a stalled turn.
  A first-stage score >= 0.6 escalates: a second flash opinion on a stricter prompt and, when
  `auxiliary.coach_defect_confirm` (or `COACH_DEFECT_CONFIRM_MODEL`) is set, a stronger model on the same prompt;
  `p_defect` is the mean of the stage scores. Outside-world problems (`kind: external`) never count.
- **Turn end** (`post_llm_call`): machine signals first, no judge (a provider-error reply, an interrupted turn, one
  tool error repeated 3×). Then, when the reply admits a mistake, hands you a step, or the turn's tool log shows
  errors, a turn-end audit by the same judge; only self-admissions file (the other audit kinds cost more false alarms
  than they catch), at `COACH_SELFHEAL_AUDIT_THRESHOLD` (default 0.9, after the confirm model when set).

Both judges append the private overlay `COACH_REPLY_PROFILE` / `<HERMES_HOME>/coach/reply_profile.md` (shared with
the reply check), so they know your standing rules.

Both run in a background thread (the turn never waits) and are fail-open. They skip cron, subagents and Kanban
workers. `COACH_SELFHEAL=off|shadow|live` (default `shadow`): shadow logs a `selfheal_detect` event to
`coach/log.jsonl`; live also appends the defect to `coach/selfheal_queue.jsonl` for the self-heal lane, once per
fingerprint per UTC day (`coach/selfheal.json` holds the fingerprints). The fingerprint is a hash of the component
and the symptom's stemmed content words, so "sidebar sessions reshuffle" and "session sidebar reshuffles" match.
`COACH_SELFHEAL_THRESHOLD` (default 0.6). Set the judge model with `auxiliary.coach_defect.provider` / `.model`
(evaluated on `gemini-3.8-flash`) and the confirm model with `auxiliary.coach_defect_confirm` (evaluated on
`claude-sonnet-5-5`).

Replay evals:
- `eval/selfheal_incident_replay.py` (`run`, then `score --relabel --report`): the production code over an
  incident-labeled turn set (episodes + five-whys verdicts + every normal turn), scored like the self-correct replay:
  caught before any fix ask, caught counting the fix ask, false positives per normal turn, every miss with its quote,
  every false positive, catch per root-cause cluster.
- `eval/selfheal_replay.py`: per-turn catch rate / precision on a hand-labeled set (`--mode admission` for the audit).
The real sets hold your own words, so they live in the private coach data repo.

### Self-heal lane (`self_heal.py file_defect` → investigation → Inbox proposal → Do it → fix)

Every new fingerprint (first time that UTC day) goes to `file_defect(d)`, from any entry path: the two hooks above
(complaint, admission, turn-end machine signal), the digest backstop, and direct callers (machine-signal sweepers,
monitors). `d` = `{fingerprint, component, symptom, source, kind, p_defect, session_id, quote, host, at, details}`.

- **Where it decides.** On the hub (`coach/selfheal-config.json`: `hub_host`, `hub_ssh`, `board`, `bb_board`; no hub
  configured = this host) it decides locally; on any other host, live mode pipes `d` to `self_heal.py file --mode live`
  on the hub over ssh, so dedupe, recurrence, suppression and budget live in one place (`coach/selfheal_lane.json`).
- **Shadow** (`COACH_SELFHEAL=shadow`, else `mode` in `coach/selfheal-config.json`, default shadow) logs `selfheal_would_file` with the full card args and creates
  nothing. **Live** creates the investigation card.
- **Investigation card**: key `selfheal:<fp>`, created_by `self-heal`, model `claude-opus-5-5`, 1 h, skills five-whys +
  hermes-mods-repo (Baby Bear: board `bb`, five-whys + bb-self-fix). Its brief makes it investigate live, with a
  per-component checklist (Kanban events/runs, surface writer + backing state, processes/launchd/logs, git log), write
  the five-whys chain, and post ONE Inbox card of type `selfheal` (key `selfheal:<fp>`): **What happened** (symptom,
  PT time, `@session` link, quote), **Why** (the chain with evidence), **Plan** (changes, regression test, detection
  guard, live check), cost/risk, undo. It never ships the fix. Not a defect → `event not_defect`, no Inbox card.
- **Do it** resolves the Inbox card; the hub cron (every 5 min) sees the outcome and runs `file_fix` (also `self_heal.py fix <fp>` by hand): fix card `selfheal:<fp>:fix` whose acceptance is the
  regression test red before / green after, ship under the standing approval, verify live, and re-post the same key
  as a `selfheal-result` card "Fixed: …" with the proof (OK / Undo). **Not now** snoozes the fingerprint 14 days,
  **Never** suppresses it. (Do it is not a Kanban `task` action because a worker is fenced from creating cards via the CLI.)
- **Recurrence**: a fixed fingerprint that comes back files `selfheal:<fp>:r<n>`, whose brief says "previous fix <id>
  didn't hold" and requires a detection-level guard.
- **Budget**: engine_budget lane `selfheal` ($25/UTC day, its own cap outside the $20 engine total; spend = these
  cards' worker sessions), ≤ 3 running, ≤ 1 filing per fingerprint per UTC day. Over the $ cap the card is created
  and scheduled to the next UTC midnight; at 3 running, 1 h out. Each card body carries a `Budget for this card` line
  ($8 investigation, $15 fix) for the engine watchdog.
- **Hub cron** (`~/.hermes/scripts/selfheal_lane.py` → `self_heal.cron`, every 5 min, no agent): Inbox sync, queue drain, and every 25+ min the digest backstop (the complaint detector over
  new user turns of interactive sessions in the coach digests, per-session cursor; the judge gets each turn's `prev`
  agent reply from digest v6 plus the session title and the 3 earlier user turns, since without them the strict stage
  scored context-free reports like "why didnt this work [screenshot]" as new issues), Inbox sync. In the cascade a
  stage whose answer is empty or unparseable is retried once and then left out of the mean, not counted as a 0.
- Events (coach `log.jsonl`, read by `coach_weekly.py`): `selfheal_would_file`, `_filed`, `_recurrence`, `_skip`,
  `_proposed`, `_approved`, `_fixed`, `_reverted`, `_not_defect`, `_declined`. Cards report theirs with
  `self_heal.py event <kind> <fp> --card <id>`. `self_heal.py status` prints the lane state and budget.
- Tests: `tests/test_self_heal_lane.py` (each entry path, dedupe, recurrence, budget, bb routing, shadow vs live,
  Do it → fix card, Not now / Never). `COACH_SELFHEAL_NO_EXEC=1` (set by the test conftest) refuses any real
  `hermes`/ssh call.

### Machine-signal sweeper (`signals.py`, no agent, no LLM)

Raw machine signals become `file_defect` calls with `source: signal` and stable fingerprints:

| kind | source | fingerprint (host in it?) |
|---|---|---|
| `errors` | new tracebacks in `<home>/logs/errors.log` (any level), minus tool output (`agent.tool_executor`) and provider transients (429/529/overloaded/timeouts) | exception type + innermost 4 frames as `file:function`, no line numbers (no) |
| `hook` | `Hook '<hook>' callback <cb> raised: …` (plugin hook exceptions) | hook + callback + exception type (no) |
| `cron` | a job whose sticky `last_failure.at` is new (`cron/jobs.json`), even if it has recovered since | job name (yes) |
| `kanban` | a card with ≥ 2 runs `crashed` / `timed_out` / `spawn_failed`, every board db; self-heal's own `selfheal:` cards are skipped | board + card id (yes) |
| `gateway` | `gateway.previous_unclean_exit`, `asyncio.run.exception`, `gateway.exit_nonzero` in `logs/gateway-exit-diag.log` (a clean exit or a code-75 restart is not a crash) | tag + exception type (yes) |
| `coach` | coach `fixloop_card` (2nd confirmed failure of a fix) and `reply_nudge` with rule `handoff` | session (+ issue) (no) |
| `monitor` | a propose-only monitor's finding (`route-monitor`, below) | the monitor's own dedupe key |

Profiles (`<home>/profiles/*`) are swept too. Cursor `<home>/coach/signals-cursor.json` (offset + inode per log, last
run id per board, last failure per job); first sight looks back 7 days; a rotated log is re-read; ≤ 25 signals per
kind per pass, same fingerprint folded into one (`seen Nx`).

- **Hub**: cron `self-heal signals (hub)` (`~/.hermes/scripts/selfheal_signals.py` → `signals.py --host hermes-hub
  sweep`, every 30 min, from the scheduler since a Kanban worker can't create cards). It also drains
  `<home>/coach/signals/spool.jsonl`, where the other hosts' signals land.
- **Macs / mini**: `signals.py forward` ships new raw signals over ssh to the hub's `signals.py ingest` (the hub
  files them); the cursor only moves when the hub accepted the batch. Every pass sends a heartbeat first (even with
  no signals), recorded in the hub's `coach/signals/hosts.json`; the hub sweep files a `plugin` defect for a host
  whose forwarder has been silent > 6 h. It rides on the existing launchd coach feed
  (`hermes-coach/scripts/coach_feed.py`, every 30 min) and is a no-op without a `hub_ssh` in
  `coach/selfheal-config.json` or on the hub itself. The ssh to the hub pins the key (`hub_ssh_key` in that file,
  default `~/.ssh/id_ed25519`) with `IdentitiesOnly=yes`, because launchd jobs offer different default identities
  than a shell and the hub refuses them preauth. Each outcome prints one line, which the feed records as
  `<host>/feed-status.json` in the coach data repo (`signals`: ok / no-hub / hub / error, plus the error text and
  `since`; rewritten only when that state changes). That file is the hub's view of a forwarder that has never
  reached it, which the silent-host check can't see.
- **Monitors**: `signals.py route-monitor` (JSON `{monitor, key, title, body, prompt, component}` on stdin) files the
  finding and prints `{"owned": …}`. `owned` = the lane filed it or already has it open; then the monitor posts no
  Do-it card and the investigation posts the What happened / Why / Plan proposal. In shadow mode (or on any error)
  it is not owned and the monitor posts its old card. Callers: mac-worktree-gc (leaks, GC abort, stale report),
  guard-conformance, and `coach_askgate.py --post --selfheal` (each hit the lane has no filing for; the weekly card
  keeps its review prompt only for hits the lane did not take).
- Tests: `tests/test_signals.py` (one per source with fixtures in `tests/fixtures/signals/`, cursor, rotation, hub
  sweep in shadow, forwarder success/failure, ingest, monitor routing and `lane_owns`; also passes under Python 3.9,
  the Macs' `/usr/bin/python3`).

## User channel (Desktop panel above the composer)

Each panel has one primary button and Dismiss. No menus.

| Rule | Trigger | Button |
|---|---|---|
| `still_broken` | 3rd user turn like "still not working / same error / didn't work" | **Restart clean**: a new session whose first message is a brief (Goal / Context / Tried so far / Repro / Done when) + an `@session` link |
| `long_session` | over 250 messages (counted across compressions) | **Spin out**: runs the spin-out plugin's planner, and its panel takes over. **Compact** (secondary): runs `/compress` in the session |
| `screenshot_midturn` | an image with fewer than 3 words, sent while a turn runs | **Add a line**: prefills "About the screenshot: " |
| `move_to_board` | the 10th subagent in the session, or a job over 10 min in chat (a tool call that ran ≥10 min, or a background job still being polled 10 min after it started) | **Move to board**: a handoff card (brief + `@session`) on the active board, made with spin-out's `kanban_cards` |
| `hermes_bug` | the same Hermes failure signature (an exception in a Hermes tool, a traceback from Hermes code, or a Hermes-infra tool error such as `cross_session`) in 2+ sessions within 7 days | **File it once**: one card on the `hermes` board (idempotent per signature, never offered again) |

### Guardrails (one gate, server side)

- At most 1 nudge per session per 15 user turns.
- A dismissal mutes that rule for the session.
- 3 dismissals of a rule within 7 days demote it to digest-only. It is then logged as `digest` and not shown.
- Everything goes to `<HERMES_HOME>/coach/log.jsonl`: every shown nudge, every choice (`accepted` / `dismissed` / `expired`), every digest-only suppression and every agent hint. `GET /api/plugins/coach/stats?days=7` reports precision per rule as accepted ÷ (shown − expired), which is KPI 4.

### Feature curriculum (tips)

`curriculum.yaml` lists features to learn: each entry has a live `trigger`, a one-line `tip`, an optional `try`
drill, the `commands` it names, and an `adopted` test. A private overlay at `<HERMES_HOME>/coach/curriculum.yaml`
adds entries or overrides them by `id` (`enabled: false` drops one). The file header documents both mini-languages.

| Tip | Trigger | Retires when (14 days) |
|---|---|---|
| `project_bot` | 6th session in the same repo in 14 days | 3 interactive sessions in a non-default profile (a Bot) |
| `kanban_max_runtime` | a job over 10 min in chat | `kanban_create` with `max_runtime_seconds` in 3 sessions |
| `todo_checklist` | a turn with 3+ list items, and no todo use yet | todo used in 3 sessions |
| `cron_whenever` | "every day / whenever / each time" | a cron job created in 3 sessions |
| `worktree_parallel` | a 2nd live session in the same repo | 2 sessions in a linked git worktree (sibling `../repo-x` or `.worktrees/`), or 2 worktree cards |
| `session_mention` | "as we discussed / in the other session" | `@session:` typed in 3 sessions |
| `kanban_l1_boards` | "follow-ups / next steps / backlog" | 5 cards created from your own chats |
| `kanban_l2_fanout` | 5 subagents, or 3 `delegate_task` calls | 3 cards with `parents`, or one `kanban swarm` |
| `kanban_l3_review` | "deploy / ship / you said it was done" | 3 `request_review` / `request_changes` |
| `kanban_l3_goal_mode` | 2nd "still not working" | 3 `goal_mode` cards |
| `kanban_l4_triage` | a message from a phone platform, or "at some point / no rush" | 3 triage cards, or 2 `specify` / `decompose` runs |
| `kanban_l4_specialists` | 3 `kanban_create` calls in the session | 3 cards with a non-default assignee |

How a tip shows: tips rank below every bus rule, pass the same gate (rule name `tip:<id>`), and appear as one panel
with **Try it** (pre-fills the drill in the composer) or **Got it**. Spacing lives in `coach/curriculum_state.json`:
after a show, a tip waits 20 h before it can show again anywhere. An accept waits 3 days, and each dismissal doubles
the wait (2, 4, 8 … days, capped at 30). At most 2 tips show per day. A text trigger counts for 2 user turns.
`COACH_TIPS=0` turns tips off.

Retiring: the hermes-coach miner (`coach_mine.py --write-adoption`, a daily no-agent cron on the coach Bot)
evaluates every `adopted` test over the digests from all hosts and writes `reports/adoption.json` into the data repo.
Adoption is sticky. The plugin reads `~/code/hermes-coach-data/reports/adoption.json` (override with
`COACH_DATA_DIR` or `COACH_ADOPTION`), and an adopted tip never shows again. The Mac's coach feed copies the private
overlay to `curriculum/<host>.yaml` in the data repo, so the Bot checks those entries too. Those adopted tests read
the digest's `features` (content-free counts from tool-call args, digest v4), `tools` and `signals`.

### prompt-coach, topic-shift and spin-out share the gate

Decision: they **share the rate limiter but are not folded in**. Their panels offer a real choice (New session /
Fork / Send here; Run new / Replace / Keep), and squeezing that into a one-button nudge would lose it. Each one calls
`window.__coach.claim(stored, rule)` before showing and `window.__coach.report(stored, rule, event)` afterwards.
Both calls fail open when coach isn't loaded.

- Only one bus panel is visible per session at a time.
- Their dismissals mute them for the session, and the weekly demotion applies to them too.
- Their shows use up coach's 15-turn budget. They aren't blocked by that budget, because they respond to a message you just sent.
- spin-out: only its automatic panel goes through the gate. ⇧⌘F always shows.

## Files

- `__init__.py`: the hooks (`pre_llm_call`, `post_tool_call`, `transform_tool_result`).
- `self_heal.py`: the complaint and self-admission detectors, fingerprints and shadow/live handling;
  `eval/selfheal_replay.py` replays them over a labeled set.
- `signals.py`: the machine-signal sweeper, forwarder and monitor router (stdlib, Python 3.9+).
- `agent_rules.py`: the agent-channel rules, plus the signals the user channel needs (subagents, long jobs, bug signatures).
- `core.py`: the file store under `<HERMES_HOME>/coach/` (`sessions/<id>.json`, `bugs.jsonl`, `log.jsonl`), the gate, the user-rule evaluation, the briefs (spin-out's transcript reader + aux task `coach`) and stats.
- `second_opinion.py`: the fix-loop review card and the unverified-diff review (settings, repo/diff detection, the
  neutral reviewer prompt); `eval/second_opinion_replay.py` counts how often each would fire over `state.db`.
- `curriculum.py` + `curriculum.yaml`: the tips (loading/overlay, live triggers, spacing, adoption read).
- `dashboard/plugin_api.py`: `/state`, `/offer`, `/event`, `/restart`, `/board`, `/file-bug`, `/stats`.
- `desktop-plugins/coach/plugin.js`: the panel. It checks after each finished turn, every 60 s while a turn runs, and on screenshot sends.

The brief model is set with `auxiliary.coach.provider` / `.model` (default: the auto aux model).

## Tests

```bash
cd plugins/coach
uv run --with pytest --with fastapi --with httpx python -m pytest -q -p no:cacheprovider tests
PATH=~/.nvm/versions/node/v20.20.2/bin:$PATH bash tests/desktop/run.sh   # desktop flow, mocked SDK + real spin-out
# miner adoption over real data (Hermes venv has ruamel.yaml):
~/.hermes/hermes-agent/venv/bin/python ../../skills/productivity/hermes-coach/scripts/coach_mine.py --data ~/code/hermes-coach-data --top 0 --write-adoption
```
