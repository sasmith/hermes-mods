---
name: messaging-bridges
description: "Use when linking Slack/WhatsApp/Signal or posting as user."
version: 0.2.0
platforms: [macos]
---

# Messaging Bridges (Slack, WhatsApp, Signal)

Wire local CLIs/bridges to third-party chat networks using **persistent-session** auth so there is no repeat login. Each network has one durable pattern:

| Network | Mechanism | State lives in |
|---|---|---|
| WhatsApp | whatsmeow Multi-Device: scan QR from phone → session file | `whatsapp-bridge/store/whatsapp.db` |
| Signal | `signal-cli link` as secondary device: scan QR from Signal app | `~/.local/share/signal-cli/` |
| iMessage/SMS | `imsg` reads Messages.app directly | macOS Messages signed-in state |
| Slack | Hermes gateway Slack adapter, Socket Mode app (bot `xoxb-` + app `xapp-` tokens) | `~/.hermes/.env` |

Slack setup procedure: `references/slack-gateway-setup.md`.
Slack Web API pitfalls for custom bridges: send **form-encoded** bodies (`application/x-www-form-urlencoded`); read methods such as `users.lookupByEmail`, `users.info` and `conversations.history` return `invalid_arguments` for a JSON body even though `chat.postMessage` accepts JSON. A bot token gets `not_allowed_token_type` from `users.setPhoto`: the bot's avatar is the app icon, set only in the app's Basic Information page, so hand the user the image file and that page. Link app settings as `https://app.slack.com/app-settings/<TEAM_ID>/<APP_ID>/app-manifest` (team from `auth.test`, app from `bots.info`); the old `api.slack.com/apps/<APP_ID>/app-manifest` path can fail. Adding scopes/events: put the manifest JSON on the clipboard, user pastes it in the JSON tab, saves and reinstalls; then store the bot token they copy, restart the bridge, and confirm the grant from the `x-oauth-scopes` response header of `auth.test`. If `launchctl bootstrap` returns exit 5 right after a bootout, the job is just unloaded: retry the bootstrap once. For a single token, `scripts/slack-token-from-clipboard` (copy it, run the script) stores it in `~/.hermes/.env` without printing it. To capture tokens without chat exposure, run a background clipboard watcher that matches `xoxb-`/`xapp-`, verifies each (`auth.test` / `apps.connections.open`), stores it, and only then clears the clipboard; users often copy one token at a time, so store each as it arrives.
Posting/replying in Slack **as the user** (connector, or the signed-in Chrome tab via chrome-devtools + `chat.postMessage`), with pre/post thread read-back: `references/slack-post-as-user.md`.

User is terse and dislikes multi-choice menus — just execute the next concrete step and surface a single actionable artifact (QR image, URL, path) at a time. Don't ask "which first?" when the obvious order is: start the bridge → render QR → ask them to scan.

## Always-on rules

- **For “report to the channel and ask for fixes,” read the relevant discussion and verified report before composing.** Lead with the measured outcome, separate confirmed mechanisms from hypotheses, include the minimal reproduction conditions and evidence links, and ask for tested fixes or code references rather than a vague opinion. Identify assistant-authored posts as sent on the user's behalf. After posting, distinguish verified delivery from receiving an answer; never imply consultation is complete or promise monitoring without an actual follow-up mechanism.

0. **An assistant/bot persona gets its own account, never the user's.** On the user's own WhatsApp/Signal account the bot's replies appear as messages *from the user*, so the phone never notifies them of replies or late follow-ups. Give the bot a separate number (WhatsApp Business app runs alongside regular WhatsApp on the same phone) and link the bridge to that. Before asking the user to obtain a number, search the secret store for one they already registered (token-broker skill, "Finding a credential").
0a. **Give the bot's number a NEW registration in WhatsApp Business; never use Settings → Account → Change Number in the user's regular WhatsApp.** Change Number moves the whole account (every group, admin and creator rights) to the new number. Registering Business on that number then inherits all of it, and re-registering the old number makes a fresh account with no groups, so the user can't post in any group. Spell out in the setup steps which app to open and that it is a fresh registration. Recovery: `references/whatsapp-account-recovery.md`.
0b. **Check for an existing bridge before building one.** `pgrep -fl whatsapp-bridge`, `launchctl list | grep -i bridge`, and `sqlite3 store/whatsapp.db "select jid,push_name from whatsmeow_device"` show which account an already-running bridge is linked to. Never reuse or wipe another bridge's store; a new bridge gets its own store dir and port.

