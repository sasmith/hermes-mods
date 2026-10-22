"""Terminal/password hand-off rule: a final reply must not hand David a step a shell could do.

Examples are scrubbed paraphrases of real replies (the private replay eval holds the originals).
"""
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ask_gate_handoff_t", HERE / "handoff.py")
handoff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handoff)

INCIDENT = ("Rose's Brain is set up, but the app on her desktop is quit and I couldn't reopen it.\n\n"
            "- SSH to `desk.local` refuses my key, and I won't try passwords.\n\n"
            "If you'd rather I SSH in, add `~/.ssh/id_ed25519.pub` to her account's `~/.ssh/authorized_keys`.")

HANDOFFS = [
    INCIDENT,
    "What's left for you:\n1. Try playing something. If it doesn't work, run on the box: "
    "`sudo launchctl kickstart -k system/com.apple.oahd`\n2. Reboot it when convenient.",
    "Run `sudo visudo -f /etc/sudoers.d/hermes` once on the box and add this line:\n\n```\nyou ALL=(ALL) NOPASSWD: /x\n```",
    "## Needs you\n2. **Spotlight:** run this from the laptop. It asks for the box's password:\n   ```\n"
    "   ssh -t host 'sudo mdutil -i off /Volumes/X'\n   ```",
    "Phases 0 and 1 are low-risk. Want me to start on them? Step 4 needs one `sudo` command from you.",
    "**Still open:**\n1. **Sign in your 5 accounts.**\n   ```sh\n   sudo docker exec -it app hermes auth add x\n   ```",
    "To get it sooner, run `hermes update` yourself and relaunch.",
    "To clear the stuck file tracker, run this there and enter the box's password when asked:\n"
    "```\nsudo killall fseventsd\n```\nTell me once it's done.",
    "Or run `sudo killall coreaudiod` in Terminal on the box itself. It asks for the password.",
    "The only thing left for you is turning off system sleep: `ssh host 'sudo pmset -a sleep 0'`.",
    "For the 5 logins, run this in your own terminal (each one prints a URL):",
    "Optional: run `sudo mdutil -i off /Volumes/Assets` on the box so Spotlight stops indexing it.",
    "::user-step{id=\"us_1\" s=\"s\" t=\"If you'd rather I SSH in, add `~/.ssh/id_ed25519.pub` to her `~/.ssh/authorized_keys`.\"}",
]

PHYSICAL = [
    "Log in to Rose's desktop at its screen and open the app; I'll take it from there.",
    "On your iPhone, tap **Stream**, then tell me when the dot turns green.",
    "Plug the USB drive into the box and tell me when it's in.",
    "Want me to ship it? Say go.",
    "Click **Allow** on the prompt on the desktop's own screen.",
    "Open **System Settings → Privacy & Security → Full Disk Access**, click **+** and paste this path.",
    "In the Accessibility settings, turn on **Terminal**.",
    "Paste the 6-digit code here and I'll run the verify step.",
    "The Chrome window is in front of you with your email filled in: type your password and approve the code.",
    "Don't paste the code into the chat. I'll read it from your clipboard.",
    "I ran `ssh-copy-id` through run_in_user_terminal; you typed the password and key auth works now.",
    "Done. I added the key to `~/.ssh/authorized_keys` on the box and `ssh box true` works.",
    "I'll check it and run it through the confirm-in-terminal flow rather than leave it for you to type.",
    "You finished the real setup in your terminal: the bridge is signed in.",
    "To take it down, run `launchctl bootout gui/501/com.example.demo`.",
    "- Gave t_12345678 the fix: unlock the keychain at the start of each build.",
    "`ssh box` works for you with passwordless sudo.",
    "It tells me to run `ssh-copy-id` (or the command itself) in your terminal, then carry on.",
    "Objections (from the quotes): \"do it yourself via ssh\" and \"why didn't you do this yourself\".",
]


@pytest.mark.parametrize("text", HANDOFFS)
def test_handoffs_are_found(text):
    assert handoff.find_handoffs(text), text


@pytest.mark.parametrize("text", PHYSICAL)
def test_physical_and_reports_are_not_handoffs(text):
    assert handoff.find_handoffs(text) == [], text


def test_opened_terminal_detects_tool_and_handrolled_windows():
    assert handoff.opened_terminal([{"tool": "run_in_user_terminal", "args": "{}"}])
    assert handoff.opened_terminal([{"tool": "tool_call", "args": '{"name": "run_in_user_terminal"}'}])
    assert handoff.opened_terminal([{"tool": "terminal", "args": 'osascript -e \'tell application "Terminal" to do script\''}])
    assert not handoff.opened_terminal([{"tool": "terminal", "args": "ssh -o BatchMode=yes host true"}])


# ---- the gate --------------------------------------------------------------------------------------------
def _turn(*calls):
    msgs = []
    for i, (name, args, result) in enumerate(calls):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "function": {"name": name, "arguments": args}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": result})
    return msgs


SSH_FAIL = ("terminal", '{"command": "ssh -o BatchMode=yes desk.local true"}',
            "Permission denied (publickey,password,keyboard-interactive).")


def test_gate_holds_incident_without_model_call(gate):
    r = gate.pre_final_gate(session_id="s1", platform="tui", final_response=INCIDENT,
                            turn_messages=_turn(SSH_FAIL), attempt=0)
    assert r and r["hold"]
    assert "run_in_user_terminal" in r["nudge"] and "authorized_keys" in r["nudge"]
    assert "ssh-copy-id" in r["nudge"] and "physical" in r["nudge"]


def test_gate_lets_handoff_through_when_turn_opened_terminal(gate):
    gate.LLM = lambda s, u: '{"asks": []}'
    text = "In the Terminal window, type the box's password; the rest runs by itself."
    r = gate.pre_final_gate(session_id="s1", platform="tui", final_response=text, attempt=0,
                            turn_messages=_turn(SSH_FAIL, ("tool_call", '{"name": "run_in_user_terminal"}', "{}")))
    assert r is None or not r.get("hold")


@pytest.mark.parametrize("text", PHYSICAL[:3])
def test_gate_never_holds_physical_steps_for_handoff(gate, text):
    gate.LLM = lambda s, u: '{"asks": [{"i": 0, "verdict": "ok", "reason": "physical"}]}'
    r = gate.pre_final_gate(session_id="s1", platform="tui", final_response=text, attempt=0)
    assert r is None or "terminal/password" not in (r.get("nudge") or "")


def test_gate_handoff_nudge_budget(gate):
    gate.LLM = lambda s, u: '{"asks": [{"i": 0, "verdict": "ok", "reason": "x"}]}'
    assert gate.pre_final_gate(session_id="s1", platform="tui", final_response=INCIDENT, attempt=1)["hold"]
    r = gate.pre_final_gate(session_id="s1", platform="tui", final_response=INCIDENT, attempt=2)
    assert r is None or "terminal/password" not in (r.get("nudge") or "")


def test_gate_skips_kanban_workers(gate, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    assert gate.pre_final_gate(session_id="s1", platform="tui", final_response=INCIDENT, attempt=0) is None


def test_gate_howto_question_gets_instructions(gate):
    gate.LOG.set_users("s1", ["how do I add my key to the box myself?"])
    r = gate.pre_final_gate(session_id="s1", platform="tui", final_response=INCIDENT, attempt=0)
    assert r is None or "terminal/password" not in (r.get("nudge") or "")
