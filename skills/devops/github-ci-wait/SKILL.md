---
name: github-ci-wait
description: "Use when waiting for a pushed commit's GitHub CI or release, or for Actions blocked by billing. Waits on runs/tags containing the commit, never the newest run."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [github, ci, actions, release, wait, background]
---

# Waiting for a commit's GitHub CI / release

## Rule

Never write your own watcher that follows "the newest run" (`gh run list -L 1`) or one specific run id
(`gh run watch <id>`). On a busy branch, concurrency groups replace pending runs and most end `cancelled`.
The run that actually builds your commit is often one started by someone else's later push. Newest-run
watchers miss it and wait for hours while the session looks idle. Ask instead whether any successful run, or
any release tag, *contains* your commit.

## Fixing the CI you wait on: use a worktree

A CI fix (or any branch work) goes in its own worktree: `git worktree add <dir> -b <fix-branch> origin/main`, then
push from there. Never `git checkout`/`switch` a branch in a shared checkout that something runs from (a sync
mirror, a deploy checkout, the live app checkout): the scripts and services in it vanish with the old branch. That
happened when CI-fix workers switched a sync mirror to fork branches and its launchd job failed for 4 hours.
[branch-pin-guard](../../../plugins/branch-pin-guard/) blocks this for checkouts listed in
`$HERMES_HOME/branch-pins.json`. A checkout you find off its branch: commit or stash the stray work onto its own
branch, check the pinned branch back out, and carry on in a worktree.

## Use the script