1. **Never shell-background with `nohup`/`disown`/`setsid`.** Hermes' terminal refuses those wrappers; use `terminal(background=true, notify=[...])` and watch the log with `notify` patterns.
2. **Render QRs as PNG, not ASCII, when delivering to chat.** Terminal-QR half-blocks mangle in message bubbles. Patch the bridge to also write the raw QR string to a file, then `qrencode -o qr.png -s 12 -m 3 < qr.txt` and deliver via `MEDIA:` path.
3. **Deliver one artifact per turn** when the user has to act on their phone. Show the QR, then stop talking; don't chain three unrelated prompts underneath.
0c. **Creating groups / adding members is an outward action — never do it until the user explicitly says go.** Build and present the roster first (google-workspace `references/people-lists-from-drive.md`), iterate on edits, and keep waiting even after edits stop; an approved-looking list is not a go. The `lharries/whatsapp-mcp` bridge only exposes `/api/send` and `/api/download`, so group creation needs a new endpoint (whatsmeow `CreateGroup`, then promote co-admins via `UpdateGroupParticipants`) and a linked session.
3a. **Name every Hermes-linked device `Hermes (<host>)`, e.g. `Hermes (MBP)`, before showing the QR.** The user picks devices out of Linked Devices by that name; set `store.DeviceProps.Os = proto.String("Hermes (MBP)")` before `sqlstore.New` (QR flow accepts any name; PairPhone needs `Browser (OS)`, see below). Renaming after linking needs a relink.
3b. **Choosing how an agent gets onto WhatsApp** (WhatsApp's native third-party agents, Cloud API, or a linked whatsmeow device): `references/whatsapp-integration-options.md`.
4. **Session files are the whole value — back them up before rebuilding.** `store/whatsapp.db` and `~/.local/share/signal-cli/` are what makes auth persistent; wiping them forces a new QR scan.

## Sending something to a third party on the user's behalf ("share X with <person> on Signal")

1. Resolve the person in Contacts (`contacts_cli.py <name>` on the mini). Several cards with the same first name often share one number, so dedupe by phone before asking which one is meant.
2. BB's own chat bridges (`messaging_cli.py`) can only message people on that bridge's `allowedUsers`. That list is an engagement allowlist: anyone on it can command BB (lights, garage). Never add a non-household person to it just to deliver a message.
3. Send as the user: first check for a signal-cli account linked to the user's own number (`signal-cli listAccounts`; see Usage after link below), else use Signal Desktop on the MacBook (`open "sgnl://signal.me/#p/+E164"`, put the text on the clipboard, paste, Enter). Before any UI automation, check the screen is unlocked: babybear `screen_capture` on the laptop target reports frontmost `loginwindow` when it's locked, and `osascript` keystrokes then hang until the command times out.
4. A link meant to stay unlisted goes out with a one-line "private link, please don't post it" note. Report it sent only once the send path confirms it (a message id, or a screenshot of the sent bubble).

## WhatsApp — `lharries/whatsapp-mcp` whatsmeow bridge

Clone, build, run. Session persists in `whatsapp-bridge/store/whatsapp.db`.

```bash
mkdir -p ~/hermes-bridges && cd ~/hermes-bridges
git clone --depth 1 https://github.com/lharries/whatsapp-mcp.git
cd whatsapp-mcp/whatsapp-bridge
go build -o whatsapp-bridge .
```

### Hermes tool access to several accounts

Current install: `~/hermes-bridges/whatsapp-mcp` (patched), stores in `~/hermes-bridges/wa-<name>/`, launchd `com.daveey.hermes.wa-<name>`, MCP servers `whatsapp-<name>` (personal on 8081, bb on 8082). Check these before building anything (rule 0b).

1. One patched lharries binary serves every account via env: `WA_HOME` (store dir; chdir there at start so every relative `store/...` path follows), `WA_PORT` (REST bound to 127.0.0.1), `WA_EXPECTED` (E.164 without +; any other linked account is logged out, exit 3). Also patch in: LID resolution in send (pitfall below), `qr.txt` dump, a `LINKED_AS +<num>` log line on connect, and the device name (rule 3a).
2. One launchd agent per account (`RunAtLoad`, `KeepAlive`, `ThrottleInterval 10`, log to `$WA_HOME/bridge.log`). The QR wait exits after ~3 min; KeepAlive restarts it with a fresh code, so the live QR page keeps working.
3. Pair both at once: one HTML page with a QR per account, each labelled with app name + number, refreshed every 2 s from a loop that `qrencode`s each `qr.txt` and reports `LINKED_AS` from the log (`terminal(background=true, notify=["BOTH_LINKED"])`). Write the page and loop with `write_file`; the terminal rejects a command containing `&` backgrounding even inside a heredoc.
4. Register MCP: `yes Y | hermes mcp add whatsapp-<name> --command /opt/homebrew/bin/uv --env WA_HOME=.. WA_PORT=.. --args --directory <repo>/whatsapp-mcp-server run main.py`. Agents can't edit `config.yaml` directly; `hermes mcp add` prompts for tool selection and cancels on EOF, so pipe `yes`. The Python server needs the same env patch (`MESSAGES_DB_PATH` from `WA_HOME`, API URL from `WA_PORT`).
5. Verify: `whatsmeow_device` jid matches, `messages.db` chat/message counts are non-zero after history sync, and `whatsapp.list_chats()` works via `uv run` with the env. Don't send a test from the user's personal account unasked.

Chats on LID accounts list by LID digits, not contact names. Bulk mark-read: the patched bridge has `POST /api/markread {"jids":[...]}` (`SendAppState(BuildMarkChatAsRead(jid,true,time.Time{},nil))`, zero time = now); pick targets from the Mac desktop `ChatStorage.sqlite` `ZWACHATSESSION.ZUNREADCOUNT`, exclude chats with real recent messages in the bridge's `messages.db`, test one, and verify the desktop count drops to 0 (~20 s lag). A linked device that stays offline ~14 days gets dropped, so a laptop host is weaker than an always-on one; say so.

### Pitfall: `Client outdated (405) connect failure`

WhatsApp rejects stale `whatsmeow` client-version strings. The upstream repo's pinned version lags. Fix every time this error appears in the bridge log:

```bash
go get -u go.mau.fi/whatsmeow@latest
go mod tidy
```

Recent whatsmeow added `context.Context` as the first arg to several APIs; after `go mod tidy` the build will fail at ~5 call sites in `main.go`. Add `context.Background()` as the first argument to each:

- `client.Download(downloader)` → `client.Download(context.Background(), downloader)`
- `sqlstore.New("sqlite3", ..., dbLog)` → `sqlstore.New(context.Background(), "sqlite3", ..., dbLog)`
- `container.GetFirstDevice()` → `container.GetFirstDevice(context.Background())`
- `client.GetGroupInfo(jid)` → `client.GetGroupInfo(context.Background(), jid)`
- `client.Store.Contacts.GetContact(jid)` → `client.Store.Contacts.GetContact(context.Background(), jid)`

Then rebuild. These are mechanical — the compiler lists every site.

### Patch for chat-deliverable QR

In `main.go` the QR handler only prints ASCII. Add a raw-code dump next to the terminal render so you can `qrencode` it:

```go
if evt.Event == "code" {
    fmt.Println("\nScan this QR code with your WhatsApp app:")
    qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
    _ = os.WriteFile("qr.txt", []byte(evt.Code), 0644)
    fmt.Println("QR_RAW_WRITTEN")
}
```

Run flow (via `terminal(background=true, notify=["QR_RAW_WRITTEN","Successfully"])`):

```bash
cd ~/hermes-bridges/whatsapp-mcp/whatsapp-bridge
rm -rf store qr.txt whatsapp.log   # ONLY on first setup; preserves session otherwise
./whatsapp-bridge > whatsapp.log 2>&1
```

After `QR_RAW_WRITTEN`:

```bash
qrencode -o ~/hermes-bridges/whatsapp-qr.png -s 12 -m 3 < qr.txt
```

Deliver the PNG via `MEDIA:/Users/.../whatsapp-qr.png` and tell the user: WhatsApp → Settings → Linked Devices → Link a Device. If the bridge runs on another host, a better option than a static PNG: a small loop (`terminal(background=true, notify=["LINKED"])`) that copies the remote `qr.png` locally every 3 s, plus an HTML page that reloads the image and switches to "Linked ✓". Open that page in the Hermes preview pane (`desktop_preview open <path>.html`) so the code the user scans is never stale. Before rewriting the page after a previous run, read it first, because the loop overwrites it on success.

### Pitfall: the QR got scanned by the wrong account

When the bot has its own number in WhatsApp Business on the user's phone, the regular WhatsApp app on that phone scans the same QR just as happily, and the bridge links to the user's personal account. Prevent and detect it:
- Name the app in every scan instruction and on the QR page: "scan from **WhatsApp Business** (signed in as +1 …), not regular WhatsApp".
- Right after `paired`/`connected`, check the linked JID against the expected number (`sqlite3 store.db "select jid from whatsmeow_device"` or the bridge's status doc). Never report "linked" or send a test message before this check passes. Bridges you build should enforce it: a configured `expectedAccount`; on mismatch call `client.Logout`, publish an error state, and go back to pairing.
- To unlink a wrong session by hand: stop the service, back up the store, then run a ~25-line Go program against the store: `sqlstore.New(ctx, "sqlite3", "file:<store>?_foreign_keys=on", waLog.Noop)`, then `GetFirstDevice`, `NewClient`, `Connect`, sleep ~3 s, `Logout(ctx)`. Build it with the bridge's own `go.mod` so the whatsmeow version matches. `Logout` also removes the device from the phone's Linked Devices. Run it again to confirm `d.ID == nil`, then restart the service; it comes back in pairing mode. Don't just delete the store file: that leaves a dead linked-device entry that takes up one of the 4 slots.

### Pitfall: QR auto-rotates every ~20s inside the SAME bridge run

The `evt.Event == "code"` branch fires repeatedly while unpaired — whatsmeow rotates the pairing code roughly every 20 seconds. The ASCII QR you see in `whatsapp.log` is a historical dump of past rotations, not what the user's phone will accept right now. Only `qr.txt` holds the live code (your patch overwrites it on every rotation). When the user is slow to scan, **re-render the PNG from the current `qr.txt` and resend — do NOT restart the bridge**. Restarting resets the device-side pairing session and wastes a device slot.

### Pitfall: "I scanned, device didn't link"

If the user says they scanned successfully but `store/whatsapp.db` mtime hasn't advanced and the log has no `Successfully paired`, the PNG they scanned was from a prior rotation. Re-render from the current `qr.txt` and ask them to retry. Do not add new bridges or new patches.

### Fallback: phone pair code (no QR)

When the user cannot scan (bad camera, repeated stale-PNG failures), whatsmeow supports an 8-char code the user types in WhatsApp instead. Gate on an env var so default QR flow is unchanged:

```go
if phone := os.Getenv("WHATSAPP_PAIR_PHONE"); phone != "" {
    client.Connect()
    code, err := client.PairPhone(context.Background(), phone, true,
        whatsmeow.PairClientChrome, "Chrome (macOS)")
    if err != nil { logger.Errorf("Failed to request pair code: %v", err); return }
    fmt.Printf("\nPAIR_CODE: %s\n", code)
    _ = os.WriteFile("pair_code.txt", []byte(code), 0644)
    go func() {
        qch, _ := client.GetQRChannel(context.Background())
        for evt := range qch { if evt.Event == "success" { connected <- true; return } }
    }()
}
```

Launch with `WHATSAPP_PAIR_PHONE="<E164-without-plus>"`, e.g. `16506446875` not `+16506446875`. User: WhatsApp → Linked Devices → Link a Device → **Link with phone number instead** → types code.

### Pitfall: `PairPhone` returns `400 bad-request`

First check the display-name argument: it must have the form `"Browser (OS)"` with a real OS, e.g. `"Chrome (macOS)"`. WhatsApp rejects a custom name like `"Chrome (<bot name>)"` with 400 on every attempt, even on a fresh number with no linked devices. Don't conclude the number is unregistered from a 400 until the name is fixed.

Otherwise, almost always: the WhatsApp account is at the 4-device limit, with stale pairings from earlier QR attempts counting as live slots. Tell the user to open WhatsApp → Settings → Linked Devices and delete every entry that isn't the current session (`Chrome`, `Mac`, `Firefox`, `Linux`, etc.), then retry. Toggling `+` on/off in the phone string does NOT fix 400 — don't burn a cycle on it.

On successful pair the bridge logs `Successfully connected and authenticated!`. Independent check: `store/whatsapp.db` mtime bumps past the pre-pairing timestamp. From then on just relaunching `./whatsapp-bridge` resumes the session — no QR.

### Pitfall: sends to a phone-number JID are acked but never delivered (LID accounts)

Many accounts are migrated to LIDs (`<digits>@lid`). `SendMessage` to `<phone>@s.whatsapp.net` for such a recipient returns a server message ID and no error, but whatsmeow builds no Signal session with the recipient's devices, so nothing arrives. Resolve the recipient first: `client.Store.LIDs.GetLIDForPN(ctx, pnJID)`, falling back to `client.IsOnWhatsApp(ctx, []string{"+E164"})` (its result `.JID` is the LID). Send/edit/typing go to that JID. For replies, reuse the inbound message's chat JID unchanged. **A returned message ID is not proof of delivery.** Check `sqlite3 store.db "select their_id from whatsmeow_sessions"` for `<lid>_1:<device>` rows for the recipient, or have the user confirm receipt, before you report a message as sent.

### Triage: "I didn't get it" / "it didn't answer me"

Read the bridge's own log before blaming delivery or the bot's brain:
- **Outbound not received:** see the LID pitfall above. Check for recipient session rows, not the returned message ID.
- **Inbound not answered:** grep the service log for the inbound line (e.g. `whatsapp <id> -> hub <id>`) or an `ignoring ... from` line around the user's send time. If there is no line at all, the message never reached the bridge. The usual cause is that the user typed it **inside the WhatsApp Business app** (the bot's own account), so it went out *as* the bot and the bridge drops `IsFromMe` messages. The other cause is that it was sent while the bridge was restarting. Ask which app they typed in, and tell them to text the bot's contact from their regular WhatsApp. Don't debug the assistant's tool use for a turn that never existed.
- **Replies are slow:** time each hop before tuning the bot. That means the network message timestamp, when the bridge received it, when it was submitted to the backend, the first reply, and when that reply was delivered. Log all five on one line per message. Then check the bridge's inbound path for a round trip it waits on. A common bug in wrappers around a stdio sidecar (a whatsmeow or signal-cli process speaking JSON lines) is a self-deadlock: the loop that reads the sidecar's stdout awaits the event handler, and the handler awaits a request/response (mark-read, typing) whose reply only that same loop can read. Every message then stalls for the full request timeout, and the timeout error is often logged only at debug level. Handlers must spawn a task or fire the request without awaiting it. Unit-test fakes that answer synchronously hide this, so test against a stand-in process that answers asynchronously over a real pipe.

- **User can't post in groups / gets no group messages after a number change:** the groups probably now belong to another account (often the bot's). Diagnose from evidence, then fix: `references/whatsapp-account-recovery.md`. The same file covers chats that come back archived (or pinned/muted) after the account moves: that state follows the account, and it can be bulk-fixed from a temporary linked device, keeping the chats that were archived before the incident.

