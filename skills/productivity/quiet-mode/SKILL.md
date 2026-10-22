---
name: quiet-mode
description: "Use when user wants Hermes off screen/speakers (movie)."
version: 1.0.0
author: daveey (local)
metadata:
  hermes:
    tags: [quiet, movie, dnd, screen, speakers, focus]
---

# Quiet / movie mode

Plugin `~/.hermes/plugins/quiet-mode` gates recognized screen/speaker tool calls in processes that loaded it.
State: `~/.hermes/quiet-mode.json` (host-wide, re-read per call). An enabled flag is not proof of silence;
verify the offending workload separately when the user reports noise.

## Toggle ("movie mode", "I'm watching something", "stop using my screen")

- In chat: `/quiet` (toggle), `/quiet on 2h`, `/quiet off`, `/quiet status` — alias `/movie`.
- Shell (fast, no Hermes startup): `~/.hermes/bin/hermes-quiet on 2h|off|status`. Also `hermes quiet ...`.
- As the agent: run `~/.hermes/bin/hermes-quiet on <dur>` with the terminal tool (not itself blocked).
- Bare number = minutes; `2h`, `90m`, `1h30m` work. No duration = until turned off.

## What gets blocked

computer_use; desktop_ui tools (desktop_preview, drive_preview, gui_tour, show_tip, focus_pane, apply_layout, …);
text_to_speech; all browser_* (browser is a local visible Chrome) except browser_vault_list; chrome-devtools MCP;
spotify_*; ha_call_service for media_player/tts/remote/assist_satellite/notify/light/scene/siren; and
terminal/execute_code/process commands matching `open`, `osascript`, `say`, `afplay`, cliclick, peekaboo,
`shortcuts run`, `hermes desktop`, spotify/openhue CLIs, etc. (regex list `_SHELL_PATTERNS` in the plugin).

## If a tool result says "QUIET MODE — blocked"

Don't retry or work around it with another tool/script. Keep doing silent work; if the step is required, end
the turn with a note of what is pending. Never turn quiet mode off yourself unless the user asks.

## Ending quiet mode ("done with the movie", "tell sessions to resume")

1. `~/.hermes/bin/hermes-quiet off` (then `status` to confirm).
2. Find paused work: workers that hit the gate end up as `blocked` Kanban cards whose reason mentions quiet mode.
   Scheduled cards and live chat sessions pick up again without help. The live board is `~/.hermes/kanban.db`;
   `~/.hermes/kanban/kanban.db` and `kanban/boards/*/kanban.db` can be empty or stale, so querying those misses
   the cards. Search in one query:
   `sqlite3 ~/.hermes/kanban.db "select id,status,title from tasks where status='blocked' and id in (select task_id from task_events where kind='blocked' and lower(payload) like '%quiet%')"`
3. For each match, run `kanban_unblock` and `kanban_comment` ("quiet mode is off, resume screen/simulator steps").
   Leave cards blocked on other human steps (such as daytime device tests) alone, and list them in the reply.

## Pitfalls

- A newly enabled/edited plugin loads only in NEW Hermes processes; the running Desktop backend needs an app
  restart. The state file, by contrast, is live.
- Treat movie mode as a request for uninterrupted playback, not merely an enabled flag. Keep host volume unchanged
  when silencing another workload. A tool-call gate does not suspend already-running processes; Desktop's own
  sounds and non-Hermes processes also need separate checks.
- For an authorized simulator shutdown: `xcrun simctl list devices booted`, then `xcrun simctl shutdown <UDID>`
  for the identified device, then repeat the list to verify. This stops the device, not its owning agent session
  or future launches; report those separately. Do not describe shutdown as a persistent speaker-access block.
- Extend coverage by editing `BLOCK_TOOLS` / `HA_BLOCK_DOMAINS` / `_SHELL_PATTERNS`, then run
  `~/.hermes/hermes-agent/venv/bin/python -m pytest -q ~/.hermes/plugins/quiet-mode/tests_quiet.py`.
