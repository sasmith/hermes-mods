---
name: five-whys
description: "Use for failure analysis: why-chain to a fixable root. Any bug, incident, failed task, red CI, bad agent output or postmortem."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [root-cause, failure-analysis, postmortem, incident, debugging, 5-whys, lean]
    related_skills: [systematic-debugging, test-driven-development, requesting-code-review]
---

# Five Whys: failure analysis that ends at a cause you can remove

Taiichi Ohno (Toyota Production System) called repeating "why?" five times "the basis of Toyota's scientific
approach". Each answer becomes the next question, so the chain walks from the symptom down to a cause that,
once removed, stops the problem coming back. Without it, people replace the fuse; with it, they fit the strainer
that keeps metal chips out of the pump that starved the bearing that overloaded the motor that blew the fuse.

The number five is not the rule. The rule is: **keep asking until the answer is a missing or broken process, guard
or design that you can change, and that would have stopped this failure and others like it.** Sometimes that's
three whys, sometimes eight.

## When to use

Any time you explain why something went wrong, before you call it fixed:
- a bug or regression, after it is reproduced (use `systematic-debugging` to get evidence first, then this to
  decide what the fix really is);
- an incident, outage, failed deploy, red CI, or flaky test;
- a failed task, Kanban card, cron job or agent run, including your own mistakes and other agents' wrong outputs;
- an issue closed as "not a bug", "expected", "user error" or "transient" (these need a why-chain the most, because
  they are where the process gap tends to be);
- a problem that came back after it was "fixed": the root is why the earlier fix didn't hold.

Skip it for problems with no failure (planning, feature choices). Scale it to the impact: a one-line typo gets a
two-line chain; an outage gets the full write-up.

## Procedure

### 1. State the problem as an observed fact

Specific, observable, and measured: what happened, where, when, how often, impact. Use the log line, error text,
job id or metric. "Login is broken" is not a problem statement. "Since 14:05 PDT, 38% of `/login` POSTs return 502
from the edge; 0% before the 14:02 deploy of a1b2c3" is.

**When the trigger is an alert, card or Inbox message, first prove that the production path raised it.** Test
fixtures, dry runs and other copies of a job can post the same alert with the same title. Do these checks before
you debug the reported failure:
- Match the alert's creation time against the real job log and journal
  (`journalctl --user -u <svc>`). Did a real run fail at that moment?
- Compare the alert's error text with what the code would really print. A path-less `cannot read board`, where
  production prints `cannot read {db}: …`, is a hard-coded fixture string. Grep the tests for it.
- Read the alert store directly for the post time; for the Hermes Inbox that is `~/.hermes/inbox/inbox.db`,
  table `items`, field `created_at`. Then check git history around that time for test work and fixes already
  committed.

If a test raised the alert, the problem becomes "a test reached a live side effect". Fix and prove it at that
level: snapshot the alert store's row count, run the suite, and confirm the count did not change. Then trigger
one real run (for example `systemctl --user start <svc>`) and confirm its success line, instead of waiting
for the next scheduled run.

### 2. Ask why, one causal step at a time

For each answer:
- **It is a fact with evidence**, not a guess: cite the log line, test, `file:line`, commit, config value or
  measurement. Toyota's version of this is *genchi genbutsu*, go and see. If you only have a hypothesis, label it
  `(hypothesis)`, test it, and don't build further whys on it until it holds.
- **It directly causes the previous answer.** Read the chain backwards with "therefore": "no strainer, therefore
  chips got in, therefore the shaft wore…". If a "therefore" doesn't follow, a step is missing or wrong.
- **It is one step**, not a leap to the cause you already suspected. Jumping straight to a familiar cause is the
  most common way the method fails.

### 3. Branch where causes split

One problem often has several independent causes. When one why has two true answers, branch and follow each.
Always ask two separate questions, each with its own chain:
1. **Why did it happen?** (what made the defect)
2. **Why wasn't it caught?** (why tests, review, monitoring, types or the gate let it through to the user)

For an agent-caused failure, also ask **why did the instructions allow it?** (skill, prompt, tool description or
default that pointed the agent the wrong way or failed to stop it).

### 4. Know when to stop

Stop when the answer is all of:
- **inside your control** (a cause outside it, like a vendor outage, means you stop at the last level you control:
  "why were we exposed to it with no fallback/timeout/alert?");
- **a process, guard or design**, not a person (see Blameless below);
- **removing it would have prevented this failure** and plausibly a class of similar ones.

Never stop at these. Each is a symptom that needs another why:
- "human error", "the agent made a mistake", "the model hallucinated", "forgot to…": why did the system make the
  mistake possible, easy, or invisible?
- "flaky", "transient", "race condition", "timing": what exactly races, and why does nothing tolerate it?
- "not a bug" or "expected behavior": then why was it filed as a failure, and why will it be filed again?
- "bad data" or "misconfigured": why was that data or config accepted?
- "it was already like that": why didn't anything notice?

### 5. Check for recurrence

Search the history (issues, git log, earlier cards, session memory) for the same chain. If it has happened before,
the earlier countermeasure didn't hold, and *that* is the next why. Ries's rule applies: tolerate a mistake the
first time, never the second. The second occurrence needs a systemic fix.

### 6. Countermeasures in proportion