### Profile picture and other one-off account ops

Set the linked account's own avatar with `client.SetGroupPhoto(ctx, types.EmptyJID, jpegBytes)`; an empty target means "own profile". Pass a JPEG (convert with `sips -s format jpeg -Z 640 in.png --out out.jpg`); PNG gets `ErrInvalidImageFormat`. Verify with `GetProfilePictureInfo(ownJID)`. Don't ask the user to do it by hand on the phone unless they are in a hurry.

**Never open a live bridge's `store.db` with a second whatsmeow client while the bridge runs.** Two clients on one store corrupt the Signal/prekey state. Expose one-off ops (logout, set photo, probe, bulk mark-read) as REST endpoints on the running bridge: add the handler next to `/api/send`, rebuild, `launchctl kickstart -k` (the session survives a restart), call it with a one-item canary, verify, then batch. If a throwaway Go tool is unavoidable, stop the service first (`launchctl bootout`), run the tool, restart the service, and delete the tool. Never do this while another agent or Kanban card owns that bridge: put the request on its card with `kanban_comment` instead (`cross_session send` to a Kanban worker transcript is refused).

## Signal — `signal-cli` secondary device

```bash
brew install signal-cli qrencode
# Launch via terminal(background=true, pty=true, notify=["sgnl:", "Associated with:"])
signal-cli link -n "hermes-mac" 2>&1 | tee ~/hermes-bridges/signal-link.log
```

