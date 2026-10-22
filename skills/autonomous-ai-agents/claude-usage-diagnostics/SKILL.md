---
name: claude-usage-diagnostics
description: "Use when Claude/Codex usage runs out or accounts show red."
version: 1.0.0
author: Hermes Agent (curator)
license: MIT
metadata:
  hermes:
    tags: [Claude, Anthropic, rate-limits, credential-pool, diagnostics]
    related_skills: [claude-code, token-broker, hermes-agent]
---

# Claude Usage-Limit Diagnostics

## When to Use

- The user reports being "out of Claude usage", or desktop sessions stick on "rate limited — resets in ~X" and endlessly retry.
- New Hermes sessions silently fall back to another provider (e.g. codex) instead of using Claude.
- A Claude account "should have plenty left" per the usage modal but calls still 429 — the contradiction needs reconciling with evidence.

Diagnose from **evidence, in this order**: (1) the verbatim API error in session transcripts, (2) a live model×account probe **calibrated against a known-good client**, (3) the running session inventory. Consult the usage modal last, and never rely on it alone. Every claim you report must point to a verified measurement. If the claim can't be measured, say it's unverified.

## Procedure

### 1. Get the verbatim error from transcripts, not the UI

Session transcripts live at `~/.claude/projects/<munged-cwd>/<session-id>.jsonl`. Map running desktop sessions to IDs with `ps aux | grep 'MacOS/claude'` (the `--resume=<id>`, `--model`, and `--effort` flags are in the command line), then grep the day's transcripts:

```bash
find ~/.claude/projects -name "*.jsonl" -newermt "today" -exec grep -h -oE "[^\"]{0,120}rate_limit, HTTP 4[^\"]{0,150}" {} + | sort -u
```

The message names the exact bucket:
- `You've hit your session limit · resets <time>` — 5-hour rolling window, per account.
- `You've hit your weekly limit · resets <date> <time>` — **per-model** weekly cap; the model is named at the end (`model sent to the API: claude-<model>`).
- `You've hit your <Model> limit` — that model's weekly bucket.
- `This request would exceed your account's rate limit. Please try again later.` — short-window request-rate throttle, not quota.

The reset time in the user-facing "resets in ~2.6d" message derives from this error — reconcile the two before explaining anything.

### 2. Probe the live API with pooled tokens

Hermes pool tokens are in `~/.hermes/auth.json` → `credential_pool.anthropic[]` → `access_token` (opaque `sk-ant-oat01…`). Run `scripts/probe_anthropic_pool.py` (or inline the same request) to probe models × credentials:

- Auth sanity check: a nonexistent model name returns **404**, proving the token authenticates — a 429 is then a genuine limit, not a broken token.
- **Max-plan OAuth tokens need a Claude-Code-shaped request.** Include the system prompt `You are Claude Code, Anthropic's official CLI for Claude.` plus `anthropic-beta: claude-code-20250219,oauth-2025-04-20`. Without them Anthropic returns **429 on healthy accounts**, and that is indistinguishable from a real rate limit. The bundled script already does this. Never hand-roll a bare `/v1/messages` probe.
- **Calibrate the probe before trusting it.** Run it against a credential you know works right now: the one Hermes is actively serving from (`←` in `hermes auth list`), or the account the user says works in the desktop app. If the probe says 429 where real traffic succeeds, the probe is wrong. Fix it before reporting anything.
- `GET /v1/models` with the same Bearer token lists what the account can see.
- **Per-account quota windows:** `GET https://api.anthropic.com/api/oauth/usage` with `Authorization: Bearer <token>`, `anthropic-beta: oauth-2025-04-20`, `User-Agent: claude-code/2.1.0` returns `five_hour` / `seven_day` `utilization` (0-100) + `resets_at` per account. **Check `expires_at_ms` before reading its errors** — an expired access token gets `429 "Rate limited"` here, not 401, so it masquerades as a quota limit. If the resetwatch plugin is installed, `cd ~/.hermes/plugins/hermes-resetwatch/desktop && <gateway venv python> probe.py --fresh` prints a JSON list with one row per pooled Claude/Codex account (plan, windows, reset times) — the fastest full matrix.

