# Sidebar row shows the background processes holding it off green, with Stop

Patches:

- `patches/0184-feat-desktop-sidebar-row-shows-the-background-proces.patch`

What you see: a finished chat kept un-green by a long background process (for example a 72 h `gh pr checks --watch`)
now shows a process glyph beside the Kanban badge (with a count when more than one). Hover lists each command with its
age and a Stop button; clicking the row opens the session.

## Behavior

- Session-list endpoints stamp `background_processes` (id, redacted command truncated to 160 chars, `started_at`,
  `pid`; oldest first, at most 8) next to `background_active`.
- Stop posts `/api/sessions/{id}/background-processes/{proc}/stop`. A process this backend supervises is killed via
  `process_registry.kill_process(source="sidebar.stop")` (the `process_manage kill` path); one owned by another Hermes
  process is terminated by pid after re-checking its start time. Ownership is checked against the row's lineage ids
  (`not_found` otherwise). The stamp cache is dropped, so the row goes green at once.
- The glyph appears only while `background_active` > 0, matching the existing stripe rule
  (see [sidebar-status-stripe.md](sidebar-status-stripe.md)).

## How it works

`hermes_cli/process_session_activity.py` keeps a per-collect `_details` map (registry sessions plus every profile's
`processes.json` checkpoint) and `annotate_background_activity` attaches the list; `stop_session_process` does the
kill. `hermes_cli/web_routers/sessions.py` exposes the stop route; `web_models.py` carries the field. The desktop
renders `BackgroundProcessBadge` in `session-row.tsx`, with `store/session-background-stop.ts` for the POST and
`session-dot-state.ts` / `lib/session-signatures.ts` so the row repaints when the list changes.

## Code

- `hermes_cli/process_session_activity.py`, `hermes_cli/web_models.py`, `hermes_cli/web_routers/sessions.py`.
- `apps/desktop/src/app/chat/sidebar/background-process-badge.tsx`, `session-row.tsx`, `api/sessions.ts`,
  `store/session-background-stop.ts`, `store/session-dot-state.ts`, `types/hermes.ts`, `i18n/en.ts`, `types.ts`,
  `overlay-gaps.json` (other locales listed there as gaps).

## Tests

- `tests/hermes_cli/test_process_session_activity.py` (details, caps, stop paths).
- `apps/desktop/src/app/chat/sidebar/session-row.test.tsx`, `store/session-dot-state.test.ts`.
Not re-run when this doc was written (untested here).

## Applying

`git am -3`. Touches i18n files shared with other patches; resolve by keeping both sets of keys, then
`npm run i18n:keys`.

## Gotchas

- Needs a Desktop update to appear (renderer change).
- Non-English locales fall back to English until translated.

## Plugin?

No. Core sidebar and session endpoints.
