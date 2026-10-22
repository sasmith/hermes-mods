# Fork CI on self-hosted runners, and the test fixes that made it green

Patches:

- `patches/0173-ci-run-Linux-CI-on-self-hosted-hermes-hub-runners-da.patch`: Linux CI jobs run on self-hosted runners.
- `patches/0176-test-git_pack_tidy-skip-incremental-midx-case-on-git.patch`: skip the `--incremental` midx case on old git.
- `patches/0177-test-relay-shared-metrics-load-tolerant-worker-join-.patch`: load-tolerant relay-metrics timeouts.
- `patches/0179-test-allowlist-reviewed-bare-which-sites-codesign-gh.patch`: allowlist two reviewed bare `which()` sites.
- `patches/0181-ci-python-test-job-provisions-PM-locked-Node-de-flak.patch`: python test job gets the locked Node; two flakes fixed.
- `patches/0182-tests-doctor-isolate-vercel-backend-and-gh-auth-test.patch`: doctor tests no longer depend on the runner.
- `patches/0183-tests-isolate-gateway-service-tests-from-container-r.patch`: gateway service tests no longer depend on the runner.
- `patches/0188-test-make-3-env-sensitive-tests-independent-of-the-C.patch`: orphan-readopt (TUI) and termux-api detection tests no longer depend on the runner env.
- `patches/0189-lint-fix-12-ruff-errors-blocking-CI-daveey-11.patch`: 12 ruff errors (update_cmd_git, startup_resume and their tests) that kept the lint job red.

What you see: the private fork's CI runs (and passes) without GitHub-hosted minutes. None of this changes runtime
behavior; it is workflow wiring and test isolation.

## Behavior

- `scripts/fork-ci-runner.sh` rewrites `runs-on:` of every Linux job (ubuntu-latest / 24.04 / 32-core) in 22 workflow
  files to `[self-hosted, <runner label>]`. macOS, Windows, arm and `${{ matrix.* }}` runners are left alone. It is
  idempotent; rerun it after merging upstream and commit the result.
- `tests-os` stays wired (`tests/ci/test_update_ci_routing.py` asserts it) and is expected red on the fork.
- The python test job passes `toolchain: all` to `./.github/actions/setup-pm`, so it gets the PM-locked Node (apt's
  Node 18 cannot strip `.ts` or `require()` ESM, which several tests need).
- Tests now tolerate a loaded runner or a container: relay-metrics worker join and parked-flush waits are 120 s caps
  (healthy runs still return at once); the compute_host hello read allows 30 s; the typing-timeout test turns the
  delivery ledger off and allows 5 s.
- Tests that assume a host install pin `is_container()` to `False` (doctor vercel check, gateway service detection,
  multiplex served record), and the doctor gh-auth test stubs `_safe_which`.
- `test_kanban_restart_survival` waits for a numeric pid in `worker-ready` (the file is truncated before it is written).
- `test_git_pack_tidy` skips the `multi-pack-index write --incremental` case on git < 2.47 (exit 129 there).
- `tests/fixtures/resolution_allowlist.json` lists `_prebuilt_desktop_app_mismatch`, `_prebuilt_signature_error`
  (macOS `codesign`) and `review_session_activity._gh` (`gh` with homebrew fallbacks) as reviewed bare-`which` sites.

## How it works

Pure workflow/test edits; no production code path changes.

## Code

- `scripts/fork-ci-runner.sh`, `.github/workflows/*.yml` (0173); `.github/workflows/tests.yml` (0181).
- Tests: `tests/hermes_cli/test_git_pack_tidy.py`, `test_relay_shared_metrics_runtime.py`, `test_kanban_restart_survival.py`,
  `test_doctor.py`, `test_gateway_multiplex_served_record.py`, `test_gateway_service.py`, `tests/tui_gateway/test_compute_host.py`,
  `tests/gateway/test_run_progress_topics.py`, `tests/fixtures/resolution_allowlist.json`.

## Tests

These patches are the tests. Verified by the fork's CI going green on the runners; each edited file was run with
`scripts/run_tests.sh <file>` when written. Not re-run when this doc was written (untested here).

## Applying

`git am -3` in order. 0173 is only useful if you run your own runners; replace the label in the script. The
mirror's `runs-on` label is whatever your runners register.

## Gotchas

- Upstream merges re-introduce `ubuntu-latest`; rerun `scripts/fork-ci-runner.sh`.
- Self-hosted runners are shared and heavily loaded, so keep timeouts generous caps rather than tight bounds.

## Plugin?

No. CI config and tests.
