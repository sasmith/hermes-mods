# Sidebar: hide automated / test sessions by default

Patches:

- `patches/0166-feat-sidebar-hide-automated-test-sessions-by-default.patch`: per-profile
  `sessions.automated_sources`, the `automated` row flag, the Desktop filter toggle and the shared predicate.
- `patches/0190-feat-sessions-sessions.interactive_sources-allowlist.patch`: per-profile
  `sessions.interactive_sources` allowlist; every other source is automated.
- `patches/0191-feat-sessions-root-sessions.sidebar_profiles-makes-u.patch`: ROOT-home
  `sessions.sidebar_profiles`; every profile not on it starts automated (`["*"]`) with no per-profile config.
- `patches/0192-fix-profiles-a-clone-drops-the-source-s-sidebar-sour.patch`: `profile create --clone` drops
  the source's sidebar source lists so a cloned bot profile still gets that default.

What you see: sessions created by bots, Slack followers, probes and smoke tests stop appearing in the
Desktop sidebar. The sidebar filter menu has a new checkbox, **Show automated & test sessions**, that
brings them back. A bot session that is waiting on you stays visible either way.

## Behavior

- New per-profile config key `sessions.automated_sources` (list of session sources, default `[]` = upstream
  behaviour, nothing is marked). `"*"` marks every session in that profile's store. Example for a bot profile:
  `hermes -p alpha-follow config set sessions.automated_sources '["*"]' --force`.
- The backend adds `automated: bool` to every sidebar row: the recents, cron and messaging slices and the
  unified list from `GET /api/profiles/sessions` (including `profile=all`), `GET /api/sessions`, and every
  `ProjectTreeSession` from the project-tree RPC (also the all-profiles tree). Each row is judged by the config
  of the profile store it came from, not by the serving process's `HERMES_HOME`, the same rule
  `sessions.sidebar_exclude_sources` uses.
- 0190: per-profile `sessions.interactive_sources` (default `[]` = off). When non-empty, a row whose source is
  NOT in that list is `automated` too, on top of `automated_sources` (which still wins: a source in both is
  automated). That turns the deny-list into an allowlist on your own chat profile, so a new eval, script or bot
  that writes there under a new source (`hermes chat -q --source selfheal-replay`, a `session.create` with
  `source: scratch`) is hidden by default instead of leaking until someone lists it. A row with no source at all
  (legacy) is never flagged by the allowlist alone. Same per-store home scoping. Example:
  `hermes config set sessions.interactive_sources '["desktop","tui","cli","slack","discord"]' --force`; pick the
  list from `select source, count(*) from sessions group by 1` in that profile's `state.db`.
- 0191: `sessions.sidebar_profiles` in the ROOT home's config.yaml (default `[]` = off; a profile's own copy of
  the key is ignored). When non-empty, every profile NOT listed is treated as `automated_sources: ["*"]` unless
  its own config.yaml spells out `automated_sources` or `interactive_sources` (even `[]`, which is how a
  non-listed profile opts back in). So a new bot profile (`hermes profile create mac-ops`) is hidden from the
  sidebar from its first session, with nobody remembering to configure it. Example:
  `hermes config set sessions.sidebar_profiles '["default"]' --force` (root, no `-p`).
- 0192: `hermes profile create --clone/--clone-from/--clone-all` removes `sessions.automated_sources`,
  `interactive_sources` and `sidebar_profiles` from the clone's config.yaml when the root's `sidebar_profiles` is
  set and the new profile is not on it. Otherwise the copied default-profile allowlist would count as the clone's
  own explicit choice and its cli/desktop sessions would paint. Unset root list = the clone keeps them.
- Desktop: while the toggle is off (the default), rows with `automated: true` are dropped from Recents, the
  project lanes/tree and the messaging (per-platform) sections, for every gateway and profile. Rows stay
  visible if any of these is true:
  - pinned (server `pinned`, a local pin, or a pin on the row's lineage root);
  - dot state `needs-input` or `stalled`;
  - status stripe `stuck` (red) or `blocked` (grey ⌘B park, blocked Kanban card, review wait).
- ⌘] / ⌘[ visible-order stepping skips exactly the rows the list hides.
- The toggle is stored in localStorage `hermes.desktop.sidebarShowAutomated` (default `false`). It lights the
  filter-menu "filters active" dot only when ON, so the default does not look like an active filter.
  "Clear filters" / "Reset sidebar view" set it back to off.

## How it works

- `hermes_cli/session_listing.py`: `automated_session_sources(home)` reads `sessions.automated_sources` under
  that home (`set_hermes_home_override` + `load_config_readonly`; returns `[]` if unset or unreadable; accepts a
  comma string too). `is_automated_source(source, sources)` handles `"*"` and is case-insensitive.
  Since 0190 `automated_session_sources` returns an `AutomatedSourcePolicy`: a `list` subclass (the
  `automated_sources` entries, so list callers keep working) with `.interactive` = `sessions.interactive_sources`,
  truthy when either is set. `is_automated_source` applies the allowlist only when it is given that policy.
  Since 0191 it also resolves the store's `(profile, root)` (`_store_profile_and_root` via
  `hermes_constants.named_profile_home`: `<root>/profiles/<name>` = named, anything else = `default` in the root)
  and reads `sidebar_profiles` from the root; an unlisted profile with no explicit key in its raw config.yaml
  (`_profile_sets_source_policy`, `read_raw_config_readonly`, no defaults merged) gets `AutomatedSourcePolicy(["*"])`.
  Every surface (REST lists, sidebar slices, project tree) already funnels through this function.
