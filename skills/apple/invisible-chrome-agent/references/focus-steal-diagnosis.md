# Finding what steals focus on macOS (any app, incl. the agent Chrome)

Goal: name the requesting process from raw logs, not guess. Order:

1. **History (already on disk).** Only default-level lines persist, and info/debug do not, so `log show --debug` for
   the requester returns nothing. WindowServer's focus line does persist:
   `log show --last 3h --style compact --predicate 'process == "WindowServer" AND eventMessage CONTAINS "Deferring
   events from frontmost process"'`. Parse `PSN … (<App>) -> <pid: N>`, collapse repeats, and count by pid. Map
   each pid with `ps -o ppid,lstart,command`. Two processes can share one app name: the agent Chrome has
   `--user-data-dir …/chrome-agent`, and its parent is the supervisor. A steal shows as user app → X → user app
   within 0.1–2 s, over and over.
2. **Live logger (run before changing anything).** Run three streams side by side:
   - NSWorkspace `didActivateApplicationNotification`: a small Swift binary that logs timestamp, app, pid, ppid,
     parent command and previous app as JSONL.
   - Requester attribution: `log stream --level debug` with a SUBSYSTEM-scoped predicate:
     `(subsystem == "com.apple.launchservices" AND category == "cas" AND (composedMessage CONTAINS
     "_LSRequestProcessBecomeFrontmost" OR composedMessage CONTAINS "SetFrontApplication(")) OR (subsystem ==
     "com.apple.SkyLight" AND category == "processes" AND composedMessage CONTAINS "SLPSetFrontProcess")`.
     A broad `eventMessage CONTAINS` predicate at debug level drops messages ("Messages dropped during live
     streaming").
   - A 50 ms `ps -axo pid,ppid,command` poll that records every new `osascript` process with its ancestor chain.
     JXA `activateWithOptions` runs for under 0.5 s, so it's gone before anyone can look it up afterwards.
3. **Read the requester.** The process name on the `_LSRequestProcessBecomeFrontmost` / `SLPSetFrontProcess` line
   just before the activation tells you who asked:
   - `osascript[pid]` with `LSBringForwardRequestForced`: a script raised it. Take the caller from the ps ancestry.
   - `Dock`: the user pressed Cmd-Tab or clicked the Dock, so it's not a steal.
   - `osascript` whose ancestor is `hermes-chrome-agent run`: the focus guard handing focus back. That's the end of
     a steal, not its cause; the activation just before it is the steal.
   - `osascript` whose ancestor is a Hermes worker (`hermes_cli.main … chat -q work kanban task`) or the Desktop
     `serve` backend, with no matching tool call: a plugin hook raised it (browser-focus `transform_tool_result` /
     `transform_llm_output`). Confirm in `state.db` by finding the hook's appended note (`[browser-focus]`, "brought
     the Chrome window to the front") in that session's tool result or reply at that second.
   - Chrome front right after the user's app quit (e.g. Hermes restarting for an update): macOS picked the next
     app. Not a steal.
   - No requester line, with the app itself becoming front: the app raised itself. For Chrome that's a CDP call.
   - `Screen Sharing` frontmost while the user reports a "lost cursor" or can't Cmd-Tab back: a control-mode VNC
     window took key focus. It swaps the local cursor for `invisibleCursor`, and `captureSpecialKeys` sends
     Cmd-Tab to the remote Mac; if the remote cursor is hidden as well, no cursor shows anywhere. To confirm, run
     `log show --start … --predicate 'process == "Screen Sharing"' | grep -F '(ScreenSharing)'`. Look for
     `sessionState`, `set control mode`, `windowDidBecomeKey`, `invisible cursor`, `cursor is hidden`. Agents
     usually opened it via `open … vnc://`.
4. **Match bursts to callers.** Look up agent tool calls at the burst timestamps:
   `select session_id, timestamp, tool_calls from messages where timestamp between …` in `~/.hermes/state.db`,
   searching for `createTarget|activateTarget|json/new|json/activate|unittest|osascript|vnc://|open -a`.
   `timestamp` is epoch seconds. Convert local times with `strftime('%s','2026-01-02 21:36:00','utc')`: the `'utc'`
   modifier tells SQLite the input is local time. Without it the input is read as UTC, the window shifts by the
   UTC offset, and you get the wrong (often previous-day) calls. Print rows with
   `datetime(timestamp,'unixepoch','localtime')` so you can check the window. Test-suite runs and
   cron jobs show up here, and their start-to-finish times line up with the bursts.
5. **Reproduce each suspect** with Finder in front (`osascript -e 'tell application "Finder" to activate'`), run
   the one call, and read the logger. Then fix every caller at its source (code, skill text, test fixture).
6. **Prove it.** Run the same logger for at least 30 minutes after the fix. Count agent-app activations whose
   requester is not Dock and not an asked-for show. A guard that hands focus back still counts as a steal.
   Fixes to Hermes core or to a plugin only take effect in processes started after them (Kanban workers pick them
   up at once; the Desktop backend and gateway need a restart), so check that
   (`git merge-base --is-ancestor <fix> HEAD` plus the serve process's start time) before the proof run.
