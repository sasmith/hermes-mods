---
name: invisible-chrome-agent
description: "Use when driving the agent Chrome or a Mac app steals focus. Hidden Chrome on a virtual display, a CDP supervisor, and front-window fixes."
version: 1.0.0
author: Claude Code
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [chrome, cdp, browser, virtual-display, passkeys, webauthn, launchd, macos]
    category: apple
    related_skills: [local-chrome-remote-debugging]
---

# Invisible Chrome agent (macOS)

A real Google Chrome that Hermes drives over CDP, rendered on a private-API virtual display
("Hermes Agent Display") so it never covers the user's screen. Real Chrome, not Chromium, matters:
Google Chrome carries Apple's `com.apple.developer.web-browser.public-key-credential` entitlement,
so iCloud Keychain passkeys work in it. A human still has to tap Touch ID for every passkey operation.

## Pieces (all in `scripts/`)

| File | What |
|---|---|
| `vdisplay.swift` + `CGVirtualDisplay.h` | `hermes-vdisplay`: creates a 1440x900 `CGVirtualDisplay` (override with `HERMES_VDISPLAY_SIZE=WxH`) that lives while the process runs. Parks it at the bottom-right corner of the desktop arrangement (corner contact only, so the cursor rarely wanders in), re-parks it if a monitor change makes it share an edge, and prints `display <id> bounds x,y WxH main x,y WxH` at start and on every change. |
| `hermes-chrome-agent` | Stdlib-only Python supervisor + CLI (runs under `/usr/bin/python3`, ships its own minimal CDP websocket client). |
| `window-evict.swift` | `hermes-window-evict X Y W H [--skip-pid N]...`: moves every layer-0 window of a non-Chrome app that sits on the virtual display (bounds X Y W H) to the main display's visible frame via Accessibility (match by frame, set position, shrink if larger), printing `evict <bundle> <title> from x,y to x,y`. `untrusted` (exit 0) when the calling process lacks Accessibility; `--check` prints `trusted`/`untrusted`. Skips `Hermes Touch ID.app`. |
| `notifier.swift` | `Hermes Touch ID.app` (bundle `com.hermes.chrome-agent.notify`, LSUIElement, Developer-ID signed when the keychain has one), built by `install`: posts the passkey notification with UNUserNotificationCenter and reports clicks back to the supervisor over stdout. |
| `test_hermes_chrome_agent.py` | Unit tests: click-through path, evict scheduling, focus guard, focus-filter rules and the proxy end to end against a fake Chrome (`/usr/bin/python3 -m unittest -v test_hermes_chrome_agent` in `scripts/`). |

`hermes-chrome-agent run` (what launchd runs):
- starts `hermes-vdisplay`, then Chrome with `--user-data-dir=$HERMES_HOME/chrome-agent --remote-debugging-port=9334
  --remote-debugging-address=127.0.0.1 --remote-allow-origins=<127.0.0.1/localhost on 9333 and 9334>`, the three
  anti-backgrounding flags, and Memory Saver / Energy Saver forced off in `Local State` (hidden agent tabs otherwise
  get frozen or discarded);
- serves **127.0.0.1:9333 as a CDP focus-filter proxy** in front of Chrome's internal port 9334 (HTTP `/json/*` and
  every browser/page websocket, transparent: `/json/version` and `/json/list` URLs point back at 9333, so no client
  changes). Unless the show flag (`$HERMES_HOME/cache/chrome-agent/show`, set by `show` / browser_focus, honouring
  `--minutes` expiry) exists, it rewrites every call that makes macOS raise Chrome: `Target.createTarget` gets
  `background: true` (and loses `focus: true`); `Target.activateTarget` and `Page.bringToFront` are answered `{}`
  without reaching Chrome; `GET /json/activate/<id>` is answered `200 Target activated`; `PUT /json/new?<url>` becomes
  a background `Target.createTarget` and returns the new target like Chrome would. Each rewrite logs
  `focus-filter: <method> from pid N (<command>) rewritten` (peer found with lsof). The proxy drops
  `permessage-deflate` offers so it can read every message. Port overrides: `HERMES_CHROME_AGENT_PORT` (public),
  `HERMES_CHROME_AGENT_CHROME_PORT` (internal, default PORT+1); `HERMES_CHROME_AGENT_FOCUS_FILTER=0` puts Chrome
  straight on 9333 again (no proxy). The supervisor's own CDP connection (re-homing, passkey reveal) uses 9334;
- every second: `Target.getTargets` → `Browser.getWindowForTarget` for every page, and `Browser.setWindowBounds` for
  any window not inside the virtual display (new windows, popups, OAuth windows land there within ~1s).
  `--window-position` alone is ignored by macOS, so this loop is what actually keeps windows hidden;