`scripts/wait_for_ci.py` (in this skill's dir). Run it from inside the repo checkout; it needs `git` and an authenticated `gh`.

```bash
S=<this skill dir>/scripts/wait_for_ci.py
python3 -B $S <sha>                                   # every workflow that ran on a commit containing <sha> passes
python3 -B $S <sha> --workflow ci.yml                 # just this workflow (repeatable)
python3 -B $S <sha> --workflow release.yml --tag-glob 'v*'   # released: a v* tag contains <sha>
python3 -B $S <sha> --once                            # one check: exit 0 done, 2 not yet
```

Exit codes: 0 done (prints the run URLs or tags); 1 stalled (nothing queued or running will finish it,
and the failing run's URL is printed); 2 timed out (default 120 min) or not yet with `--once`; 3 the sha isn't
on `origin/<branch>` (default `main`; `--branch` to change). `--dispatch` (requires `--workflow`) re-runs the
workflow via `gh workflow run` on a stall, at most twice, instead of exiting 1. Pass several shas to wait
until one run or tag contains all of them.

How to run it from an agent:
- Start it with `terminal(background=true, notify=true)` and end the turn. Don't run a foreground sleep loop:
  it hits the tool timeout. Don't poll a background job either.
- In a Kanban worker there is no async notify, and a poll loop burns `max_runtime_seconds` (most worker
  timeouts were exactly this). Run `--once`; if it exits 2, call `kanban_continue(checkpoint=<sha, what to
  check, next steps>, resume_after_seconds=600..1800)` and end the run. The card re-queues itself and the
  next run starts with `--once` again.
- Before telling the user something is "waiting on CI/the release", run `--once`. It has often shipped already.
- On exit 1, investigate the printed run (`gh run view <id> --log-failed`). If an earlier commit by
  someone else broke it, say whose.
- Don't cancel another session's queued run to get yours through: the queued run will contain your
  commit too.

## Notes

- `--repo` is a LOCAL checkout path (the script runs `git -C <repo> fetch`). An `owner/name` slug resolves to `~/code/<name>` only when that checkout's origin is the same repo; anything else exits with "not a local checkout". Run it from inside the checkout or pass the path.

- Workflows with `paths:` filters don't run for every commit. Without `--workflow`, the script waits on the
  workflows that have a run on a commit containing yours. With `--workflow X`, when X never runs it reports a
  stall after the 3-minute startup grace period plus two idle polls.
- Release tags are the most reliable signal when a repo publishes them: a tag on a commit that contains yours
  means CI built and released your change, whichever run did it.
- A repo with its own wrapper (e.g. a `tools/wait_for_release.py` that knows which product tags a path
  maps to) takes precedence; check the repo's AGENTS.md first.
- Tests: `python3 -B -m unittest discover -s tests` from this skill's dir.
- Self-hosted runners keep files between jobs. `gitleaks/gitleaks-action@v2` downloads to the fixed path
  `os.tmpdir()/gitleaks.tmp`, so one interrupted run makes every later install fail with "Destination file path
  ... already exists" on that runner. Fix in the workflow, not on the hosts: set `TMPDIR: ${{ runner.temp }}` in the
  step's `env` (emptied per job). Check every runner's failure first (`runner_name` per job) to tell a systemic
  cause from one dirty host.

## CI refused for billing (account-wide outage)

Symptom: every job fails in 1–4 s with zero steps, across all workflows and repos of the account. The cause is
in the job's check-run annotation, not the log: `gh api repos/R/check-runs/<job id>/annotations --jq '.[].message'`
contains "The job was not started because recent account payments have failed or your spending limit needs to
be increased". This is not a code failure: don't fix-forward or revert anything for it, and tell running repair
workers to stop chasing tests.

1. **Wait for it to clear with this skill's `scripts/actions_billing_probe.sh OWNER/REPO [cheap-workflow.yml]`**
   (don't write a new probe script: this one already handles the in-progress and zero-steps cases), started in
   the background with notify on `ACTIONS OK` / `STILL BLOCKED`. The probe re-runs a cheap Linux workflow, so it
   doesn't depend on someone pushing. "I raised the limit" isn't proof: confirm with the probe before relaying it.
2. **List everyone who is waiting before it clears**, and save the list to a scratch file so it survives compaction:
   - chats: `session_search` on the exact phrases `"payments have failed"` and `"spending limit"`. One search
     misses sessions that only quoted the other phrase.
   - Kanban: read the *latest* `blocked` event reason of each blocked card, not the title:
     `select t.id,t.session_id,json_extract(e.payload,'$.reason') from tasks t join task_events e on e.task_id=t.id where t.status='blocked' and e.kind='blocked' and e.created_at=(select max(created_at) from task_events where task_id=t.id and kind='blocked') and (e.payload like '%spending limit%' or e.payload like '%payments have failed%' or e.payload like '%billing%')`.
     Run it on every board/host that has workers, e.g. a remote host's board db over ssh.
   - The card `session_id`s add the chats that own those cards.
3. **Notify only after the probe passes.** A message arrives as a user turn, so sending it early makes every
   session retry into the same refusal. Once it passes: `cross_session send` each chat (CI is back, resume),
   then `kanban_comment` + `kanban_unblock` each card (or `hermes kanban --board <b> comment/unblock` on the
   remote board).
4. **If the probe keeps failing more than ~30 min after the user's change,** tell the user what you observed (how
   many probes, first and last times), then narrow the cause yourself before handing them pages to click:
   - Scope: list the account's repos' newest runs. Public repos still running while private ones are refused
     means GitHub is blocking all *paid* usage on the account, not one workflow or repo.
   - Usage: `gh api "/users/<user>/settings/billing/usage?year=Y&month=M"` returns per-day, per-repo, per-SKU
     items: aggregate `grossAmount` by (repo, sku) and by day to find the runaway workflow (hosted macOS is the
     usual one). There's no API for a personal account's budgets (`/settings/billing/budgets` is 404): read the
     Budgets, Payment information and Payment history pages yourself with `browser_exec(local=true)` (the user's
     signed-in Chrome) instead of asking for screenshots.
   - Mail: search the user's Gmail (same browser) for GitHub mail since the block began (budget reached,
     payment failed, `[GitHub Support]`). No such mail plus a budget under its cap plus all-successful payments
     points away from budget/charges.
   - "The issue we filed" about billing usually means a GitHub Support ticket, not a Kanban/board card: find its
     id in the `[GitHub Support]` mail and read it at `https://support.github.com/ticket/personal/0/<id>` before
     answering. Check board cards only if no ticket exists.
   - Then the remaining documented cause is a failed card **authorization hold**: GitHub places holds after
     significant metered usage or a budget increase, a declined hold locks billing, and it never appears in
     payment history (that only lists completed charges). Docs:
     https://docs.github.com/en/billing/how-tos/troubleshooting/locked-account. Tell the user to look for a
     declined GitHub authorization at the bank, re-save the card on the Payment information page (that triggers a
     new authorization), and otherwise open a support ticket asking for a billing-lock review.
   - Keep the probe running throughout; it ends the wait the moment any of these clears it.
5. **When billing can't be fixed soon, move repos to self-hosted runners instead of waiting** (registration,
   no-sudo workflow edits, dirty-checkout pitfalls, refused PR reruns): `references/self-hosted-runner-migration.md`.
   Before moving a repo, check its `runs-on` and runners: another session may have moved it already.
6. **Detection:** `~/.hermes/scripts/gh_actions_billing_watch.py` (hourly no_agent cron) alerts on refused jobs,
   daily hosted spend and non-Linux SKUs, posting to the Hermes Inbox itself because Desktop cron has no home
   channel (`deliver=all` fails with "no delivery target"). Run it with `--force` for live state.

## Rescuing a session already stuck on a hand-rolled watcher

Read its last background command (`ps -axo pid,etime,command | grep 'gh run'`). If it follows the newest run or one
run id, check the commit with `--once`. When it has already shipped, kill the watcher. A `zsh -lic` loop ignores
SIGTERM, so use `pkill -P <pid>; kill -9 <pid>` and confirm with `ps -p`. Then send the session the facts: which run
or tag contains its commit, and what to do next. Also tell it to use this script next time.

## Adding helper scripts to a repo

Before choosing a directory for a new tool, read the CI workflows' `on.push.paths` and any plan step's path rules. A
script under a shared or product directory (e.g. `shared/`, `lib/`) can trigger a full rebuild and release of every
product on push. Put agent tooling where no release workflow watches (often `tools/`), and confirm by checking that
no new run started after the push.
