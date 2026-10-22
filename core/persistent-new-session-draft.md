# Persistent new-session draft (⌘N returns to it)

## Behavior

The new-session screen behaves like a place you navigate to, not a fresh form each time. Type into a
new session (text, pasted images, file chips), go to another session, press ⌘N, and the
new-session screen comes back with exactly that draft. The draft is reset only after you send it
(it moves onto the session it creates) or empty it.

Upstream (#66662) rotates the fresh-draft key on every New Chat, so a second ⌘N always opens an empty
composer and leaves the earlier unsent text in an unreachable bucket.

## How it works

- `store/composer.ts` adds `freshDraftHasContent()` (the current `__new__:<uuid>` bucket has text or
  attachments) and `rotateFreshDraftKeyUnlessPending()` (keep the key while it has content,
  otherwise `rotateFreshDraftKey()`).
- `startFreshSessionDraft` (`app/session/hooks/use-session-actions/index.ts`) calls the new helper
  instead of `rotateFreshDraftKey()`. If you are already on the new-session screen it first sends
  `requestComposerDraftSync('flush')`, so keystrokes still inside the persist debounce count as
  content and are not rotated into an orphaned key.
- Leaving the new-session screen already stashes the live editor text and attachments under the fresh
  key (the composer's scope-swap cleanup). Returning restores them from that bucket.

## Dead-session prune (0170)

The Desktop's dead-session prune probes each stored draft scope as a session id and clears it on a 404. It excluded
only the legacy `__new__` bucket, so the per-lifecycle `__new__:<uuid>` key (where the unsent new-chat text sits after
you switch away) was probed, 404'd, and the draft was deleted a few seconds later; ⌘N then rotated to a blank
composer. `stashedDraftScopes()` (`store/composer.ts`) now excludes every fresh-draft scope (`isFreshDraftScope`).

## Code

Patch 0036: `apps/desktop/src/store/composer.ts`, `apps/desktop/src/app/session/hooks/use-session-actions/index.ts`.
Patch 0170: `apps/desktop/src/store/composer.ts` (`stashedDraftScopes`).

## Tests

`use-session-actions.test.tsx`: "returns to the pending new-session draft (text + attachments) instead
of rotating it away". It fails without the patch. The existing #66662 isolation tests still pass,
because they rotate keys that are empty.

0170: `store/dead-session-prune.test.ts` "never discards a per-lifecycle new-chat draft (__new__:<uuid>)" (also asserts
`getSession` is not called with the fresh key, and that a real dead session's draft is still pruned).

## Applying

Applies on its own: `git am -3 core/patches/0036-*.patch` (0170 after it), then rebuild the desktop app.

## Gotchas

- Text persists across app restarts (localStorage), but attachments are kept in memory only, as in
  upstream. After a restart the text comes back without the images.
- If a session you were typing into turns out to be deleted, upstream moves that text into an empty
  new-session draft with an "Restored your unsent message" notice. While a pending new-session draft
  exists, that restore does nothing, so the pending draft is never overwritten.
- Desktop-only change; no plugin surface controls the fresh-draft key, so it has to be a core patch.
