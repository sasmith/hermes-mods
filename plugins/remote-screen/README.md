# remote-screen

See and drive another Mac's screen over macOS **Screen Sharing (VNC)**, with no window opening on the Mac
Hermes runs on. Built for one job above all: **approving permission prompts** ("“X” would like to access …"
→ Allow) that block unattended work on a headless or remote Mac.

| Tool | What it does |
|---|---|
| `remote_screen_hosts` | Configured hosts: Screen Sharing reachable?, login stored?, the one-time setup command, and a `::screenshare` button directive for the user. |
| `remote_screenshot` | Grabs the remote screen. The image goes straight to the model; the full-res PNG is saved. `ocr: true` adds on-screen text with boxes. |
| `remote_input` | `click` / `double_click` / `right_click` / `move` / `scroll` / `type` / `key` (`"cmd+space return"`). Target by coordinates in the last screenshot's image, or by on-screen text (`target_text`, OCR). Returns a fresh screenshot. |
| `remote_approve_dialog` | Screenshot → OCR → find a permission prompt whose **requester** is on the allowlist → click its approve button → re-check the prompt is gone. Refuses password/Touch ID prompts and anything not allowlisted, returning a `::screenshare` button for the user instead. `dry_run: true` only reports. Before/after screenshots + a JSON log per attempt in `<hermes home>/cache/remote-screen/<host>/approvals/`. |

## Why VNC

Synthetic local clicks (CGEvent, Accessibility, `cliclick`, cua-driver) are ignored by many macOS security
prompts. Clicks sent through Screen Sharing arrive via `screensharingd` as a remote user's real input, which
is how a human approves prompts from another Mac. Verified on macOS 26: both an Automation prompt (“X” wants
access to control “Finder”) and a Files & Folders prompt (“X” would like to access files in your Desktop
folder) were approved this way, and TCC recorded them as allowed. Apple says some approvals refuse remote
input (kernel/system extensions); the tool reports `click_ignored` when the prompt is still there afterwards.

## Setup

1. On each target Mac: **System Settings → General → Sharing → Screen Sharing** on, your account allowed.
2. On the controlling Mac, write `<hermes home>/remote-screen.yaml`:

   ```yaml
   hosts:
     studio:
       label: "Studio Mac"
       address: studio.local        # what this Mac can reach; host:port works too
       aliases: [studio]
       user: me                     # macOS account on the target
       # password_env: STUDIO_PW    # optional: read the password from an env var instead of Keychain
       # screenshare_address: ...   # optional: address for the user's ::screenshare button
   approve:
     apps: [Hermes Python, ghostty]        # requesters that may be auto-approved; empty = approve nothing
     buttons: [Allow, OK, Always Allow, Allow Full Access, Allow While Using App]
   ```

3. Store the login in the controlling Mac's Keychain (you type it; the agent never sees it).
   `remote_screen_hosts` prints this exact command per host; the agent runs it with `run_in_user_terminal`:

   ```sh
   security add-generic-password -U -s hermes-remote-screen -a 'me@studio.local' -l 'Hermes remote-screen: studio.local' -w
   ```

Requirements: controller on macOS (Vision OCR via a small Swift helper, compiled on first use with `swiftc`
into `<hermes home>/cache/remote-screen/`), `cryptography` and `Pillow` in the Hermes venv (both ship with
Hermes). Target: any Mac with Screen Sharing (Apple auth types 30 and 33; classic VNC password as a fallback).

## Safety

- The allowlist matches only the **requester name** in the prompt (the quoted app name on the request line,
  or the line above when it wraps), never other text on screen, so an allowlisted name in a background
  window can't approve someone else's prompt.
- Password, Touch ID and admin prompts are never handled; the agent must not type passwords via `remote_input`.
- Quiet mode (movie mode): input aimed at **this** Mac's own screen is refused while it is on. Remote Macs
  are unaffected, since they don't touch the screen you're watching.
- The target Mac shows macOS's "Your screen is being controlled" indicator while a tool call is connected,
  and anyone at that Mac sees the pointer move.

## Local Screen Sharing guard

`screen_guard.py` (hooks `pre_tool_call` + `pre_llm_call`, registered even when the tools are not) refuses terminal /
execute_code (also inside `tool_call`) commands that open Screen Sharing on this Mac: `open ... vnc://`,
`open -a "Screen Sharing"`, `open -b com.apple.ScreenSharing`, `osascript ... Screen Sharing`. A control-mode window
steals key focus, hides the cursor and captures Cmd-Tab. The error points to `remote_screenshot` / `remote_input` /
`remote_approve_dialog` or a `::screenshare{host=...}` button. Allowed only when the current user turn of an
interactive chat explicitly asks ("open Screen Sharing to ..."); Kanban workers, cron and delegated subagents are
always refused. Off switch: `HERMES_SCREEN_SHARE_GUARD=0`. Tests: `tests_screen_guard.py`.

## Tests

```sh
cd plugins/remote-screen
uv run --with pytest --with cryptography --with pillow python -m pytest -q -p no:cacheprovider tests_remote_screen.py tests_screen_guard.py
```

An in-process fake Apple VNC server covers type-30 login (DH + AES credential blocks), bad-password errors,
raw + zlib framebuffer decoding, pointer/key events and key combos. The approval logic is tested on OCR
fixtures: allowlisted approve, non-allowlisted refusal, background text not counting as the requester,
wrapped app names, a prompt wrapped mid-phrase (real macOS 26 OCR), password prompts, and no prompt at all.
