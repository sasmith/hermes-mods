"""Tests for user-terminal: the runner script under a real pty (no window opened).

Run: uv run --with pytest python -m pytest -q -p no:cacheprovider tests_user_terminal.py
"""

import importlib.util
import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("user_terminal", Path(__file__).with_name("__init__.py"))
ut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ut)


def _drive(job: Path, answers: list[str], timeout: float = 20) -> bytes:
    """Run runner.sh in a pty, typing each answer once its prompt shows up. With a confirm file the first
    answer goes to ``Run it?``; without one every answer goes to ``Press Enter``."""
    asks = (job / "confirm").exists()
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["SHELL"] = "/bin/bash"
        os.execv("/bin/bash", ["/bin/bash", str(job / "runner.sh"), str(job)])
    buf, deadline, pending = b"", time.time() + timeout, list(answers)
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        if pending and (b"Run it?" in buf if asks and len(pending) == len(answers) else b"Press Enter" in buf):
            os.write(fd, pending.pop(0).encode())
    os.waitpid(pid, 0)
    return buf


@pytest.fixture(autouse=True)
def _confirm_env(monkeypatch):
    monkeypatch.delenv("HERMES_USER_TERMINAL_CONFIRM", raising=False)


def test_default_runs_without_asking(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_USER_TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(ut, "confirm_mode", lambda: "off")
    job = ut.create_job("echo hello-from-job; exit 3", "testing", str(tmp_path))
    assert not (job / "confirm").exists()
    screen = _drive(job, ["\n"])
    assert b"Run it?" not in screen and b"Ctrl-C cancels" in screen
    res = ut.wait_for(job, 1)
    assert res["status"] == "ran" and res["exit_code"] == 3
    assert "hello-from-job" in res["output"]


@pytest.mark.parametrize("env,mode", [("", "off"), ("off", "off"), ("remote", "remote"), ("all", "all"),
                                      ("1", "all"), ("bogus", "off")])
def test_confirm_mode_env(monkeypatch, env, mode):
    monkeypatch.setenv("HERMES_USER_TERMINAL_CONFIRM", env)
    monkeypatch.setitem(sys.modules, "hermes_cli", None)  # no config -> env only
    assert ut.confirm_mode() == mode


@pytest.mark.parametrize("mode,origin,asks", [("off", None, False), ("off", {"gateway": "g"}, False),
                                              ("remote", None, False), ("remote", {"gateway": "g"}, True),
                                              ("all", None, True)])
def test_confirm_file_follows_mode(tmp_path, monkeypatch, mode, origin, asks):
    monkeypatch.setenv("HERMES_USER_TERMINAL_DIR", str(tmp_path))
    monkeypatch.setattr(ut, "confirm_mode", lambda: mode)
    assert (ut.create_job("true", "", str(tmp_path), origin) / "confirm").exists() is asks


def test_confirm_runs_and_captures(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_USER_TERMINAL_DIR", str(tmp_path))
    job = ut.create_job("echo hello-from-job; exit 3", "testing", str(tmp_path), confirm=True)
    _drive(job, ["y\n", "\n"])
    res = ut.wait_for(job, 1)
    assert res["status"] == "ran" and res["exit_code"] == 3
    assert "hello-from-job" in res["output"]


def test_decline(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_USER_TERMINAL_DIR", str(tmp_path))
    job = ut.create_job("touch SHOULD_NOT_EXIST", "", str(tmp_path), confirm=True)
    _drive(job, ["n\n"])
    res = ut.wait_for(job, 1)
    assert res["status"] == "declined"
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_pending_then_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_USER_TERMINAL_DIR", str(tmp_path))
    job = ut.create_job("true", "", str(tmp_path))
    res = ut.wait_for(job, 1)
    assert res["status"] == "pending" and res["job_id"] == job.name
    (job / "status.json").write_text(json.dumps({"status": "ran", "exit_code": 0}))
    out = json.loads(ut.handle({"job_id": job.name, "wait_s": 1}))
    assert out["status"] == "ran"


def test_clean_output_strips_ansi():
    assert ut.clean_output("\x1b[31mred\x1b[0m\r\nprogress 10%\rprogress 100%\n") == "red\nprogress 100%"


def test_terminal_order_prefers_iterm(monkeypatch):
    monkeypatch.delenv("HERMES_USER_TERMINAL", raising=False)
    monkeypatch.setattr(ut, "_has_iterm", lambda: True)
    assert ut.terminal_order()[:2] == ["iterm", "terminal"]
    monkeypatch.setattr(ut, "_has_iterm", lambda: False)
    assert ut.terminal_order()[0] == "terminal"
    monkeypatch.setenv("HERMES_USER_TERMINAL", "cmux")
    if ut.CMUX_BIN.exists():
        assert ut.terminal_order()[0] == "cmux"


def test_sudo_failure_hint_replaces_core_tip():
    out = "x\nsudo: a password is required\n\n\n💡 Tip: To enable sudo over messaging, add SUDO_PASSWORD to ~/.hermes/.env on the agent machine."
    new = ut.transform_terminal_output(command="sudo ls", output=out, returncode=1)
    assert "SUDO_PASSWORD to" not in new and "run_in_user_terminal" in new
    assert new.startswith("x\nsudo: a password is required")
    assert ut.transform_terminal_output(command="sudo ls", output=new) is None  # idempotent
    assert ut.transform_terminal_output(command="ls", output="ok") is None


# ---------------------------------------------------------------- remote mode (relay to the owner's Mac)

import threading  # noqa: E402

_rspec = importlib.util.spec_from_file_location(
    "sm_relay_files", Path(__file__).resolve().parent.parent / "session-mentions" / "relay_files.py")
rf = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(rf)

ORIGIN = {"gateway": "nous-1", "profile": "alpha", "session_id": "20261021_000505_e1e78133", "title": "Token revoke"}


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A screenless gateway home (tmp/gw) and the Mac's job dir (tmp/mac); session identity from env only."""
    gw, mac = tmp_path / "gw", tmp_path / "mac"
    gw.mkdir()
    mac.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(gw))
    monkeypatch.setenv("HERMES_USER_TERMINAL_DIR", str(mac))
    for k in ("HERMES_PLATFORM", "HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE", "HERMES_USER_TERMINAL_DEVICE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ut, "_home", lambda: gw)
    monkeypatch.setattr(ut, "configured_device", lambda: ut._slug(os.environ.get("HERMES_USER_TERMINAL_DEVICE", "")))
    return ut._root(), mac


def _beat(base: Path, device: str, aliases=()) -> None:
    rf.register_courier(base, device, list(aliases) or [device], ["nous-1"], home=False)


def _outbox(base: Path) -> list:
    return list((base / "outbox").glob("*.json")) if (base / "outbox").is_dir() else []


# Invariant 1 (confirm=remote|all): nothing relayed runs without the owner typing y in the Mac window.
@pytest.mark.parametrize("answer", ["\n", "n\n", "no\n"])
def test_relayed_job_runs_only_after_y(remote, monkeypatch, tmp_path, answer):
    monkeypatch.setattr(ut, "open_window", lambda job: "fake")
    monkeypatch.setattr(ut, "confirm_mode", lambda: "remote")
    marker = tmp_path / "RAN"
    res = ut.start_relayed(f"touch {marker}", "why", str(tmp_path), ORIGIN)
    assert res["ok"] and res["status"] == "waiting_for_user"
    job = ut.jobs_dir() / res["job_id"]
    time.sleep(0.3)
    assert not marker.exists() and ut.read_status(job)["status"] == "waiting_for_user"
    _drive(job, [answer])
    assert ut.relayed_status(res["job_id"], "nous-1", 1)["status"] == "declined"
    assert not marker.exists()


def test_relayed_start_requires_origin_and_never_runs_on_open(remote, monkeypatch, tmp_path):
    monkeypatch.setattr(ut, "open_window", lambda job: "fake")
    assert not ut.start_relayed("true", "", "", None)["ok"]
    assert not ut.start_relayed("true", "", "", {"profile": "x"})["ok"]
    assert "cwd does not exist" in ut.start_relayed("true", "", "/nope/nope", ORIGIN)["error"]


# Invariant 2: only the owner's own sessions may call it; messaging/unknown surfaces are refused.
@pytest.mark.parametrize("env,ok", [
    ({"HERMES_SESSION_PLATFORM": "slack"}, False),
    ({"HERMES_SESSION_SOURCE": "desktop", "HERMES_SESSION_PLATFORM": "slack"}, False),
    ({"HERMES_SESSION_SOURCE": "cron"}, False),
    ({}, False),  # nothing known about the caller: default-deny
    ({"HERMES_SESSION_SOURCE": "desktop"}, True),
    ({"HERMES_SESSION_SOURCE": "tui"}, True),
    ({"HERMES_SESSION_PLATFORM": "api_server"}, True),
    ({"HERMES_SESSION_SOURCE": "cli"}, True),
])
def test_owner_gate(remote, monkeypatch, env, ok):
    base, _ = remote
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    out = json.loads(ut.handle_remote({"command": "echo ok", "wait_s": 1}))
    assert out["ok"] is False
    if ok:
        assert "refused" not in out["error"]  # passed the gate; failed only on "no Desktop connected"
    else:
        assert "refused" in out["error"] and "owner" in out["error"] and "paste" in out["error"]
        assert not _outbox(base)  # nothing was sent toward the Mac


def test_owner_gate_reads_session_row_source(remote, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
    monkeypatch.setattr(ut, "caller_surfaces", lambda sid: ["desktop", "slack"] if sid == "s-slack" else ["desktop"])
    assert ut.owner_refusal("s-slack") and ut.owner_refusal("s-desk") is None


def test_owner_dm_refusal_routes_to_ops_not_paste_it(remote, monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "hermes_cli.config", types.SimpleNamespace(
        load_config=lambda: {"owner": {"slack_user_ids": ["U0OWNER"]}}))
    base = {"HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_CHAT_TYPE": "dm", "HERMES_SESSION_CHAT_ID": "D1",
            "HERMES_SESSION_USER_ID": "U0OWNER"}
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    out = json.loads(ut.handle_remote({"command": "echo ok", "wait_s": 1}))
    assert out["ok"] is False and "mac_ops_handoff" in out["error"] and "Never hand over commands" in out["error"]
    assert not _outbox(remote[0])  # still refused: nothing sent toward the Mac
    # a channel, or someone else's DM, keeps the old text (no handoff offer)
    for over in ({"HERMES_SESSION_CHAT_TYPE": "channel", "HERMES_SESSION_CHAT_ID": "C1"},
                 {"HERMES_SESSION_USER_ID": "U0STRANGER"}):
        for k, v in {**base, **over}.items():
            monkeypatch.setenv(k, v)
        out = json.loads(ut.handle_remote({"command": "echo ok", "wait_s": 1}))
        assert "mac_ops_handoff" not in out["error"] and "refused" in out["error"]


# Invariant 3: the window names the originating gateway, profile and session; control chars can't hide text.
def test_window_shows_origin_and_neutralises_escapes(remote, monkeypatch, tmp_path):
    monkeypatch.setattr(ut, "open_window", lambda job: "fake")
    monkeypatch.setattr(ut, "confirm_mode", lambda: "all")  # decline so the escape-laden command never runs
    res = ut.start_relayed("echo visible\x1b[2K\rhidden", "reason\x1b]0;x\x07", str(tmp_path),
                           {**ORIGIN, "title": "evil\x1b[31m"})
    job = ut.jobs_dir() / res["job_id"]
    screen = _drive(job, ["n\n"]).decode(errors="replace")
    assert "From another machine" in screen
    assert "Gateway nous-1 · profile alpha · session 20261021_000505_e1e78133" in screen
    assert "^[[2K^Mhidden" in screen and "^[]0;x^G" in screen and "evil^[[31m" in screen
    assert "Hermes (nous-1/alpha) wants to run a command" in screen  # window title
    assert (job / "cmd.sh").read_bytes() == b"echo visible\x1b[2K\rhidden\n"  # runs verbatim, shown visibly


# Invariant 4: no Desktop courier online -> an immediate error, never a silent hang.
def test_no_courier_errors_immediately(remote, monkeypatch):
    base, _ = remote
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
    t = time.time()
    out = json.loads(ut.handle_remote({"command": "echo ok", "wait_s": 600}))
    assert time.time() - t < 2 and out["ok"] is False and "No Hermes Desktop" in out["error"]
    assert "do not hand them the command" in out["error"] and not _outbox(base)


def test_configured_mac_offline_or_stale_errors_immediately(remote, monkeypatch):
    base, _ = remote
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
    monkeypatch.setenv("HERMES_USER_TERMINAL_DEVICE", "macbook")
    _beat(base, "mac-mini")
    _beat(base, "macbook", ["macbook", "david-mbp"])
    old = time.time() - 120
    os.utime(base / "couriers" / "macbook.json", (old, old))
    t = time.time()
    out = json.loads(ut.handle_remote({"command": "echo ok", "wait_s": 600}))
    assert time.time() - t < 2 and "`macbook`" in out["error"] and "mac-mini" in out["error"]
    assert not _outbox(base)


def test_courier_alive_but_never_claims_errors_fast(remote, monkeypatch):
    base, _ = remote
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
    monkeypatch.setattr(ut, "PICKUP_S", 0.6)
    _beat(base, "macbook")
    t = time.time()
    out = json.loads(ut.handle_remote({"command": "echo ok", "wait_s": 600}))
    assert time.time() - t < 3 and "did not pick up" in out["error"] and not _outbox(base)


def test_several_couriers_need_a_configured_device(remote, monkeypatch):
    base, _ = remote
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
    _beat(base, "macbook")
    _beat(base, "mac-mini")
    assert "user_terminal.device" in json.loads(ut.handle_remote({"command": "x"}))["error"]


def test_status_only_for_the_origin_gateway(remote, monkeypatch, tmp_path):
    monkeypatch.setattr(ut, "open_window", lambda job: "fake")
    jid = ut.start_relayed("true", "", str(tmp_path), ORIGIN)["job_id"]
    assert ut.relayed_status(jid, "nous-1", 0)["status"] == "pending"
    assert ut.relayed_status(jid, "hermes-hub", 0)["ok"] is False
    assert ut.relayed_status(jid, "", 0)["ok"] is False
    local = ut.create_job("true", "", str(tmp_path))  # a local (non-relayed) job is never readable remotely
    assert ut.relayed_status(local.name, "nous-1", 0)["ok"] is False


# Round trip: remote tool -> outbox -> fake Desktop courier (real claim rule) -> Mac-side local path -> result.
class FakeCourier(threading.Thread):
    """Plays a Mac's Desktop courier: claims with relay_files' real rule, runs the request through the
    user-terminal Mac routes' functions (start_relayed / relayed_status) and posts the result."""

    def __init__(self, base: Path, device: str, gateway_key: str = "nous-1", aliases=()):
        super().__init__(daemon=True)
        self.base, self.device, self.gw, self.aliases = base, device, gateway_key, list(aliases) or [device]
        self.stop, self.claimed = threading.Event(), []

    def run(self):
        while not self.stop.is_set():
            for req in rf.claim_pending(self.base, home=False, courier=self.device, reach=["nous-1"], aliases=self.aliases):
                self.claimed.append(req)
                a = req["args"]
                if a.get("job_id"):
                    res = ut.relayed_status(a["job_id"], self.gw, a.get("wait_s") or 0)
                else:
                    origin = {"gateway": self.gw, "profile": req["sender"].get("profile"),
                              "session_id": req["sender"].get("session_id"), "title": req["sender"].get("title")}
                    res = ut.start_relayed(a["command"], a.get("reason"), a.get("cwd"), origin)
                rf.write_result(req["id"], {**res, "device": self.device}, self.base)
            time.sleep(0.1)


def test_round_trip_remote_to_mac_and_back(remote, monkeypatch, tmp_path):
    base, mac = remote
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
    monkeypatch.setenv("HERMES_USER_TERMINAL_DEVICE", "david-mbp")  # an alias resolves to the device
    monkeypatch.setattr(ut, "POLL_S", 1)
    screens = []

    def fake_window(job):  # default confirm=off: it runs at once, the owner only closes the window
        threading.Thread(target=lambda: screens.append(_drive(job, ["\n"])), daemon=True).start()
        return "fake"

    monkeypatch.setattr(ut, "open_window", fake_window)
    mbp, mini = FakeCourier(base, "macbook", aliases=["macbook", "david-mbp"]), FakeCourier(base, "mac-mini")
    _beat(base, "macbook", ["macbook", "david-mbp"])
    _beat(base, "mac-mini")
    mbp.start()
    mini.start()
    try:
        out = json.loads(ut.handle_remote({"command": "echo ok-$((40+2))", "reason": "test", "cwd": str(tmp_path),
                                           "wait_s": 20}, session_id="s1"))
    finally:
        mbp.stop.set()
        mini.stop.set()
    assert out["ok"] and out["status"] == "ran" and out["exit_code"] == 0, out
    assert "ok-42" in out["output"] and out["device"] == "macbook" and out["terminal"] == "fake"
    assert mbp.claimed and not mini.claimed  # routed to the named Mac only
    assert mbp.claimed[0]["action"] == "user_terminal" and mbp.claimed[0]["route"] == "macbook"
    assert "Gateway nous-1 · profile" in screens[0].decode(errors="replace")


def test_round_trip_pending_then_resume_with_job_id(remote, monkeypatch, tmp_path):
    base, _ = remote
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
    monkeypatch.setattr(ut, "POLL_S", 1)
    monkeypatch.setattr(ut, "open_window", lambda job: "fake")
    c = FakeCourier(base, "macbook")
    _beat(base, "macbook")
    c.start()
    try:
        first = json.loads(ut.handle_remote({"command": "true", "cwd": str(tmp_path), "wait_s": 1}))
        assert first["ok"] and first["status"] == "pending" and "job_id" in first["note"]
        job = ut.jobs_dir() / first["job_id"]
        (job / "status.json").write_text(json.dumps({"status": "ran", "exit_code": 0}))
        again = json.loads(ut.handle_remote({"job_id": first["job_id"], "wait_s": 5}))
    finally:
        c.stop.set()
    assert again["ok"] and again["status"] == "ran" and again["job_id"] == first["job_id"]


def test_register_picks_remote_handler_without_a_screen(monkeypatch):
    got = {}

    class Ctx:
        def register_tool(self, **kw):
            got.update(kw)

        def register_hook(self, *a):
            pass

    monkeypatch.setattr(ut, "has_screen", lambda: False)
    ut.register(Ctx())
    assert got["handler"] is ut.handle_remote and got["schema"]["parameters"] == ut.SCHEMA["parameters"]
    monkeypatch.setattr(ut, "has_screen", lambda: True)
    ut.register(Ctx())
    assert got["handler"] is ut.handle


def test_dashboard_routes_start_and_status(remote, monkeypatch, tmp_path):
    """The Mac-side REST routes the courier calls (dashboard/plugin_api.py), end to end over HTTP."""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location("ut_api", Path(__file__).with_name("dashboard") / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    mod = api._ut()  # the route's own fresh copy of the plugin module
    monkeypatch.setattr(mod, "open_window", lambda job: "fake")
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/user-terminal")
    c = TestClient(app)
    assert c.get("/api/plugins/user-terminal/ping").json()["relay"] is True
    r = c.post("/api/plugins/user-terminal/start", json={"command": "echo hi", "reason": "r", "cwd": str(tmp_path),
                                                       "origin": ORIGIN}).json()
    assert r["ok"] and r["status"] == "waiting_for_user" and r["terminal"] == "fake"
    st = c.get(f"/api/plugins/user-terminal/status/{r['job_id']}?gateway=nous-1&wait_s=0").json()
    assert st["status"] == "pending"
    assert c.get(f"/api/plugins/user-terminal/status/{r['job_id']}?gateway=other").json()["ok"] is False
    assert c.post("/api/plugins/user-terminal/start", json={"command": "x"}).json()["ok"] is False  # no origin


@pytest.mark.parametrize("out", [
    "daveey@desk.local: Permission denied (publickey,password,keyboard-interactive).",
    "Permission denied (publickey,keyboard-interactive).",
    # nested: laptop -> hop -> host, the inner ssh's failure arrives on its own line
    "Warning: Permanently added 'desk.local' (ED25519) to the list of known hosts.\r\n"
    "you@desk.local: Permission denied (publickey,password,keyboard-interactive).\nexit 255",
    "Enter passphrase for key '/Users/you/.ssh/id_ed25519':",
    "you@desk.local's password: ",
    "/usr/bin/ssh-copy-id: ERROR: Permission denied (publickey).",
])
def test_ssh_password_auth_gets_ssh_copy_id_hint(out):
    new = ut.transform_terminal_output(command="ssh -o BatchMode=yes desk.local true", output=out)
    assert new and "run_in_user_terminal" in new and "ssh-copy-id -i ~/.ssh/id_ed25519.pub" in new
    assert "hand it back" in new
    assert ut.transform_terminal_output(command="ssh x", output=new) is None  # idempotent


@pytest.mark.parametrize("out", [
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "security: SecKeychainUnlock: User interaction is not allowed.",
])
def test_git_https_and_keychain_get_hint(out):
    new = ut.transform_terminal_output(command="git pull", output=out)
    assert new and "run_in_user_terminal" in new and "ssh-copy-id" not in new


@pytest.mark.parametrize("out", [
    "Permission denied (publickey).",                         # key-only server: a password can't help
    "73 sudo: a password is required",                        # a log/grep that mentions it, not sudo itself
    "grep: Permission denied (publickey,password) appears in auth.log",
    "ok",
])
def test_no_hint_for_mentions_or_key_only_servers(out):
    assert ut.transform_terminal_output(command="x", output=out) is None
