---
name: hermes-coach
description: "Use when mining Hermes sessions across hosts for usage patterns. Digests, feed, coach Bot."
version: 0.1.0
author: daveey
license: MIT
metadata:
  hermes:
    tags: [hermes, coaching, usage-analysis, sessions, power-user]
    related_skills: [hermes-training, ambitious-requests]
---

# Hermes coach: cross-host session mining

Collects a compact, redacted digest of every Hermes session on every host and feeds it to a coach Bot,
which finds anti-patterns and proposes improvements. Phase 1 of a measure → detect → nudge → adapt loop.

## When to Use

- Setting up or debugging the digest feed on a host, or adding a new host.
- Asking the coach-bot questions about usage across all machines ("which sessions were repeat-fix loops?").

## Pieces

| Script | Runs on | Does |
|---|---|---|
| `scripts/session_digest.py` | every host | `state.db` (+ `profiles/*/state.db`) → `<out>/<host>/<profile>/<id>.json`, rewritten only on change |
| `scripts/coach_feed.py` | hosts that push | pull → digest → commit `<host>/` → push, to a private git repo (`--exclude-profile` skips shared bots holding other people's chats); first runs the self-heal machine-signal forwarder (`plugins/coach/signals.py forward`, ssh to the hub, no-op without `hub_ssh` in `coach/selfheal-config.json`) |
| `scripts/coach_mine.py` | the coach | KPIs per host + overall (this window vs previous; interactive = allow list `INTERACTIVE_SOURCES` desktop/tui/cli/slack/telegram/discord/whatsapp/signal with a user turn, so bot sources like `bb-reviewer`/`taxprep-*` never dilute p50/p90; those print on an `other_sources:` line with counts + cost and land in the `kpis.jsonl` row), top-N unlabeled sessions by waste score; digests matching `DIR/exclude-profiles.txt` / `--exclude-profile` `HOST/PROFILE_GLOB` (shared team bots) are dropped at load; `--write-kpis` writes today's row of `reports/kpis.jsonl` (a rerun replaces it); `--write-adoption` evaluates the coach plugin's curriculum `adopted` tests (public file + `curriculum/*.yaml` overlays) into sticky `reports/adoption.json` (needs PyYAML or ruamel.yaml) |
| `scripts/coach_weekly.py` | the coach | weekly context: the 6 plan KPIs (latest `kpis.jsonl` row vs the row a week earlier, gaps recomputed from digests), top findings by est. waste, spend vs output per agent (host/profile × cron job / kanban / chats / source: $ vs the previous window, no-op share from `[SILENT]`/"nothing to fix" last replies, flags for mostly-no-op agents ≥ $5, crons repeated across hosts, doubled spend), `--check key=value@UTC#signal` before/after verdicts (keep / raise / revert), the self-heal lane (filed / proposed / approved / fixed / reverted / not-a-defect, recurrence, $ per fix from `selfheal_*` coach events + `selfheal:<component>-<hash>` cards, or the hub's `reports/selfheal-lane.jsonl` written by `--selfheal-export`; "no activity" when there is none), proposal state + rejected list |
| `scripts/coach_proposals.py` | both | `add` parses ```` ```coach-proposal ```` JSON blocks from a report into the append-only `reports/proposals.jsonl` (drops anything matching a rejected fingerprint) and assigns the rollout `tier` (auto / canary / review from kind+target+cost+risk; a block's `tier` may only raise it); `list`, `rejected`, `mark` |
| `scripts/coach_apply.py` | the user's machine | `coach-apply` engine: show/apply/reject/revert, and `auto` = the daily rollout pass (auto tier: gate then apply; canary: this host only + a before/after `check`, promote on KEEP, revert on REVERT; review: a Kanban card; `kind: fix`: the fixer lane; pending > 7 days with a failing gate: dropped with the reason). Tests: `test_coach_apply.py` |
| `scripts/coach_fixer.py` | the user's machine | fixer lane: `fix` proposals (pattern slug, ≥2 sessions, hypothesis, acceptance) → one `coach-fix:<slug>` card a day (`--max-runtime 3600`, created_by coach) while fixer spend today < $7; spend = coach cards' worker-session cost per UTC day, logged as lane `fixer` in `research/state/spend.jsonl`. `spend` / `gate` CLI. Tests: `test_coach_fixer.py` |
| `scripts/coach_eval.py` | the user's machine | replay eval that gates a change before rollout. `detector`: a threshold / judge-prompt (`COACH_DETECT_TAG` detect file) / prefilter-module variant scored against the labeled reply-check and fix-loop sets (`~/.hermes/coach/{replyset,fixloop}`); score = F0.5 when precision ≥ 0.75, else 0; free. `behavior`: the frozen benchmark (`<data>/eval/bench-v1.jsonl`, 36 real episodes: handoff, confirm, unverified_done, repeat_fix, unwatched, control; `bench-build` mines a new version) replayed under baseline vs candidate instructions (AGENTS/SOUL/skill/memory edits applied only in a private eval home `~/.cache/coach-eval/root`, layered into the system prompt like Hermes does) with one bounded provider request per call (`coach_llm_worker.py` under the Hermes interpreter: Hermes credentials + price table, no tools/retries, explicit max_tokens), graded blind by an LLM judge; PASS needs a target gain ≥ max(2, n/4) (run-to-run noise is ±2 of 6), `--pool` / `verdict` combine repeated runs; hard cap: each call's worst case (bytes-bound input + max_tokens × price table) is reserved in `spend.jsonl` lane `trial` before dispatch and settled to the measured cost after, so `--budget` (default $5) and the $10/day lane can't be exceeded. Exit 0 PASS / 1 REJECT / 2 INCOMPLETE. Tests: `test_coach_eval.py` |
| `scripts/coach_trial.py` | the user's machine | trial lane: the weekly digest's one `trial` proposal (idea, ≥2 gap findings, pattern, candidate) → one goal-mode `coach-trial:<slug>` card a day (2 h) while the lane's spend (trial cards + eval runs) today < $10; the card builds the idea behind a flag, runs `coach_eval.py`, attaches the report, and on PASS registers the real proposal with its tier |
| `scripts/coach_askgate.py` | the always-on hub (cron `coach-kpi-weekly`, Mon 15:30 UTC, via a wrapper that also pushes `reports/askgate/` + `reports/selfheal-lane.jsonl`) | two weekly KPIs (Mon-Sun PT) from the cross-host digests, each hit with session + date + quote, target 0: `ask_me` (David's "why are you asking me / I already did it / do it yourself") and `had_to_ask_fix` (David asking for a Hermes defect to be fixed or root-caused: "make a card to fix", "root cause", "figure out why", "broken again", "why did you not…", screenshot + "fix"; interactive chats only, ask-me wins a tie); `--weeks 6 --baseline FILE`, `--last-week --post --selfheal` (ONE Inbox card, key `ask-gate-weekly:<monday>`, with the self-heal lane section; writes the marker `reports/askgate/<monday>.json`, and a host that finds another host's marker skips its post). With `--selfheal` every hit the self-heal lane has no filing for goes to it as a monitor finding (`signals.py`), and the card keeps its Do-it review prompt only for hits the lane did not take (all of them in shadow). Tests: `test_coach_askgate.py` |
| `scripts/engine_budget.py` | the user's machine (+ gates everywhere) | one hard $20/UTC-day cap over every lane (scout $3, fixer $7, trial $10, `bot` = the coach Bot's own turns), plus lane `selfheal` ($25/UTC day, its own cap outside the $20 total; cards keyed `selfheal:<fp>…`, see `plugins/coach/README.md` "Self-heal lane"): `snapshot()` sums the ledger + this host's live coach-card cost; every gate (`coach_fixer.gate`, `coach_trial.gate`, `coach_eval.Budget`, the scout's `x_budget_left`) refuses at its lane cap OR the total. `watchdog` (each `coach-apply auto` + launchd every 15 min): a RUNNING engine card over its card budget (`Budget [for this card] ≤ $N` in the body, else the lane cap) is blocked needs_input (no auto-resume: raise its Budget line or close it), one stopped on its lane/total cap is scheduled to the next UTC midnight; either way its worker is SIGTERMed; never a non-engine card; stops logged to `research/state/budget-stops.jsonl` (weekly digest lists them), Inbox `engine-budget:<day>` with a raise-the-cap Do-it (`budget-overrides.jsonl`); cap stops resumed the next UTC day. Tests: `test_engine_budget.py` |

The private repo layout is `<host>/<profile>/<session_id>.json`. Each host writes only its own subtree, so pushes never conflict.

Digest = session stats (msgs counted from `messages`, since `sessions.message_count` goes stale), cost,
subagent children folded into the root, user turns (first 10 + last 50, ≤500 chars; each with `prev`, the agent reply before it clipped to its
first 150 + last 450 chars, v6, for the self-heal backstop judge), tool counts, tool-error
counts, pattern signals, `features` (content-free feature-use counts from tool-call args: kanban_create options,
todo, cron/profile/board creation, worktrees; v4), and the last reply (≤800 chars). Removed: injected blocks (`<memory-context>`,
system notes, pasted content), Hermes-written user-role messages (background-process notices, compression
summaries, which are counted as `synthetic_*` signals), base64 image parts, and secrets (provider keys, GitHub/Slack
tokens, JWTs, private keys, `token=…` values with digits, long opaque blobs). Home paths become `~/`.
Never included: tool output, system prompts, reasoning.

## Setup on a host

```bash
git clone git@github.com:<you>/hermes-coach-data ~/code/hermes-coach-data
/usr/bin/python3 <skill>/scripts/coach_feed.py --host <name>        # first run backfills 45 days
```

Schedule it every 30 min (launchd `StartInterval 1800` on macOS). Check `git log` in the data repo.
A host with a non-default home passes `--home` (e.g. `~/.bb-hermes`). Profiles are discovered automatically.

## Running the coach Bot

A Bot profile on an always-on gateway owns the model work. Two crons: a no-agent feed for that host's own sessions
(silent unless broken, `--deliver local`), and a daily no-agent prerun that pulls, digests, runs
`coach_mine.py --write-kpis` and prints the labeling queue plus numbered instructions with `--deliver bot-chat`.
The Bot labels per `references/labeling.md`, writes `findings/` + `reports/`, then commits through a push helper.
Put the KPI goals and the user's learning track in its SOUL.md so every report names the next drill.
A weekly cron (Sunday, after the daily mine) prints `coach_weekly.py` output with instructions; the Bot writes
`reports/weekly-<date>.md` with 1-3 `coach-proposal` blocks (`{kind: skill_patch|config|shared_config|curriculum|plugin_rule|core,
target, change, evidence, title, short, effect, before, risk, tier, signal}`; `short`/`effect`/`before` are the Inbox
card copy, see the `coach_proposals.py` docstring), registers them with `coach_proposals.py add`, and replies with the summary.
On the user's machine a daily `coach_apply.py auto` (launchd) rolls them out by tier, editing through the normal mod
path (config set / shared config / skill file in the mods repo, committed) and appending each decision with its revert
info; the next digest lists every apply with `coach-apply revert <id>`. The user can still reject or revert by hand.
The user wants every accepted improvement in the Hermes Inbox: `notify_inbox` in `coach_apply.py` posts one card per
proposal (`hermes inbox post --key coach-improvement:<id>`, so canary → kept/reverted updates the same card) from every
path that changes status: auto, canary verdicts, hand apply/revert, and `coach_proposals.py mark` (review-tier cards
mark there, bypassing coach-apply). A new status-changing path must call it too. `coach-apply inbox --days N` backfills.
Card layout ("Now / Before", board `~/design/coach-inbox-cards/`): row title `<Tip|Rule|Code|Setting>: <short>` with a
`Trying · ` / `Kept · ` / `Undone · ` prefix only off the plain applied state; body = **Now** (effect) + **Before**
(the problem), then the literal change (the tip as a `[!TIP]` alert, the rule text added, `key: old → new`, or what
a commit shipped and left out), then one muted line (scope · live time PT · `@session` source · undo · id). The Do-it
prompt goes out with `--prompt-label`, so the card shows one line instead of the whole review prompt. A proposal
without `effect`/`before` falls back to the evidence quote; append a field-only line to `proposals.jsonl` (`{"id",
"short", "effect", "before"}`, no `status`/`at`) to fix the copy of an old one.
The cloud Bot can't read Mac paths: ship any doc that SOUL.md points at (e.g. the plan) into the profile's `work/`
and reference it by that cloud path. Re-ship whenever the source changes.

After changing a script: commit to hermes-mods (the Macs fast-forward through sync), then re-ship the changed files
to nous-1 `/opt/data/profiles/coach/scripts/` (skills there sync from git on their own; `nous-push` ships plugins only).
Recipe: one local script per ship, each file as `base64 -d > <name>.new <<'B64' … B64`, then print `wc -c` and
`diff <name> <name>.new | grep -c '^[<>]'`; run it with the gateways skill's `nx.sh`; if the diff is only your change,
`mv` the `.new` files in a second nx.sh call and rerun the script against `/opt/data/profiles/coach/work/hermes-coach-data`.
A new `coach_weekly.py` section is ignored by the Bot until `coach_sunday.py` (`~/code/hermes/hosts/nous-1/coach/`,
commit to `state`) gets a matching numbered instruction: edit and re-ship both. A digest change also needs every
host's feed rerun so all digests share one version (`DIGEST_V`).
The cloud profile has no `coach_apply.py`: guard any import of it from a shipped script with `try/except ImportError`,
or that path crashes there. To re-render an OPEN Inbox card in a new format, `hermes inbox archive <sid>` then post
again (a same-key re-post onto an open card appends an update under the old message); rename the archived copy first,
or the new card's title gets a ` (2)` suffix.

## Inbox card types

Improvement cards are `coach-improvement` typed (`inbox-types/coach-improvement.yaml`, fields `id`): **Review** (the
check-whether-it-helps prompt), **Keep** (resolve; a canary is promoted at once), **Undo** (`task`: runs
`coach-apply revert`), **Not now** (resolve; nothing of that kind + target is proposed for 14 days), **Never**
(resolve; never proposed again, and undone if live). `coach-apply auto` begins with `sync_inbox`: it reads each recent
card's `hermes inbox outcome coach-improvement:<id>` and acts once per outcome, logging it to
`reports/inbox-decisions.jsonl`; Not now / Never are rows in `reports/suppressions.jsonl` (`coach_proposals.suppressed`),
which `coach_proposals.py add` (the cloud Bot too, once it pulls the data repo) and the auto pass honour. A pending
proposal that is suppressed is dropped with the reason. `coach_apply.py decide <id> keep|not_now|never` does the same
from a chat (Review closes the card's buttons); `coach_proposals.py suppressions [--clear KIND TARGET]` lists or lifts
them. A failed revert on Never is logged in `inbox-decisions.jsonl` (detail `FAILED: …`) and not retried. `engine-budget` cards (field `day`): **Raise cap…** (`task`,
input = the new total in USD; the card's agent writes `budget-overrides.jsonl` and unblocks the stopped cards) and
**Leave paused**; the watchdog does not re-post a card for a day whose card got Leave paused (after Raise cap it posts again if the new cap is hit). An inbox plugin
without card types, or a host without these type files, gets the old plain card (the post retries without `--type`).
Undone notices are always plain cards.

## Pitfalls

- xAI `x_search` via the Responses API (outside research scout): send `"tool_choice": "required"` and use
  `grok-4.5`, or the model often answers `[]` from memory without searching (`usage.num_server_side_tools_used: 0`).
  Each call costs ~$0.4–0.7 (`usage.cost_in_usd_ticks` / 1e10 = USD), so log it and cap per day. Citations come back
  empty; the post URLs are in the answer text. Check them with `https://publish.twitter.com/oembed?url=<post>`
  (in testing 21/21 resolved).

- Before recommending a harness change from outside material (a tweet, an article, a scout idea), grep for an existing
  mechanism first: coach detectors (`plugins/coach/fix_loop.py`, `reply_check.py`), core `VALID_HOOKS` in
  `hermes_cli/plugins.py` (e.g. `pre_verify`), `config.yaml` (`delegation`, `auxiliary`), and the target app's own
  code. Write the proposal as a delta on what exists ("make the hint act"), never as a new build, because a
  config-only skim proposes duplicates of features that already ship.
- Verify redaction before the first push to a new repo: `gitleaks dir <repo>/<host>` must report no leaks.
- Hermes writes many user-role messages itself: delegation results, cross-session messages, turn-cap notices,
  `/goal` continuations, `[IMPORTANT: …]` notices, compression summaries, model-switch notes. Counted as user turns,
  they inflated repeat-fix about 10× and the Hermes-meta share about 4×. Extend `SYNTHETIC` whenever a new
  prefix appears (`grep -rhoE '"text": "\[[A-Z][A-Za-z: ]{4,40}' <host> | sort | uniq -c`), and normalize
  per-session rates by real user turns, not by the kept turn sample.
- Sanity-check a new KPI against the raw numbers before reporting it. Weight delegated work by spend: a count-based
  Kanban share (sessions vs orchestrating chats) reads 68% when the cost-weighted one is 14%, because each card is a
  session while one chat can hold 50 subagents.
- Treat the coach's first report as a check of the pipeline itself: it audits the digests and finds metric bugs.
  Fix those first, before acting on its usage findings.
- Keyword signals (`hermes_meta` etc.) hit incidental mentions. Classify a session by title keywords or by the
  signal's share of real turns, never by a raw count ≥ N.
- A new curriculum `adopted` test that needs a fact the digest lacks: add a counter to `call_features()` /
  `CMD_FEATURES` in `session_digest.py` (names only, never argument text), bump `DIGEST_V`, re-ship, rerun every feed.
  Sanity-check new adoption counts against real sessions before trusting them (agent-run commands count too).
- Verify a regex detector's hits (e.g. `coach_askgate.py`) against what the agent said just before, not the quote alone,
  because quotes like "why didnt the agent…" or "…yourself" fire on third-party bots and on handoffs nobody asked for.
  Local sessions: the previous assistant row in `state.db` `messages`. Other hosts: the digest
  `~/code/hermes-coach-data/<host>/<profile>/<sid>.json` has `title`, user turns and `last_reply` only, never the reply
  before a mid-session correction. Read that with `cross_session` action=read, target `@session:<profile>@<host>/<sid>`
  (profile from the findings row or the digest path); a bare `<host>:<sid>` reads the default profile, and the same id
  can exist under several profiles. A compacted session returns only its tail; later replies there often show the
  pattern still recurring, which is stronger evidence than the old quote.
- Reviewing a week's ask-gate hits (the Inbox Do-it): per hit, pull the reply before the correction and grep the skills
  for a symptom fix that already landed. Then run the ask-gate plugin's `core.find_asks` over all those replies: the
  ones it finds no ask in are the root gap, fixed in the plugin with the replies as test fixtures (a card behind any
  in-flight ask-gate branch, not an edit to its worktree). For bot-profile hits, read the bot's `SOUL.md` and prerun
  prompt in `~/code/hermes/hosts/<host>/<bot>/` (branch `state`): a report template that demands an asks section, or a
  stale capability claim such as a too-narrow token scope, produces the asks, and cron runs bypass the ask-gate.
  After narrowing a detector, diff the old and new hit lists and explain every dropped hit. Structural rules like
  "skip a session's first turn" drop real hits, because a new session can open by answering an ask from another session.
- Reviewing the weekly `had_to_ask_fix` hits: replay them through `self_heal.complaint_verdict` the way the backstop
  sees them (digest text, `prev`, `backstop_context`) and through the in-process path, then read the per-stage scores:
  `[0.9, 0.0]` with a `product_bug` strict stage is the cascade's product-bug scoping (a policy question, not a
  detector bug), an empty strict answer is a judge failure. On the hub, run any replay under the cron interpreter
  (`cron.scheduler_script._posix_cron_script_argv`); a bare `python3` lacks ruamel, `aux_judge` raises, and the
  fail-open detector silently scores every turn "no defect".
- Exclude shared team-bot profiles with a glob (`coach_feed --exclude-profile 'alpha*'`): they hold other people's
  conversations, and a Slack follower runs as `source=cli`, so each teammate message counts as a user turn and its
  frustration/stuck/repeat_fix signals inflate the host's KPIs. An exact name (`alpha`) missed the sibling
  `alpha-follow` profile. Defense in depth: list `HOST/PROFILE_GLOB` lines in the data repo's `exclude-profiles.txt`
  (or `coach_mine --exclude-profile`); `coach_mine.load()` drops those digests from KPIs and the labeling queue.
- `[REDACTED]` inside ordinary prose usually means the `token|password …` rule caught a word. That rule requires
  a digit and ≥12 chars to keep false positives down.
- User messages can be JSON lists of multimodal parts. `flatten()` keeps the text and turns images into `[screenshot]`.