`scripts/oauth_usage_all.py` prints these windows for the **Claude Code CLI login** (keychain item `Claude Code-credentials` → `claudeAiOauth`) and every Hermes pool entry in one run. Check both: `claude -p` jobs (OpenEvolve, Claude Code workers) spend the CLI login, not the Hermes pool.

Probe **all models on all credentials** and report the matrix. Partial availability is normal: some accounts are limited, others aren't. **A uniform all-429 matrix is a red flag for a broken probe, not proof that everything is limited.**

### 3. Check the pool state and what actually happened

- `hermes auth list` — per-credential status and cooldown timers. `model_cooldowns` in `auth.json` are **per model**: a credential benched for opus still serves other models.
- `hermes auth refresh anthropic <n>` re-mints tokens and clears stale cooldowns — run it once to rule out drift, then re-probe. It will not lift genuine API 429s.
- When every credential is benched for the requested model, resolution raises "No Anthropic credentials found" and the gateway falls back to the configured fallback provider. That is correct behavior — the pool DID rotate through everything first; say so plainly instead of implying rotation failed.

### 3a. Healthy live account skipped by Hermes

- Inspect `model_cooldowns` as well as credential-wide status: `hermes auth list` can show `ok` while a model-specific cooldown blocks selection. Compare with a correctly shaped live completion before declaring quota exhausted.
- For each account that succeeds live but remains blocked locally, use `hermes auth reset anthropic <exact-entry-id>`; this clears model cooldowns without rotating tokens. Leave genuinely failing accounts untouched. Read back pool state, then exercise `hermes chat --provider anthropic -m <tested-model> --oneshot --ignore-rules -t '' --max-turns 1 --run-budget 45 -Q -q 'Reply with exactly: pool-ok. Do not use any tools.'` and confirm the logged provider is Anthropic, not a fallback.
- An enabled `fast-rate-limit-fallback` plugin may still be inactive: run its `check.py` with the managed Hermes Python and inspect `wrappers_active`. After upgrades, AST compatibility guards can disable wrappers while the plugin list still reports enabled. Do not replace compatibility fingerprints without auditing changed core behavior and rerunning regression tests.

### 3b. Verify account-selection and rotation plugins together

- Test actual configured PluginManager discovery with both account-selection and fast-rate-limit-fallback enabled, not just each plugin's isolated suite. Wrapping a shared recovery function can make another plugin's fingerprint guard refuse installation even when core source is unchanged.
- Require combined recovery and unload/reload tests before recommending activation: verify selected-account wire identity, cooldown preservation, long-reset rotation/fallback, and both installers active. Never bypass compatibility by indiscriminately unwrapping `__wrapped__` or accepting new hashes; explicitly audit supported wrapper composition and reject foreign wrappers.
- Distinguish on-disk implementation, fresh-process discovery, and activation in the owning live gateway/compute child. A mocked browser harness proves UI interactions, not native-app deployment or real provider dispatch.

### 4. Find what burned the quota

Grep transcripts for 429 `task-notification` storms — each names a dead subagent and its model. A parallel subagent fleet pinned to one model can drain that model's weekly bucket in a day. `ps aux` shows which desktop sessions are open and their `--model` flags; an idle session spends nothing, so only sessions actively retrying or spawning count.

### 5. Fixes, in order of speed

1. Switch affected sessions to a model that passes the probe matrix.
2. Wait out the named reset from the verbatim error (session limits: hours; per-model weekly: days).
3. Only if the user asks for capacity beyond their Max plans: a pay-per-use API route on a dedicated identity. Two token-broker options, in the order to try:
   - `anthropic.inference` vends `ANTHROPIC_API_KEY` — but **verify with a minimal `/v1/messages` completion before promising capacity**: a key with no credit balance still lists models on `/v1/models` yet returns HTTP 400 "credit balance too low" on every completion. Model-list success ≠ working key.
   - `openrouter.inference` vends `OPENROUTER_API_KEY` and serves real Claude models as `anthropic/<name>` IDs with dots (e.g. `anthropic/claude-opus-5.5`, not dashes) via `/v1/chat/completions`. Before relying on it, `GET https://openrouter.ai/api/v1/key` shows `usage`/`limit` — the shared key has a hard spend cap and can be nearly exhausted. If it is, `openrouter.manage` (approval tier) can mint a fresh capped key. This route is verified working for opus completions.