Stdout's first line is a `sgnl://linkdevice?uuid=...&pub_key=...&capabilities=...` URI (NOT `tsdevice:/` — that is an older format no longer emitted). Extract and render:

```bash
SIG=$(grep -oE 'sgnl://[^ ]+' ~/hermes-bridges/signal-link.log | head -1)
qrencode -o ~/hermes-bridges/signal-qr.png -s 12 -m 3 "$SIG"
```

Deliver PNG. User: Signal app → Settings → Linked Devices → + → scan. The `link` process blocks until paired, then prints `Associated with: +<E164>` and exits 0. That line is the only reliable success signal — the `sgnl:` match in notify fires on QR emission, not on success, so watch both.

**`pty=true` is required** on the terminal launch — signal-cli expects a TTY for its prompt handling and will hang silently without one. State lands in `~/.local/share/signal-cli/data/`.

### Usage after link

```bash
signal-cli listAccounts                                     # confirms +<E164>
signal-cli -a +<E164> send -m "test" +<E164>                # self-send verification
signal-cli -a +<E164> receive                               # pulls pending, idempotent
```

Every subsequent command needs `-a +<E164>`; signal-cli does not auto-select when multiple accounts are registered.

### Pitfalls

- `Link request timed out, please try again.` means the sgnl:// URI expired (~5min). Re-run `signal-cli link` — the old PNG is permanently dead.
- Do NOT run `signal-cli register` for a linked-device setup. That path creates a brand-new standalone Signal account and triggers SMS verification. Only use `link`.
- **A bot's OWN Signal account is the exception: register, don't link.** Use a separate `--config <dir>` so the user's linked account in `~/.local/share/signal-cli/` is untouched. Registration needs two human steps, so ask for them one at a time: (1) open https://signalcaptchas.org/registration/generate.html in the Hermes preview pane; the user solves it, right-clicks "Open Signal" → Copy Link and pastes the `signalcaptcha://…` token (it expires in minutes, so run `signal-cli -a +E164 --config <dir> register --captcha <token>` immediately); (2) the SMS (or `--voice`) code that arrives on the bot's line goes to `verify <code>`. Don't start this until the user is ready to act on both steps.
- `INFO ... The Signal protocol expects that incoming messages are regularly received.` is informational, not an error. Ignore it.

## iMessage/SMS

Use the `imessage` skill (`imsg` CLI). No pairing. One check up-front: `imsg chats --limit 1 --json` — if it errors with a permission diagnostic, Full Disk Access needs to be granted to the terminal binary that actually runs (on cmux.app this is **ghostty**, not cmux). After granting, fully quit the terminal and relaunch; the entitlement is read at launch.
