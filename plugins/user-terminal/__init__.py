"""user-terminal — run a command in a real terminal window on the user's screen.

Instead of telling the user "please run X in your terminal", the agent calls ``run_in_user_terminal``.
The plugin opens a terminal window (iTerm if installed, else Terminal.app, else cmux), shows why the agent
wants to run the command, the working directory and the exact command, and runs it right away (the user
asked for no y/N step: they always said yes). Ctrl-C in the window cancels it. Config
``user_terminal.confirm`` (env ``HERMES_USER_TERMINAL_CONFIRM``) = ``off`` (default) | ``remote`` (ask
``Run it? [y/N]`` only for jobs relayed from another gateway) | ``all``. The command runs in a real TTY (so sudo passwords, OAuth device codes, interactive
logins and TCC prompts all work, with the terminal's permissions). The tool waits for the outcome and
returns the exit code plus the captured output to the agent.

Jobs live in ``<hermes home>/cache/user-terminal/<job_id>/``:
  meta.json   reason / cwd / command (shown by the runner)
  cmd.sh      the command, verbatim
  runner.sh   the show-and-run script the window executes
  confirm     present -> the runner asks y/N first (see confirm_mode)
  output.log  everything printed while the command ran (via script(1))
  status.json {"status": "running"|"ran"|"declined", "exit_code": n}

  origin.txt  who asked, for a request relayed from another gateway (shown by the runner)

Terminal choice: iTerm > Terminal.app > cmux; env HERMES_USER_TERMINAL = iterm | terminal | cmux tries that one first.
(cmux is last: its control socket only accepts processes started inside cmux by default.)

Remote mode (a gateway with no screen: Linux/cloud). The same tool is registered, but the handler relays
the job to the owner's Mac through the session-mentions courier: it writes a ``user_terminal`` request into
``<root>/session_mentions/outbox/`` addressed (``route``) to the Mac's device name (config
``user_terminal.device``, env ``HERMES_USER_TERMINAL_DEVICE``, else the only live courier). That Mac's
Desktop courier claims it and calls this plugin's own dashboard routes on the Mac's backend
(``dashboard/plugin_api.py``: ``POST /start``, ``GET /status/<job>``), which run the local path above
(``create_job`` + ``open_window`` + ``wait_for``). The window names the originating
gateway/profile/session. Only the owner's own sessions (desktop/cli/tui/api_server) may call it; a session
on a messaging platform (e.g. Slack on a shared bot) is refused. No live courier for that Mac -> an
immediate error, never a silent wait.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

TOOLSET = "user_terminal"
DEFAULT_WAIT = 600
MAX_WAIT = 1800
OUTPUT_TAIL = 12000
CMUX_APP = Path("/Applications/cmux.app")
CMUX_BIN = CMUX_APP / "Contents/Resources/bin/cmux"


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def jobs_dir() -> Path:
    d = Path(os.environ.get("HERMES_USER_TERMINAL_DIR") or _home() / "cache" / "user-terminal")
    d.mkdir(parents=True, exist_ok=True)
    return d


SCHEMA = {
    "name": "run_in_user_terminal",
    "description": (
        "Run a shell command in a terminal window on the user's screen, where they can type passwords "
        "and answer prompts. Use this whenever you would otherwise ask the user to run a command themselves "
        "— anything needing their password (sudo), an interactive login/OAuth/device code, a TTY prompt, "
        "or their terminal's macOS permissions. It is not an approval gate (no y/N): get a go-ahead in chat "
        "first for anything destructive. Never tell "
        "the user to copy/paste a command; call this instead. The window shows `reason`, `cwd` and the "
        "exact `command`, runs it right away (no y/N step; the user can Ctrl-C it), and this tool returns status "
        "(ran/declined/pending), exit_code and the output. The user can type into the window if the "
        "command prompts. If it is still pending after `wait_s`, call again with only `job_id` to keep waiting."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run (runs in the user's login shell)."},
            "reason": {"type": "string", "description": "One or two plain sentences shown above the command: why it is needed and what it will do."},
            "cwd": {"type": "string", "description": "Working directory (default: user's home)."},
            "wait_s": {"type": "integer", "description": f"Seconds to wait for the user + command (default {DEFAULT_WAIT}, max {MAX_WAIT})."},
            "job_id": {"type": "string", "description": "Resume waiting on an earlier pending job instead of starting a new one."},
        },
    },
}


RUNNER = r"""#!/bin/bash
JOB="$1"
B=$'\e[1m'; D=$'\e[2m'; Y=$'\e[33m'; G=$'\e[32m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
status() { printf '%s' "$1" > "$JOB/status.json.tmp" && mv "$JOB/status.json.tmp" "$JOB/status.json"; }
if [ -s "$JOB/origin.txt" ]; then printf '\e]0;Hermes (%s) wants to run a command\a' "$(head -n1 "$JOB/origin_short.txt" 2>/dev/null)"; else printf '\e]0;Hermes wants to run a command\a'; fi
clear
W=$(tput cols 2>/dev/null || echo 100); W=$(( W > 120 ? 120 : W )); W=$(( W < 40 ? 40 : W ))
wrap() { fold -s -w $((W - 2)) | sed -e 's/[[:space:]]*$//' -e 's/^/  /'; }
echo "${B}${C}━━━ Hermes wants to run a command ━━━${N}"
echo
if [ -s "$JOB/origin.txt" ]; then echo "${B}${R}From another machine:${N}"; wrap < "$JOB/origin.txt"; echo; fi
if [ -s "$JOB/reason.txt" ]; then echo "${B}Why:${N}"; tr -s ' \n' ' ' < "$JOB/reason_display.txt" | wrap; echo; echo; fi
echo "${B}Where:${N} $(cat "$JOB/cwd_display.txt")"
echo
echo "${B}Command:${N}"
printf '%s' "${Y}"; wrap < "$JOB/cmd_display.txt"; printf '%s' "${N}"
echo
if [ -f "$JOB/confirm" ]; then
  while true; do
    read -r -p "${B}Run it? [y/N]${N} " ans
    case "$ans" in
      y|Y|yes|YES) break ;;
      ""|n|N|no|NO) status '{"status":"declined"}'; echo "${R}Declined — told Hermes.${N}"; sleep 2; exit 0 ;;
    esac
  done