- `hermes_cli/profiles.py` `create_profile` calls `session_listing.strip_inherited_sidebar_policy(config, name,
  root)` on the staging dir after the channel strip (0192).
  `stamp_automated(rows, home | automated_sources=)` sets the flag on each row.
- `hermes_cli/web_routers/profiles.py` stamps rows per profile store in `_read` and on the sidebar slices
  after `_tag_rows`. `web_routers/sessions.py` stamps `/api/sessions` rows.
- `tui_gateway/methods_projects.py`: `_project_tree_automated_sources()` is read once per store and
  `_project_tree_row(r, automated_sources)` emits `automated`. The contract `ProjectTreeSession.automated` is in
  `tui_gateway/contracts/projects_pets.py`, and `apps/shared/src/gateway-contract.*` are regenerated from it.
- Desktop store: `$sidebarShowAutomated` / `setSidebarShowAutomated` live in `store/layout.ts` and are counted
  in `$sidebarFiltersActive` only when true. `store/sidebar-automated.ts` holds the ONE predicate,
  `sessionHiddenAsAutomated(row, showAutomated, ctx)`, built on `sessionStatusStripe` from
  `store/session-dot-state.ts`.
- `app/chat/sidebar/index.tsx` uses that predicate in `sessionMatchesFilters` and the messaging sections, and
  `filtersNarrow` includes `!showAutomated`. `store/sidebar-visible-order.ts` (`sidebarSessionOrder`) drops the
  same ids by looking rows up in `$sessions` / `$messagingSessions`.
- `app/chat/sidebar/filter-menu.tsx`: checkbox next to "Show archived", i18n key `sidebar.filter.automated`
  (en, de, es, fr, ru, zh, ja, ar_chrome, zh-hant_chrome).

## Code