- every ~2 s runs `hermes-window-evict` so other apps' windows that land on the virtual display (macOS treats it as a
  monitor) are moved back to the main display; each move is logged as `evict <bundle> <title> from … to …`. This needs
  Accessibility for the supervisor's interpreter (TCC charges the helper to the LaunchAgent's program): install with
  `HERMES_CHROME_AGENT_PYTHON=<an interpreter that holds Accessibility>` (default `/usr/bin/python3`, which usually
  holds none). Without the grant the log says `evict: untrusted` once per run and `status` prints it too; Chrome
  re-homing is unaffected;
- restarts only the child that died: Chrome crash → relaunch (with backoff; its tabs are lost); vdisplay crash →
  new display, windows re-homed. A stale Chrome left on the profile (SingletonLock pid) is stopped first, since a
  second launch on the same profile would just hand off to it;
- runs a **focus guard** as backstop behind the filter (thread, every 0.25 s, `lsappinfo front`): when the agent Chrome becomes frontmost without
  being asked, it hands focus back to the app the user had in front and logs `focus-guard: agent Chrome took focus
  ... returned it to pid N (App)`. A CDP client causes this with a foreground `Target.createTarget` (no
  `background: true`), `Target.activateTarget` or `Page.bringToFront`: macOS gives Chrome keyboard focus even while
  its windows are on the virtual display, so the user's typing lands in an invisible window. Allowed grabs: `show`
  ran in the last 10 s (browser_focus, status-bar toggle; judged by the show flag's mtime), a passkey reveal, or a
  user click / modifier press (Dock, Cmd-Tab) within 1 s. A grant lasts until Chrome loses focus. Off with
  `HERMES_CHROME_AGENT_FOCUS_GUARD=0`;
- auto-attaches to every page target and injects a wrapper around `navigator.credentials.get/create`
  (`Page.addScriptToEvaluateOnNewDocument` + `Runtime.addBinding`). A non-conditional WebAuthn request posts a macOS
  notification "Hermes needs Touch ID — Tap Touch ID to sign in with a passkey for <rpId>" from
  `Hermes Touch ID.app`. Clicking the notification or its **Show** button moves the window holding the asking tab
  onto the main display, `Target.activateTarget`s that tab, and brings Chrome forward (the notifier, frontmost after
  the click, hands activation to Chrome's pid; JXA as backup). Re-homing skips that window until the request
  succeeds (+3s grace) or 180s pass. A failed request (a hidden tab's `get` usually fails at once with
  NotAllowedError; sites retry) keeps the notification and the revealed window. One live notification per tab.
  There is deliberately no osascript fallback: `display notification` belongs to Script Editor, and clicking it
  opens Script Editor and shows nothing (the original bug). Notifications not allowed for Hermes Touch ID → the
  asking window is revealed on the main display at once (no activation) and the app shows a floating alert
  (Show / Dismiss / Notification Settings…) whose Show works like the notification's. App missing → window
  revealed and Chrome raised at once. Supervisor start and `status` print the permission and, when it is off,
  the deep link `x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=com.hermes.chrome-agent.notify`.
  Conditional mediation (passkey autofill on page load) is ignored. This needs `Page.enable` +
  `Runtime.enable` on each page session: without them the script and binding vanish on the first cross-site
  navigation.
- `HERMES_CHROME_AGENT_SHOW_ON_PASSKEY=1` additionally moves the requesting window to the main display until the
  request settles (max 120s), so the human sees which site they approve. Off by default: the macOS passkey sheet
  (a 448-px sheet on the Chrome window, on the virtual display) accepts a Touch ID tap without being seen.

## Install / operate

```bash
S=~/code/hermes-mods/skills/apple/invisible-chrome-agent/scripts/hermes-chrome-agent
HERMES_CHROME_AGENT_LABEL=com.example.hermes-chrome-agent $S install   # builds hermes-vdisplay into
#   $HERMES_HOME/chrome-agent-tools/, symlinks the supervisor there, writes + bootstraps the LaunchAgent
#   (RunAtLoad, KeepAlive, log $HERMES_HOME/logs/chrome-agent.log). Default label com.hermes.chrome-agent.
hermes config set browser.cdp_url http://127.0.0.1:9333     # every Hermes session now attaches here

hermes-chrome-agent status     # supervisor, displays, notifier permission, every Chrome window + URL
hermes-chrome-agent show       # windows → main display, re-homing paused (look at / click the page)
hermes-chrome-agent show --minutes 20   # same, auto-hides after 20 min (supervisor restart needed to pick up)
hermes-chrome-agent hide       # resume: windows go back within ~1s
launchctl kickstart -k gui/$UID/<label>                    # restart (graceful: SIGTERM → Chrome and display stop)
tail -f ~/.hermes/logs/chrome-agent.log
```

Sign the agent into sites once: `hermes-chrome-agent show`, log in by hand in that window, `hide`.

## How Hermes uses it

`browser.cdp_url` is the operator override in `browser_exec` routing (`tools/browser_use_cli.py`
`_resolve_backend_cdp`): it wins over cloud providers and the managed Chromium, and it disables the
real-profile route (`_resolve_real_profile_cdp` returns early when an override is set). Consequences:
- Every surface that reads this profile's config (CLI one-shots, Desktop chats, Kanban workers, cron jobs,
  gateway) drives this Chrome. The value is read per call, so running backends pick it up without a restart.
- `browser_exec(local=true)` no longer launches a snapshot copy of the user's default Chrome profile; it attaches to
  this Chrome too. The agent profile has only the logins made in it, not the user's everyday cookies.
- `browser.use_real_profile` can stay on; it is simply shadowed. Unset `browser.cdp_url` to go back.
- Sessions are tabs in this one browser. Each `browser_exec` session gets its own tab; tabs persist until closed.

## Verify

- `curl -s 127.0.0.1:9333/json/version` answers; `hermes-chrome-agent status` lists windows at the virtual display's
  origin.
- Window placement without Screen Recording permission: `CGWindowListCopyWindowInfo` bounds per owner pid (a tiny
  Swift tool) show Chrome only on the virtual display. `screencapture -D 2` needs Screen Recording for the calling
  process and shows only wallpaper while the screen is locked (`lsappinfo front` = loginwindow).
- New window: `Target.createTarget {newWindow: true, left: 100, top: 100}` → re-homed in < 0.5s (log line `rehome:`).
- Kill tests: `kill -9 <chrome pid>` → CDP back within ~1-2s; `kill -9 <vdisplay pid>` → new display id, Chrome
  windows on it.
- Passkey: on webauthn.io, Authenticate → log `webauthn get request from https://webauthn.io`; the unified log shows
  NotificationCenter receiving it (`log show --predicate 'process == "NotificationCenter"'`; "muted by display state"
  just means the screen was locked).

## Pitfalls

- **Never raise the agent Chrome unless the user asked to see it.** `Target.activateTarget`, `activate_tab`,
  `switch_tab(activate=True)`, `Page.bringToFront`, `/json/activate`, `PUT /json/new` and any `Target.createTarget`
  without `background: true` make macOS make Chrome frontmost within ~0.1 s (background create, even `newWindow`,
  does not). Through 9333 the focus filter now neutralises these (log `focus-filter: ... rewritten`, naming the
  caller); a caller that shows up there still needs fixing, and anything talking to 9334 directly bypasses the
  filter (only the supervisor and browser_focus, which reads Chrome's port from `ps`, should). Only the "Showing the user a
  page" flow below may activate. Test suites must launch their own headless Chrome, never use port 9333: a suite
  run against it raised Chrome once per test.
- **"Something keeps selecting Chrome" / "I lost my cursor or window."** Find the caller, don't guess: `grep -E 'focus-(guard|filter)'
  ~/.hermes/logs/chrome-agent.log` (a `focus-guard` hand-back with no `focus-filter` line just before it means the
  raise did not come through 9333: a direct 9334 client, the show flag, or a non-CDP activation), then follow `references/focus-steal-diagnosis.md` (persisted WindowServer
  history, live requester attribution, osascript ancestry, matching steal bursts to agent tool calls in `state.db`).

- **Other apps' windows get stranded on the virtual display, mostly after the lid closes.** In clamshell sleep the
  built-in screen goes away, the virtual display becomes the only screen (log: `bounds 0,0 1440x900 main 0,0
  1440x900`) and macOS moves every window onto it; on wake the display is re-parked in its corner and the windows
  stay there. Apps that autosave frames (SwiftUI `Window`) then reopen there after a relaunch, so force-quit/relaunch
  never helps. Symptom: the user "can't switch to" an app that is frontmost (`lsappinfo front`) and idle (`sample`
  shows the main thread in the event loop). Diagnose with CGWindowList bounds for the app's pid inside the "Hermes
  Agent Display" rect (`system_profiler SPDisplaysDataType`), and check the log for `evict` lines. The supervisor's
  evict loop fixes it within ~2 s when Accessibility is granted; it must no-op while the virtual display is the only
  screen, or it re-centers every window each pass. One-off manual fix: compile `scripts/move-app-windows.swift` and
  run it once from a no-agent gateway cron (`hermes cron create 1m --script <x>.sh --no-agent --repeat 1 --deliver
  local`), since only the gateway holds Accessibility; check `trusted true` and re-read the bounds. Dead ends:
  killing `hermes-vdisplay` (recreated at the same spot within ~1 s) and rewriting the app's `NSWindow Frame` default.
- **`status`'s eviction trust line reflects the calling shell, not the running supervisor.** From an untrusted
  terminal it prints `untrusted` while the LaunchAgent is evicting fine; judge the grant by `evict` / `evict:
  untrusted` lines in `chrome-agent.log`.

- **Chrome's tab-modal dialogs don't follow a window to another display.** "Use a saved passkey for <site>" is
  a child window; after `Browser.setWindowBounds` moves its window to the main display it stays clamped to the
  virtual one, so the revealed page shows dimmed with no dialog. `place()` therefore nudges the width (-1, then
  back), which re-anchors the dialog on the window. Check with CGWindowList: the 448x387 Chrome window must sit
  inside the revealed window's bounds.

- **Notification permission is asked once per bundle id, and a withdrawn prompt counts as denied.** If the
  helper quits while macOS's "Hermes Touch ID Notifications" prompt is up, the app ends up denied (status
  `denied`) and only System Settings > Notifications > Hermes Touch ID can fix it. The helper therefore stays alive
  until the prompt is answered, and its timeout starts only once the notification is posted. Notification
  settings read `denied` while that first prompt is still pending, so status can't tell "pending" apart.
- **Hidden tabs can stall.** Background tabs in a headed Chrome get throttled; even with the flags and savers off,
  CDP calls into a non-selected tab were seen to hang for ~20-60s at a time (also in a fresh Chrome without this
  supervisor), then recover. If a session keeps timing out on `Runtime.evaluate`/`Page.navigate`, a
  `Target.activateTarget` sent through the harness may not help: the harness stays attached to its own (stalled)
  tab. What works: `curl 127.0.0.1:9333/json/close/<id>` the stalled tabs, then open the page in its own background
  window, `cdp('Target.createTarget', url=<url>, newWindow=True, background=True)` (a tab alone in its window is
  `visible`), then `switch_tab("<new id>")` before any `js()`. Add `cdp('Emulation.setFocusEmulationEnabled',
  enabled=True)` if the page needs `document.hasFocus()`.
- **Security:** anything running as the user can drive port 9333 (and 9334): read and act as every account logged in to the agent
  profile, and trigger passkey requests (which still need a Touch ID tap). The port binds to loopback only; nothing
  stops other local processes.
- Bluetooth (phone/hybrid passkeys, security keys over BLE) is unavailable: Chrome logs "responsible app ... does not
  have Bluetooth metadata"; it is launched by launchd, not Finder. iCloud Keychain passkeys work.
- This Chrome shows a second Chrome icon in the Dock / app switcher; activating it does not move its windows.
  Raising the app or `setWindowBounds` onto the main screen just flashes the window: the 1s re-home loop pulls it
  straight back. Show pages the way the next section describes.
- `show --minutes N` expiry is enforced by the supervisor's re-home loop, so it needs a supervisor restart to take
  effect, and a restart closes every tab. Never restart it while a tab holds a login or an in-flight task the user
  still needs.

## Showing the user a page (sign-in, 2FA, click)
The user wants the window brought to them, never "find the Chrome window" instructions, and sent back once done.
1. Call `browser_focus` (browser-focus plugin; `match` = URL/title substring). It runs `show --minutes 20`, activates
   the tab over CDP and raises the app. Without the plugin: `hermes-chrome-agent show` FIRST (the focus filter
   swallows activation until the show flag exists), then
   `curl 127.0.0.1:9333/json/activate/<id>` and JXA `NSRunningApplication...activateWithOptions(3)` on Chrome's pid.
2. If the harness keeps timing out (`Runtime.evaluate`/`Page.navigate` on background tabs), skip it: open the page
   with `curl -X PUT 'http://127.0.0.1:9333/json/new?<url>'` and activate that id. Let the user click the first
   button themselves.
3. Tell them it's in front and what to click. Detect completion by polling `/json/list` for the tab leaving its login
   URL (the plugin's watcher hides automatically); otherwise `browser_focus {hide:true}` / `hermes-chrome-agent hide`.
   The Desktop status bar has a Browser shown/hidden toggle (desktop-plugins/browser-focus) for manual control.
4. Verify placement from `CGWindowListCopyWindowInfo` bounds for Chrome's pid (on the main display, not the virtual
   one) and the frontmost pid, not from the activate call's return value.
- Chrome restarts lose open tabs (no session restore, by design).
- Don't run a second Chrome on `$HERMES_HOME/chrome-agent`; the supervisor stops any stale one on startup.
- `CGDisplayCreateImage` is unavailable in the macOS 15+ SDK; use ScreenCaptureKit or `screencapture`.