else
  echo "${D}Running now. Ctrl-C cancels.${N}"
fi
echo "${D}──────────────────────────────────────────${N}"
status '{"status":"running"}'
cd "$(cat "$JOB/cwd.txt")" || { status '{"status":"ran","exit_code":1,"error":"cwd not found"}'; read -r -p "cwd not found. Enter to close. "; exit 1; }
SH="${SHELL:-/bin/zsh}"
/usr/bin/script -q "$JOB/output.log" /bin/bash -c "\"$SH\" -l \"$JOB/cmd.sh\"; echo \$? > \"$JOB/rc\""
rc=$(cat "$JOB/rc" 2>/dev/null || echo 1)
status "{\"status\":\"ran\",\"exit_code\":$rc}"
echo "${D}──────────────────────────────────────────${N}"
if [ "$rc" = 0 ]; then echo "${G}✓ Done (exit 0). Output sent to Hermes.${N}"; else echo "${R}✗ Exit $rc. Output sent to Hermes.${N}"; fi
read -r -t 600 -p "Press Enter to close. "
exit 0
"""


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg})


# ---------------------------------------------------------------- opening a window

ITERM_APPS = (Path("/Applications/iTerm.app"), Path.home() / "Applications/iTerm.app")


def _has_iterm() -> bool:
    return any(p.exists() for p in ITERM_APPS)


def terminal_order() -> list[str]:
    """iTerm if installed, then Terminal.app, then cmux. HERMES_USER_TERMINAL puts one first."""
    order = [k for k, ok in (("iterm", _has_iterm()), ("terminal", True), ("cmux", CMUX_BIN.exists())) if ok]
    want = (os.environ.get("HERMES_USER_TERMINAL") or "").strip().lower()
    if want in order:
        order.remove(want)
        order.insert(0, want)
    return order


def _cmux(*args: str, timeout: float = 5) -> subprocess.CompletedProcess:
    return subprocess.run([str(CMUX_BIN), *args], capture_output=True, text=True, timeout=timeout)


def _open_cmux(shell_cmd: str, title: str) -> str:
    ok = _cmux("ping").returncode == 0
    if not ok:
        subprocess.run(["open", "-a", str(CMUX_APP)], check=False)
        for _ in range(40):
            time.sleep(0.5)
            if _cmux("ping").returncode == 0:
                ok = True
                break
    if not ok:
        raise RuntimeError("cmux did not come up")
    out = _cmux("new-workspace", "--command", shell_cmd)
    if out.returncode != 0:
        raise RuntimeError(f"cmux new-workspace failed: {out.stderr.strip() or out.stdout.strip()}")
    m = re.search(r"workspace:\d+|[0-9A-Fa-f-]{36}", out.stdout)
    if m:
        _cmux("rename-workspace", "--workspace", m.group(0), title)
        _cmux("select-workspace", "--workspace", m.group(0))
    subprocess.run(["open", "-a", str(CMUX_APP)], check=False)
    return "cmux"


def _osa_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


COLS, ROWS = 110, 34   # the confirm window: wide enough that the wrapped text is not re-wrapped, centred on screen

# AppleScript: centre window `w` of the frontmost app on the main screen, keeping its size.
_CENTER = """
tell application "Finder" to set {sx, sy, sw, sh} to bounds of window of desktop
tell application {app} to set {x1, y1, x2, y2} to bounds of w
set ww to x2 - x1
set wh to y2 - y1
set nx to (sw - ww) div 2
set ny to (sh - wh) div 3
tell application {app} to set bounds of w to {nx, ny, nx + ww, ny + wh}
"""


def _center(app: str) -> str:
    return _CENTER.replace("{app}", _osa_quote(app))


def _open_terminal_app(shell_cmd: str) -> str:
    script = (f'tell application "Terminal"\nset t to do script {_osa_quote(shell_cmd)}\nset w to front window\n'
              f'set number of columns of w to {COLS}\nset number of rows of w to {ROWS}\nactivate\nend tell\n'
              + _center("Terminal"))
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=20)
    return "Terminal"


def _open_iterm(shell_cmd: str) -> str:
    script = ('tell application "iTerm"\nactivate\nset w to (create window with default profile command '
              + _osa_quote(shell_cmd) + f')\ntell current session of w\nset columns to {COLS}\nset rows to {ROWS}\n'
              'end tell\nend tell\n' + _center("iTerm"))
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=20)
    return "iTerm"


def open_window(job: Path) -> str:
    run_cmd = f"/bin/bash {shlex.quote(str(job / 'runner.sh'))} {shlex.quote(str(job))}"
    shell_cmd = run_cmd + "; exit"  # typed into an interactive shell (Terminal.app, cmux)
    errors = []
    for k in terminal_order():
        try:
            if k == "cmux":
                return _open_cmux(shell_cmd, "Hermes: confirm command")
            if k == "iterm":
                return _open_iterm(run_cmd)  # iTerm execs it directly, no shell
            if k == "terminal":
                return _open_terminal_app(shell_cmd)
        except Exception as e:  # fall through to the next terminal
            errors.append(f"{k}: {e}")
    raise RuntimeError("; ".join(errors) or "no terminal available")


# ---------------------------------------------------------------- jobs

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[()][A-Z0-9]")


def clean_output(raw: str) -> str:
    text = _ANSI.sub("", raw).replace("\r\n", "\n")
    lines = [ln.rsplit("\r", 1)[-1] for ln in text.split("\n")]  # keep what a \r-redraw left visible
    text = "\n".join(lines).replace("\x04", "").replace("\x08", "")
    return text.strip("\n")


_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]")


def visible(text: str) -> str:
    """Control characters (ESC, CR, BEL, C1...) shown as ^X / <U+..> so a relayed command, reason or origin
    cannot redraw the confirm window (hide part of the command, fake a prompt). Newlines and tabs stay."""
    def sub(m: re.Match) -> str:
        c = ord(m.group(0))
        return f"^{chr(c + 64)}" if c < 32 else ("^?" if c == 127 else f"<U+{c:04X}>")
    return _CTRL.sub(sub, str(text or ""))


def origin_lines(origin: dict | None) -> tuple[str, str]:
    """(full text for the window, short label for its title) for a relayed request's origin."""
    if not isinstance(origin, dict) or not origin.get("gateway"):
        return "", ""
    one = lambda k: visible(str(origin.get(k) or "")).replace("\n", " ").strip()[:200]  # noqa: E731
    gw, profile, sid, title = one("gateway"), one("profile") or "default", one("session_id"), one("title")
    text = f"Gateway {gw} · profile {profile}"
    if sid:
        text += f" · session {sid}"
    if title:
        text += f' ("{title}")'
    return text + "\n", f"{gw}/{profile}"