| File | What |
|---|---|
| `hermes_cli/session_listing.py` | `automated_session_sources`, `AutomatedSourcePolicy`, `is_automated_source`, `stamp_automated` |
| `hermes_cli/config_defaults.py` | `sessions.automated_sources: []`, `sessions.interactive_sources: []`, `sessions.sidebar_profiles: []` |
| `hermes_cli/profiles.py` | clone strips inherited sidebar source lists (0192) |
| `hermes_cli/web_routers/profiles.py`, `web_routers/sessions.py` | stamp REST rows |
| `tui_gateway/methods_projects.py`, `tui_gateway/contracts/projects_pets.py` | project-tree rows + contract |
| `apps/desktop/src/store/sidebar-automated.ts` | `sessionHiddenAsAutomated`, `automatedRowNeedsAttention` |
| `apps/desktop/src/store/layout.ts` | `$sidebarShowAutomated`, filters-active, reset |
| `apps/desktop/src/store/sidebar-visible-order.ts` | ⌘]/⌘[ skips hidden automated rows |
| `apps/desktop/src/app/chat/sidebar/index.tsx`, `filter-menu.tsx` | list filtering + checkbox |
| `apps/desktop/src/types/hermes.ts` | `automated?: boolean` on `SessionInfo` |

## Tests

- `tests/hermes_cli/test_session_listing.py::TestAutomatedSources`: a listed source → `true`, `"*"` → `true`,
  an unlisted source → `false`, key unset → `false`, and per-home scoping (two homes, only one flagged).
- `tests/hermes_cli/test_profiles_sidebar_scope.py::TestAutomatedSessions`: sidebar slices and the unified
  `profile=all` list mark only the flagged profile's rows, a listed-only source config, and the project tree per
  profile; `test_interactive_allowlist_flags_unlisted_sources_per_profile` (0190): with `interactive_sources:
  [desktop]` on one profile, its unlisted source is automated in the unified list and the project tree, the
  listed one is not, and the same source in the other (allowlist-free) profile stays unflagged.
- `tests/hermes_cli/test_session_listing.py::TestInteractiveSources` (0190): unlisted → `true`, listed → `false`
  (case/whitespace-insensitive), no source → `false`, empty list = old behaviour, `automated_sources` still wins,
  per-home scoping, policy truthy with only an allowlist, plain-list callers unchanged.
- `tests/hermes_cli/test_session_listing.py::TestSidebarProfiles` (0191): unlisted profile → every row
  automated; listed profiles (default + named, case-insensitive) → not; explicit `interactive_sources`,
  `automated_sources` and `automated_sources: []` in the profile win; unset or `[]` root list = old behaviour; read
  from the root only (a profile listing itself in its own config is still automated, and a profile-level list is
  ignored when the root sets none).
- `tests/hermes_cli/test_profiles_sidebar_scope.py::TestAutomatedSessions::test_root_sidebar_profiles_hides_unlisted_profile`
  (0191): through the REST app, `/api/profiles/sessions` flags the unlisted worker profile's cli row and not
  default's, and `/api/sessions?profile=worker` flags it too.
- `tests/hermes_cli/test_profile_clone_channels.py` (0192): `test_clone_drops_source_sidebar_lists_when_root_lists_sidebar_profiles`
  (clone loses the lists, keeps other `sessions.*` keys, its cli row is automated, source untouched) and
  `test_clone_keeps_sidebar_lists_when_root_sets_no_sidebar_profiles`.
- `tests/tui_gateway/contracts/test_generated.py`: the regenerated contract files (with
  `ProjectTreeSession.automated`) are current.
- `apps/desktop/src/store/sidebar-automated.test.ts`: hidden when off (never human or unstamped rows), shown
  when on, pinned rows kept (server flag, local pin, lineage-root pin), blocked / needs-attention rows kept,
  working or finished automated rows still hidden; `$sidebarShowAutomated` defaults off without lighting the
  filter dot, counts as a filter when on, and reset turns it off.
- `apps/desktop/src/store/sidebar-visible-order.test.ts` (3 cases): stepping skips hidden automated rows,
  steps onto them once the toggle is on, keeps an automated row with a red stripe in the order.
- Also green: `filter-menu.test.tsx`, `chat-sidebar.integration.test.tsx`, `layout-sidebar-view.test.ts`,
  `i18n/overlay-completeness.test.ts`.

Run: `scripts/run_tests.sh tests/hermes_cli/test_session_listing.py tests/hermes_cli/test_profiles_sidebar_scope.py
tests/hermes_cli/test_profile_clone_channels.py
tests/tui_gateway/contracts/test_generated.py` and, in `apps/desktop`, `npx vitest run src/store/sidebar-automated.test.ts
src/store/sidebar-visible-order.test.ts`.

## Applying

`git am core/patches/0166-*.patch` (after the earlier sidebar patches; it touches the same sidebar files and
i18n overlays as `sidebar-status-stripe.md`). Then set `sessions.automated_sources: ["*"]` on each bot or test
profile. Leave it unset on profiles you chat in. Or, with 0191 + 0192, set the root's
`sessions.sidebar_profiles: [default]` (plus any other profile you chat in) once and skip per-profile config.

## Gotchas

- Rows are only flagged by gateways running this patch. An older gateway's rows have no `automated` field and
  are never hidden. Setting the config key there does nothing until that host updates.
- The key is per profile store. `"*"` on your own chat profile would hide all of your own chats, so put it only
  on bot profiles (Slack followers, auxiliary LLM servers, kanban reviewers, probes).
- `sidebar_exclude_sources` is different: those rows are never sent to the sidebar at all, with no toggle.
  `automated_sources` keeps the rows in the payload and lets the Desktop hide or show them.
- A bot row that later turns red, grey or needs input reappears without touching the toggle. That is intended:
  a bot waiting on you must stay visible.
- `hermes config set` warns that the key is not recognized on older cores; pass `--force`.
- `interactive_sources` must list every source you chat from on that profile. A source you forgot (a new
  messaging platform) is hidden behind the toggle until you add it; open rows that need you still show.
- Agent scratch sessions for live proofs: create them with `session.create {"source": "scratch"}` (the RPC already
  takes `source`) so the allowlist hides them, and still delete them at teardown.
- `sidebar_profiles` is read from the root home only. Listing a profile there (or giving it its own
  `automated_sources`/`interactive_sources`) is the only way to make a non-default chat profile visible again;
  forgetting to list a profile you chat in hides it behind the toggle.
- A profile created before 0192 by `--clone` still carries default's lists, which win; delete them from its
  config.yaml (or set `automated_sources: ["*"]`) if it is a bot.