Pick a fix for each level of the chain, sized to the damage (Eric Ries's "proportional investment"): a small
problem gets small fixes at every level, not a rewrite. Usually:
- **Symptom level:** fix the immediate defect (the fuse). Necessary, never sufficient.
- **Root level:** fix the process, guard or design (the strainer, a check that flags any pump without one).
- **Detection level:** make the "why wasn't it caught" chain end in a test, assertion, alert, lint or gate.

Each countermeasure is concrete and verifiable: a regression test that fails before and passes after, a guard you
can trigger, a doc or skill change with its path. "Be more careful" is not a countermeasure. If a root-level fix is
too big for now, file it (card or issue with the chain) and say so in the report; don't drop it silently.

After the countermeasures ship, verify each one against live data before calling the chain closed: the guard's own
telemetry (did it fire, how often, what did it fall back to), not the worker's "done" summary. A guard that "works"
often moves the failure somewhere quieter: a loud error that falls back to a slow path, an estimate that drifted from
the real count, an urgent route parked by an unrelated hold. Each of those is a new why-chain.

### 7. Write it down

Put the chain where the fix lives: PR body, issue resolution, card comment, or postmortem doc. Use this shape:

```
Problem: <observed fact, with evidence>
Why 1: <cause> — evidence: <log/test/file:line>
Why 2: <cause> — evidence: …
Why 3: …
Root (happened): <process/guard/design gap>
Root (not caught): <why detection missed it> — chain: …
Recurrence: <first time | repeat of X; earlier fix Y didn't hold because…>
Countermeasures:
- symptom: <fix> — proof: <test/command>
- root: <fix> — proof: …
- detection: <test/alert/gate> — proof: …
- deferred: <card/issue link, if any>
```

## Blameless

The method looks for broken processes, not people to blame. When an answer names a person or agent ("Sam skipped
the test", "the worker ignored the rule"), the next why asks what let that happen: no check, unclear instruction,
a default that invites the mistake, or no feedback. Five blames are not five whys. This applies to your own errors
too: "I didn't read the file first" leads to "why did nothing stop me from editing unread code?"

## Known weaknesses, and how this procedure covers them

Critics (Teruyuki Minoura at Toyota, Alan J. Card in medicine) point out that five whys:
- **stops at symptoms.** Covered by the stop rule and the never-stop-at list in step 4.
- **can't go past what the investigator already knows.** Covered by evidence-backed answers (step 2): if you
  can't cite evidence, go and get it (logs, repro, instrumentation, `systematic-debugging`) instead of guessing.
- **isn't repeatable: different people get different chains.** Evidence on every step makes the chain checkable.
  For high-stakes postmortems, have a second agent review the chain neutrally ("trace this chain and report which
  steps the evidence supports") instead of asking it to find holes.
- **pushes toward a single root cause.** Covered by branching (step 3) and the separate not-caught chain. For a
  problem with many interacting causes, use a fishbone (Ishikawa) diagram to list candidate causes first, then run a
  why-chain down each branch that survives the evidence.
- **uses an arbitrary depth of five.** Covered by stopping on the step-4 criteria, not on a count.

## Example (software)

```
Problem: nightly export job failed 3 of the last 7 nights with "disk full" on worker-2 (job logs 10/12, 10/14, 10/17).
Why 1: /tmp hit 100% during export — evidence: df in job log, 99.8% at 02:14.
Why 2: export writes a 40 GB intermediate file to /tmp — evidence: export.py:88 tempfile.mkstemp().
Why 3: intermediate files from crashed runs are never deleted — evidence: 6 stale export-*.tmp files, oldest 10/03.
Why 4: cleanup runs only on success (finally-less code path) — evidence: export.py:120, cleanup after return.
Root (happened): temp-file lifecycle tied to the success path; no guarantee of cleanup on failure.
Root (not caught): no disk alert on workers, and no test runs export with a forced failure.
Recurrence: second time; 10/05 fix deleted the files by hand, removing the symptom only.
Countermeasures:
- symptom: delete stale files — proof: df 31%.
- root: tempfile in a context manager, cleaned in finally — proof: test_export_cleans_up_on_failure fails before, passes after.
- detection: worker disk >85% alert — proof: alert fired on a test fill.
```

## Pitfalls

- Writing the chain after the fix is chosen, so the chain is built to justify it. Do the chain first; let it pick
  the fix.
- Answers that restate the question ("it failed because it errored"). Each why must add new information.
- Stopping at the first fact that sounds technical. "The pump shaft was worn" is a fact, not the root.
- Going so deep that the root is outside anyone's control ("because physics", "because the company was underfunded").
  Back up to the last level you can change.
- Skipping the "wasn't caught" chain. It is often the cheapest fix with the widest reach.
- Debugging the failure an alert describes before checking whether a real run raised it. A test or dry run can post
  a lookalike alert while production stays healthy (see step 1).
- Closing a "not a bug" verdict with no why-chain. If the same false alarm keeps getting filed, the filer, tagger or
  classifier is the bug.

## Sources

- Taiichi Ohno, *Toyota Production System* (1988), p. 17 (machine/strainer example); Lean Enterprise Institute
  lexicon: https://www.lean.org/lexicon-terms/5-whys/
- Wikipedia, "Five whys" (bolt/shelf example, criticism): https://en.wikipedia.org/wiki/Five_whys
- Atlassian Team Playbook, 5 Whys (blameless framing, team format): https://www.atlassian.com/team-playbook/plays/5-whys
- Eric Ries, "Five Whys" (proportional investment, tolerate a mistake once): https://www.startuplessonslearned.com/2008/11/five-whys.html