def confirm_mode() -> str:
    """off (default) | remote | all — when the runner asks ``Run it? [y/N]`` before running."""
    mode = os.environ.get("HERMES_USER_TERMINAL_CONFIRM", "")
    try:
        from hermes_cli.config import load_config  # type: ignore

        v = ((load_config() or {}).get("user_terminal") or {}).get("confirm")
        if v is not None and str(v).strip():
            mode = "all" if v is True else "off" if v is False else str(v)
    except Exception:
        pass
    mode = mode.strip().lower()
    return {"1": "all", "true": "all", "yes": "all", "on": "all", "0": "off", "false": "off", "no": "off"}.get(
        mode, mode if mode in ("off", "remote", "all") else "off")


def create_job(command: str, reason: str, cwd: str, origin: dict | None = None,
               confirm: bool | None = None) -> Path:
    job = jobs_dir() / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    job.mkdir(parents=True)
    (job / "cmd.sh").write_text(command.rstrip("\n") + "\n")
    (job / "cmd_display.txt").write_text(visible(command.rstrip("\n")) + "\n")
    (job / "reason.txt").write_text(reason.strip() + "\n" if reason.strip() else "")
    (job / "reason_display.txt").write_text(visible(reason.strip()) + "\n" if reason.strip() else "")
    (job / "cwd.txt").write_text(cwd)
    (job / "cwd_display.txt").write_text(visible(cwd))
    full, short = origin_lines(origin)
    if full:
        (job / "origin.txt").write_text(full)
        (job / "origin_short.txt").write_text(short + "\n")
    if confirm is None:
        mode = confirm_mode()
        confirm = mode == "all" or (mode == "remote" and bool(full))
    if confirm:
        (job / "confirm").write_text("1\n")
    (job / "runner.sh").write_text(RUNNER)
    (job / "status.json").write_text(json.dumps({"status": "waiting_for_user"}))
    (job / "meta.json").write_text(json.dumps({"command": command, "reason": reason, "cwd": cwd,
                                                "origin": origin if full else None, "created": time.time()}))
    return job