## Pitfalls

- **A red Codex row can be a stale duplicate login, not a dead account.** Re-adding an account leaves the old pool
  entry holding its last `usage_limit_reached` until `last_error_reset_at`, while the new entry serves fine. Decode
  each `credential_pool.openai-codex[].access_token` JWT (`https://api.openai.com/auth.chatgpt_account_id`, `exp`) to
  find entries sharing an account, check live with `GET https://chatgpt.com/backend-api/wham/usage` (Bearer token +
  `ChatGPT-Account-Id`; `rate_limit.limit_reached`, `primary_window.used_percent`), then `hermes auth remove
  openai-codex <stale id>` and prove with a one-line `hermes chat -q ... --provider openai-codex`. No re-auth needed.
- **Grey / "0/N" usage chips (any usage widget) usually mean expired tokens, not no capacity.** The pool only refreshes a token when it selects that entry, and every usage fetcher (core and community plugins alike) is read-only, so idle accounts' ~8h access tokens lapse → state `unknown`. Check `expires_at_ms` in auth.json first; `hermes auth refresh anthropic <n>` fixes it now (model cooldowns survive the refresh). The `oauth-keepalive` user plugin (`~/.hermes/plugins/oauth-keepalive`) prevents recurrence; if chips grey out again, confirm it's enabled (`hermes plugins list`).

- **`hermes auth refresh anthropic` requires a target** when the pool holds more than one credential — pass an index, entry id, or exact label (`hermes auth refresh anthropic 1`); the bare command just prints usage. Loop `for i in 1 2 3 4 5; do hermes auth refresh anthropic $i; done` to refresh all.
- **A widget claiming "limit hit, resets in <5h" may just be showing the current 5-hour window's reset.** If `resets_at` matches the widget's time but `utilization` is low (a few percent) and transcripts/run logs show no limit errors, the account is not limited. Say so with the numbers, and ask which widget shows it before debugging the widget.
  - **Hermes's own chip showing 100% / "out of quota" while the raw `utilization` is ≤ 1.0:** Anthropic's `utilization` is already a 0-100 percent, but `agent/account_usage.py` passed `fraction=True` to `_usage_windows`, which multiplies any value ≤ 1 by 100. So 1% reads as 100%. It's display-only: neither the credential pool nor account-selection reads these windows. Check `git -C ~/.hermes/hermes-agent diff agent/account_usage.py` for the local carry. If the chip regresses after `hermes update`, the carry was overwritten and the upstream fix hasn't merged yet.
- **Never argue from the usage modal** — it shows session limit, weekly all-models, and only *some* per-model buckets; per-model weekly caps and request-rate throttles can be invisible while calls still 429. Quote the transcript error, then verify live.
- **When the user's direct observation contradicts your measurement, distrust the measurement first.** "It works in the desktop app on account X" while your probe says X is 429 means your probe differs from the real client. Find that difference. Don't invent a server-side theory (per-token buckets, hidden throttles) to explain away what the user is seeing.
- **Never kill processes on a theory.** Before killing anything as "feeding the rate limit", confirm it actually calls the Anthropic API: check its command line for `claude`, an SDK, or an API host. SSH/staging loops and watchers don't consume quota. Killing them destroys the user's work and helps nothing.
- **The Claude desktop app is single-account** — no pool, CLI, or credential tool can rotate its login. Stuck desktop sessions need a model switch inside the app or patience, nothing else.

## User-specific rules

- No account-swap CLIs (claude-swap/cswap were deliberately removed). The Hermes credential pool (`hermes auth add anthropic`) is the sanctioned multi-account path; do not suggest or reinstall swapping tools.
- The user's Max plans in the Hermes pool are the primary Claude path. Fix or explain the pool first; don't pivot to paid API keys (OpenRouter, broker Anthropic key) or file approval requests for them unless the user asks for extra capacity.
- Prefer Nous providers or token-broker keys for extra capacity over accumulating more OAuth accounts.