def job_origin_gateway(job: Path) -> str:
    try:
        return str((json.loads((job / "meta.json").read_text()).get("origin") or {}).get("gateway") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def read_status(job: Path) -> dict:
    try:
        return json.loads((job / "status.json").read_text())
    except (OSError, ValueError):
        return {"status": "unknown"}


def _interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted  # type: ignore

        return bool(is_interrupted())
    except Exception:
        return False


def wait_for(job: Path, wait_s: float) -> dict:
    deadline = time.time() + wait_s
    st = read_status(job)
    while st.get("status") not in ("ran", "declined") and time.time() < deadline and not _interrupted():
        time.sleep(0.5)
        st = read_status(job)
    result: dict[str, Any] = {"ok": True, "job_id": job.name, **st}
    if st.get("status") not in ("ran", "declined"):
        result["status"] = "pending" if st.get("status") != "running" else "running"
        result["note"] = ("Still waiting (user hasn't answered, or the command is still running). Call "
                          "run_in_user_terminal with this job_id to keep waiting.")
    log = job / "output.log"
    if log.exists():
        out = clean_output(log.read_text(errors="replace"))
        if len(out) > OUTPUT_TAIL:
            out = f"[… {len(out) - OUTPUT_TAIL} chars trimmed; full log {log}]\n" + out[-OUTPUT_TAIL:]
        result["output"] = out
    if result["status"] == "declined":
        result["note"] = "The user declined in the terminal. Do not re-run it unasked; ask what they want instead."
    return result


def handle(args: dict, **_kw: Any) -> str:
    try:
        wait_s = float(args.get("wait_s") or DEFAULT_WAIT)
        wait_s = max(1.0, min(wait_s, MAX_WAIT))
        job_id = str(args.get("job_id") or "").strip()
        if job_id:
            job = jobs_dir() / Path(job_id).name
            if not job.is_dir():
                return _err(f"unknown job_id {job_id}")
            return json.dumps(wait_for(job, wait_s))
        command = str(args.get("command") or "").strip()
        if not command:
            return _err("command is required (or job_id to resume waiting)")
        cwd = os.path.expanduser(str(args.get("cwd") or "").strip() or "~")
        if not os.path.isdir(cwd):
            return _err(f"cwd does not exist: {cwd}")
        job = create_job(command, str(args.get("reason") or ""), cwd)
        try:
            where = open_window(job)
        except Exception as e:
            return _err(f"could not open a terminal window: {e}")
        res = wait_for(job, wait_s)
        res["terminal"] = where
        return json.dumps(res)
    except Exception as e:  # never crash the agent loop
        return _err(f"{type(e).__name__}: {e}")


# Output shapes that mean "this needs the user's TTY/password". Core appends a SUDO_PASSWORD tip to the
# sudo ones, which steers the agent to tell the user to paste the command themselves. Plugin tools may be
# deferred behind tool_search, so the agent may not even see run_in_user_terminal at that moment.
# Line-anchored so a log or a grep that merely contains the text doesn't fire. Nested ssh (laptop -> mini ->
# host) prints the inner failure on its own line too, so it matches the same way.
_NEEDS_SUDO = re.compile(r"^(?:sudo: (?:a password is required|no tty present|a terminal is required)|"
                         r"sorry, you must have a tty to run sudo)", re.M)
_NEEDS_SSH = re.compile(
    r"^(?:[\w.-]+@[\w.\[\]:-]+: )?Permission denied \([^)\n]*(?:password|keyboard-interactive)[^)\n]*\)\.?\r?$|"
    r"^Enter passphrase for key\b|^[\w.-]+@[\w.-]+'s password: ?$|^\(?[\w.-]+@[\w.-]+\)? ?Password: ?$|"
    r"^(?:/usr/bin/)?ssh-copy-id: ERROR\b|^Too many authentication failures\b", re.M)
_NEEDS_OTHER = re.compile(r"could not read (?:Username|Password) for 'https?://|User interaction is not allowed", re.M)
_NEEDS_USER = ("sudo: a password is required", "sudo: no tty present", "sudo: a terminal is required")  # compat
_CORE_SUDO_TIP = re.compile(r"\n*💡 Tip: (?:To enable sudo over messaging|This session cannot prompt for a sudo password)[^\n]*")
_MARK = "💡 This needs the user's password/TTY."
_HEAD = ("\n\n" + _MARK + " Do NOT ask the user to run it themselves, do NOT hand it back as a step for them, and do "
         "NOT suggest SUDO_PASSWORD. Call run_in_user_terminal instead (tool_call name=\"run_in_user_terminal\", "
         "arguments {command, reason}). It opens a terminal on their screen, they type any password "
         "there, and you get the exit code + output back. ")
HINT = _HEAD + "For a remote host, wrap it: ssh -t <host> 'sudo ...'."
SSH_HINT = (_HEAD + "SSH refused key auth. Call run_in_user_terminal with `ssh-copy-id -i ~/.ssh/id_ed25519.pub "
            "<user>@<host>` (or the ssh command itself); the user types the password there; then continue with key "
            "auth. If the ssh ran on another host (a hop through the mini), wrap it: ssh -t <hop> 'ssh-copy-id "
            "<user>@<host>' so the key that gets installed is the hop's.")
OTHER_HINT = (_HEAD + "For git over HTTPS or a locked keychain, run the same command there (e.g. `git pull`, "
              "`security unlock-keychain`) so the user can type the credential.")


def needs_user(output: str) -> str | None:
    """Which hint fits this terminal output, or None."""
    if not isinstance(output, str) or _MARK in output:
        return None
    if _NEEDS_SUDO.search(output):
        return HINT
    if _NEEDS_SSH.search(output):
        return SSH_HINT
    if _NEEDS_OTHER.search(output):
        return OTHER_HINT
    return None


def transform_terminal_output(command: str = "", output: str = "", **_: Any) -> str | None:
    hint = needs_user(output)
    if hint is None:
        return None
    return _CORE_SUDO_TIP.sub("", output).rstrip() + hint


# ---------------------------------------------------------------- relayed jobs (Mac side)

def start_relayed(command: str, reason: str, cwd: str, origin: dict) -> dict:
    """Open the run window for a job relayed from another gateway (dashboard ``POST /start``)."""
    command = str(command or "").strip()
    if not command:
        return {"ok": False, "error": "command is required"}
    if not isinstance(origin, dict) or not str(origin.get("gateway") or "").strip():
        return {"ok": False, "error": "a relayed job must name its origin gateway"}
    cwd = os.path.expanduser(str(cwd or "").strip() or "~")
    if not os.path.isdir(cwd):
        return {"ok": False, "error": f"cwd does not exist on the Mac: {cwd}"}
    job = create_job(command, str(reason or ""), cwd, origin)
    try:
        where = open_window(job)
    except Exception as e:
        return {"ok": False, "error": f"could not open a terminal window on the Mac: {e}"}
    return {"ok": True, "job_id": job.name, "status": "waiting_for_user", "terminal": where}


def relayed_status(job_id: str, gateway: str, wait_s: float) -> dict:
    """Wait on a relayed job. Only the gateway that started it may read it (output can be private)."""
    job = jobs_dir() / Path(str(job_id or "")).name
    if not job_id or not job.is_dir() or not gateway or job_origin_gateway(job) != gateway:
        return {"ok": False, "error": f"unknown job_id {job_id}"}
    return wait_for(job, max(0.0, min(float(wait_s or 0), 25.0)))


# ---------------------------------------------------------------- remote mode (gateway with no screen)

# Surfaces where the session's user is the owner. Anything else (slack, telegram, discord, cron, ...)
# may be another person or nobody, so the tool refuses rather than open a window on the owner's Mac.
OWNER_SURFACES = frozenset({"desktop", "api_server", "tui", "cli", "local"})
COURIER_FRESH_S = 20.0
PICKUP_S = 15.0       # couriers drain every ~3 s
START_RESULT_S = 60.0  # the Mac opens the window and answers
POLL_S = 20            # each status request waits this long on the Mac


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env  # type: ignore

        return str(get_session_env(name, "") or "")
    except Exception:
        return os.environ.get(name, "")


def caller_surfaces(session_id: str | None) -> list[str]:
    """Every identity this turn has: process platform, session platform/source, the session row's source."""
    ids = [os.environ.get("HERMES_PLATFORM", ""), _session_env("HERMES_SESSION_PLATFORM"),
           _session_env("HERMES_SESSION_SOURCE")]
    if session_id:
        try:
            from hermes_state import SessionDB  # type: ignore

            row = SessionDB().get_session(session_id) or {}
            ids.append(str(row.get("source") or ""))
        except Exception:
            pass
    return sorted({str(i).strip().lower() for i in ids if str(i or "").strip()})


def _owner_slack_dm() -> bool:
    """The session is the owner's own Slack DM with the bot (config owner.slack_user_ids; Slack-verified fields)."""
    try:
        from hermes_cli.config import load_config  # type: ignore
        ids = (((load_config() or {}).get("owner") or {}).get("slack_user_ids")) or []
    except Exception:
        ids = []
    if isinstance(ids, str):
        ids = [x for x in re.split(r"[,\s]+", ids) if x]
    return (_session_env("HERMES_SESSION_PLATFORM").lower() == "slack"
            and _session_env("HERMES_SESSION_CHAT_TYPE").lower() in ("dm", "im")
            and _session_env("HERMES_SESSION_CHAT_ID").startswith("D")
            and _session_env("HERMES_SESSION_USER_ID") in {str(i) for i in ids})


def owner_refusal(session_id: str | None) -> str | None:
    """None when the caller is the owner's own session; else the refusal text. Default-deny."""
    surfaces = caller_surfaces(session_id)
    others = [x for x in surfaces if x not in OWNER_SURFACES]
    if surfaces and not others:
        return None
    where = ", ".join(others) or "an unknown surface"
    if _owner_slack_dm():
        return (f"run_in_user_terminal is refused on Slack, even in the owner's own DM: it needs a healthy Hermes on "
                f"the owner's Mac. This IS the owner's DM, so do not tell them it needs the owner. If they want a "
                f"machine looked at or repaired (display, mouse, frozen app, reboot), call mac_ops_handoff with their "
                f"request now: the owner-only machine agent diagnoses, repairs and posts a screenshot in this thread. "
                f"Say in one line that you handed it over. Never hand over commands to paste, keyboard shortcuts or "
                f"other do-it-yourself steps. Any other command: say it needs their Desktop/CLI chat.")
    return (f"run_in_user_terminal is refused here: it opens a confirm window on the owner's Mac and is only "
            f"available in the owner's own chats (Desktop, CLI, TUI, API). This session is on {where}, so the "
            f"person asking may not be the owner. Do not run the command another way and do not hand it over "
            f"as something to paste: tell them it needs the owner to ask for it from their own Hermes chat.")


def _root() -> Path:
    base = _home()
    if base.parent.name == "profiles":  # profiles share the gateway root's courier outbox
        base = base.parent.parent
    path = base / "session_mentions"
    for sub in ("outbox", "claimed", "results"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return path


def _slug(raw: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(raw or "").strip().lower()).strip("-")


def live_couriers(base: Path) -> list[dict]:
    out = []
    d = base / "couriers"
    for path in sorted(d.glob("*.json")) if d.is_dir() else ():
        try:
            if time.time() - path.stat().st_mtime < COURIER_FRESH_S:
                out.append(json.loads(path.read_text()))
        except Exception:
            continue
    return out


def configured_device() -> str:
    dev = os.environ.get("HERMES_USER_TERMINAL_DEVICE", "")
    try:
        from hermes_cli.config import load_config  # type: ignore

        dev = str(((load_config() or {}).get("user_terminal") or {}).get("device") or dev)
    except Exception:
        pass
    return _slug(dev)


def pick_courier(base: Path) -> tuple[str, str]:
    """(device, error). The configured Mac must have a live courier; unconfigured -> the only live one."""
    couriers = [c for c in live_couriers(base) if c.get("device")]
    seen = ", ".join(sorted(c["device"] for c in couriers)) or "none"
    want = configured_device()
    if want:
        for c in couriers:
            if want == c.get("device") or want in (c.get("aliases") or []):
                return str(c["device"]), ""
        return "", (f"The owner's Mac (`{want}`) has no Hermes Desktop connected to this gateway right now "
                    f"(live Desktops: {seen}), so no terminal can be opened there. Tell the owner that; do not "
                    f"hand them the command to run themselves.")
    if len(couriers) == 1:
        return str(couriers[0]["device"]), ""
    if not couriers:
        return "", ("No Hermes Desktop is connected to this gateway right now, so no terminal can be opened on "
                    "the owner's Mac. Tell the owner that; do not hand them the command to run themselves.")
    return "", (f"Several Desktops drain this gateway ({seen}); set `user_terminal.device` in this gateway's "
                f"config to the owner's Mac.")


def _sender(session_id: str | None) -> dict:
    info: dict[str, Any] = {"session_id": session_id or "", "title": ""}
    if session_id:
        try:
            from hermes_state import SessionDB  # type: ignore

            info["title"] = SessionDB().get_session_title(session_id) or ""
        except Exception:
            pass
    try:
        from hermes_cli.profiles import get_active_profile_name  # type: ignore

        info["profile"] = get_active_profile_name() or "default"
    except Exception:
        info["profile"] = "default"
    info["platform"] = ",".join(caller_surfaces(session_id))
    return info


def relay_once(base: Path, device: str, args: dict, sender: dict, result_wait: float) -> dict:
    """One request to the Mac's courier. Errors (never a hang) when nobody claims it within PICKUP_S."""
    rid = uuid.uuid4().hex
    outbox, result = base / "outbox" / f"{rid}.json", base / "results" / f"{rid}.json"
    req = {"id": rid, "action": "user_terminal", "route": device, "args": args, "sender": sender,
           "created_at": time.time(), "ttl": PICKUP_S + 5}
    tmp = outbox.with_name(f".{rid}.tmp")
    tmp.write_text(json.dumps(req))
    os.replace(tmp, outbox)
    pickup = time.time() + PICKUP_S
    deadline = pickup + result_wait
    while time.time() < deadline:
        if result.exists():
            try:
                data = json.loads(result.read_text())
            except ValueError:
                time.sleep(0.2)
                continue
            result.unlink(missing_ok=True)
            return data if isinstance(data, dict) else {"ok": False, "error": "courier posted no result"}
        if outbox.exists() and time.time() > pickup:
            outbox.unlink(missing_ok=True)
            return {"ok": False, "error": (f"The Hermes Desktop on `{device}` did not pick up the request (it may "
                                           f"be quitting, asleep, or running an older session-mentions plugin). "
                                           f"Tell the owner; do not hand them the command.")}
        if _interrupted():
            return {"ok": False, "error": "interrupted"}
        time.sleep(0.3)
    return {"ok": False, "error": f"`{device}` claimed the request but sent no answer in time"}


def handle_remote(args: dict, session_id: str | None = None, **_kw: Any) -> str:
    try:
        refusal = owner_refusal(session_id)
        if refusal:
            return _err(refusal)
        wait_s = max(1.0, min(float(args.get("wait_s") or DEFAULT_WAIT), MAX_WAIT))
        job_id = str(args.get("job_id") or "").strip()
        command = str(args.get("command") or "").strip()
        if not job_id and not command:
            return _err("command is required (or job_id to resume waiting)")
        base = _root()
        device, err = pick_courier(base)
        if err:
            return _err(err)
        sender = _sender(session_id)
        deadline = time.time() + wait_s
        if not job_id:
            res = relay_once(base, device, {"command": command, "reason": str(args.get("reason") or ""),
                                            "cwd": str(args.get("cwd") or "")}, sender, START_RESULT_S)
            if not res.get("ok") or not res.get("job_id"):
                return json.dumps({**res, "ok": False, "device": device})
            job_id, terminal = str(res["job_id"]), res.get("terminal")
        else:
            terminal = None
        res: dict = {}
        while True:
            left = deadline - time.time()
            res = relay_once(base, device, {"job_id": job_id, "wait_s": int(max(0, min(POLL_S, left)))}, sender,
                             POLL_S + 30)
            if not res.get("ok") or res.get("status") in ("ran", "declined") or time.time() >= deadline:
                break
            if _interrupted():
                break
        res = {**res, "job_id": job_id, "device": device}
        if terminal:
            res["terminal"] = terminal
        if res.get("ok") and res.get("status") not in ("ran", "declined"):
            res["note"] = (f"Still waiting on `{device}` (the owner hasn't answered, or the command is still "
                           f"running). Call run_in_user_terminal with this job_id to keep waiting.")
        return json.dumps(res)
    except Exception as e:  # never crash the agent loop
        return _err(f"{type(e).__name__}: {e}")


REMOTE_SCHEMA = {**SCHEMA, "description": SCHEMA["description"] + (
    " On this gateway (no screen) the window opens on the owner's Mac through its Hermes Desktop; the "
    "window names this gateway, profile and session. If it errors that no Desktop is connected, say so; "
    "never hand the command over to be pasted instead.")}


def has_screen() -> bool:
    return shutil.which("osascript") is not None or CMUX_BIN.exists()


def register(ctx) -> None:
    if not has_screen():  # Linux/cloud gateway: relay to the owner's Mac (see module docstring)
        ctx.register_tool(name="run_in_user_terminal", toolset=TOOLSET, schema=REMOTE_SCHEMA, handler=handle_remote,
                          emoji="🖥️", description="Open a terminal on the owner's Mac (via its Desktop), confirm there, run, return output")
        ctx.register_hook("transform_terminal_output", transform_terminal_output)
        return
    ctx.register_tool(name="run_in_user_terminal", toolset=TOOLSET, schema=SCHEMA, handler=handle, emoji="🖥️",
                      description="Open a terminal on the user's screen, confirm there, run, return output")
    ctx.register_hook("transform_terminal_output", transform_terminal_output)
