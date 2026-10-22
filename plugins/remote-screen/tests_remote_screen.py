"""Tests for remote-screen: RFB client against an in-process fake Apple VNC server, and the dialog-approval
decision logic. Run: uv run --with pytest --with cryptography --with pillow python -m pytest -q -p no:cacheprovider tests_remote_screen.py"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import struct
import sys
import threading
import zlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("remote_screen_plugin", HERE / "__init__.py")
rs = importlib.util.module_from_spec(_spec)
sys.modules["remote_screen_plugin"] = rs
_spec.loader.exec_module(rs)
rfb = rs.rfb

PRIME = bytes.fromhex("ffffffffffffffffc90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b139b22514a08798e3404dd"
                      "ef9519b3cd3a431b302b0a6df25f14374fe1356d6d51c245e485b576625e7ec6f44c42e9a637ed6b0bff5cb6f406b7ed"
                      "ee386bfb5a899fa5ae9f24117c4b1fe649286651ece65381ffffffffffffffff")  # RFC 2409 group 2


class FakeServer:
    """Apple-flavoured RFB server: type 30 auth, 8x4 framebuffer, records pointer/key events."""

    def __init__(self, user="alice", password="s3cret", w=8, h=4, zlib_enc=True, partial=False):
        self.user, self.password, self.w, self.h, self.zlib_enc = user, password, w, h, zlib_enc
        self.partial = partial  # multi-display Mac: only the "display" rect (top half) is ever sent
        self.events: list[tuple] = []
        self.got_creds: tuple[str, str] | None = None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.t = threading.Thread(target=self.serve, daemon=True)
        self.t.start()

    def serve(self):
        c, _ = self.sock.accept()
        f = c.makefile("rb")
        rd = lambda n: f.read(n)  # noqa: E731
        c.sendall(b"RFB 003.889\n")
        assert rd(12) == b"RFB 003.889\n"
        c.sendall(bytes([4, 30, 33, 36, 35]))
        assert rd(1) == b"\x1e"
        priv = 0x1234567
        p = int.from_bytes(PRIME, "big")
        c.sendall(struct.pack(">HH", 2, len(PRIME)) + PRIME + pow(2, priv, p).to_bytes(len(PRIME), "big"))
        ct, pub = rd(128), rd(len(PRIME))
        key = hashlib.md5(pow(int.from_bytes(pub, "big"), priv, p).to_bytes(len(PRIME), "big")).digest()
        dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        pt = dec.update(ct) + dec.finalize()
        user, pw = pt[:64].split(b"\0")[0].decode(), pt[64:].split(b"\0")[0].decode()
        self.got_creds = (user, pw)
        if (user, pw) != (self.user, self.password):
            msg = b"bad login"
            c.sendall(struct.pack(">II", 1, len(msg)) + msg)
            c.close()
            return
        c.sendall(struct.pack(">I", 0))
        assert rd(1) == b"\x01"
        c.sendall(struct.pack(">HH", self.w, self.h) + b"\0" * 16 + struct.pack(">I", 4) + b"fake")
        z = zlib.compressobj()
        while True:
            t = rd(1)
            if not t:
                return
            t = t[0]
            if t == 0:
                rd(19)
            elif t == 2:
                n = struct.unpack(">xH", rd(3))[0]
                rd(4 * n)
            elif t == 3:
                rd(9)
                # two rects: top half red, bottom half blue (BGRX)
                half = self.h // 2
                top = bytes([0, 0, 255, 0]) * (self.w * half)
                bot = bytes([255, 0, 0, 0]) * (self.w * (self.h - half))
                c.sendall(bytes([3, 0, 0, 0]) + struct.pack(">I", 1) + b"x")  # cut text first, must be skipped
                out = struct.pack(">BxH", 0, 1 if self.partial else 2)
                out += struct.pack(">HHHHi", 0, 0, self.w, half, 0) + top
                if self.partial:
                    pass
                elif self.zlib_enc:
                    data = z.compress(bot) + z.flush(zlib.Z_SYNC_FLUSH)
                    out += struct.pack(">HHHHiI", 0, half, self.w, self.h - half, 6, len(data)) + data
                else:
                    out += struct.pack(">HHHHi", 0, half, self.w, self.h - half, 0) + bot
                c.sendall(out)
            elif t == 4:
                down, sym = struct.unpack(">BxxI", rd(7))
                self.events.append(("key", down, sym))
            elif t == 5:
                mask, x, y = struct.unpack(">BHH", rd(5))
                self.events.append(("ptr", mask, x, y))


@pytest.mark.parametrize("zlib_enc", [True, False])
def test_login_screenshot_and_input(zlib_enc):
    srv = FakeServer(zlib_enc=zlib_enc)
    with rfb.RFBClient("127.0.0.1", srv.port, user="alice", password="s3cret") as c:
        assert c.security_type == 30 and (c.width, c.height) == (8, 4) and c.name == "fake"
        img = c.screenshot().to_image()
        assert img.getpixel((0, 0)) == (255, 0, 0) and img.getpixel((7, 3)) == (0, 0, 255)
        c.click(3, 2)
        c.press("cmd+a")
        c.type_text("Hi")
        import time
        time.sleep(0.2)
    ptr = [e for e in srv.events if e[0] == "ptr"]
    assert ptr[0] == ("ptr", 0, 3, 2) and ("ptr", 1, 3, 2) in ptr and ptr[-1] == ("ptr", 0, 3, 2)
    keys = [e for e in srv.events if e[0] == "key"]
    assert keys[:4] == [("key", 1, 0xFFE7), ("key", 1, ord("a")), ("key", 0, ord("a")), ("key", 0, 0xFFE7)]
    assert ("key", 1, 0xFFE1) in keys and ("key", 1, ord("H")) in keys and ("key", 1, ord("i")) in keys
    assert srv.got_creds == ("alice", "s3cret")


def test_screenshot_returns_partial_multi_display_frame(monkeypatch):
    # A Mac with two displays reports their bounding box but never sends the dead corners; the client
    # must return the frame once the server goes quiet instead of waiting out the timeout.
    import time
    monkeypatch.setattr(rfb.RFBClient, "UPDATE_GRACE_S", 0.3)
    srv = FakeServer(partial=True)
    with rfb.RFBClient("127.0.0.1", srv.port, user="alice", password="s3cret", timeout=5) as c:
        t = time.time()
        img = c.screenshot().to_image()
        assert time.time() - t < 3
    assert img.getpixel((0, 0)) == (255, 0, 0) and img.getpixel((7, 3)) == (0, 0, 0)


def test_bad_password_raises():
    srv = FakeServer()
    with pytest.raises(rfb.AuthError, match="bad login"):
        rfb.RFBClient("127.0.0.1", srv.port, user="alice", password="nope").connect()


def test_parse_combo():
    assert rfb.parse_combo("cmd+shift+4") == [0xFFE7, 0xFFE1, ord("4")]
    assert rfb.parse_combo("return") == [0xFF0D]
    assert rfb.parse_combo("A") == [ord("A")]
    with pytest.raises(ValueError):
        rfb.parse_combo("cmd+bogus")


def L(text, x, y, w=100, h=16):
    return {"text": text, "x": x, "y": y, "w": w, "h": h, "conf": 1.0}


APPS = ["Hermes Python", "ghostty"]
TCC = [L("“Hermes Python.app” would like to access", 600, 300, 300), L("your Photos.", 650, 320),
       L("Don't Allow", 560, 420), L("Allow", 760, 420, 60), L("Allow", 100, 900, 50)]  # far-away Allow ignored


def test_find_dialog_approves_allowlisted():
    d = rs.find_dialog(TCC, APPS)
    assert d["status"] == "approve" and d["app"] == "Hermes Python"
    assert (d["button"]["x"], d["button"]["y"]) == (760, 420)


def test_find_dialog_rejects_unlisted_app():
    lines = [L("“Sketchy.app” would like to access your", 600, 300, 300), L("Allow", 760, 420)]
    d = rs.find_dialog(lines, APPS)
    assert d["status"] == "blocked" and d["reason"] == "app_not_allowlisted" and d["requester"] == "Sketchy.app"


def test_background_text_never_counts_as_requester():
    # An allowlisted name visible behind/next to the dialog (another window) must not approve it.
    lines = [L("HERMES PYTHON", 500, 250, 700, 60), L("ghostty docs", 900, 310),
             L("“Sketchy” wants access to control", 600, 300, 300), L("“Finder”.", 600, 318),
             L("Don't Allow", 560, 420), L("Allow", 760, 420, 60)]
    d = rs.find_dialog(lines, APPS)
    assert d["status"] == "blocked" and d["requester"] == "Sketchy"


def test_wrapped_app_name_uses_line_above():
    lines = [L("“Hermes", 600, 282, 80), L("Python” would like to access", 600, 300, 300),
             L("Allow", 760, 420, 60)]
    d = rs.find_dialog(lines, APPS)
    assert d["status"] == "approve" and d["requester"] == "Hermes Python"


def test_find_dialog_refuses_password_prompt():
    lines = [L("ghostty wants to make changes.", 600, 300, 300), L("Enter your password to allow this.", 600, 320),
             L("Password", 560, 360), L("OK", 760, 420)]
    assert rs.find_dialog(lines, APPS)["reason"] == "password"


def test_find_dialog_none_and_no_button():
    assert rs.find_dialog([L("Finder", 0, 0), L("Allow", 10, 10)], APPS) == {"status": "none"}
    d = rs.find_dialog([L("ghostty would like to access files", 600, 300, 300)], APPS)
    assert d["reason"] == "no_approve_button"


def test_find_text_prefers_exact():
    hits = rs.find_text(TCC, "Allow")
    assert hits[0]["text"] == "Allow"
    assert rs.find_text(TCC, "allow", exact=True)[0]["text"] == "Allow"


def test_resolve_host_and_quiet(tmp_path, monkeypatch):
    cfg = tmp_path / "remote-screen.yaml"
    cfg.write_text(json.dumps({"hosts": {"mini": {"address": "box.local", "aliases": ["box"], "user": "u"}}}))
    monkeypatch.setenv("HERMES_REMOTE_SCREEN_CONFIG", str(cfg))
    h = rs.resolve_host("box")
    assert (h["name"], h["address"], h["user"], h["port"]) == ("mini", "box.local", "u", 5900)
    assert rs.resolve_host("10.0.0.9:5901")["port"] == 5901
    q = tmp_path / "quiet.json"
    monkeypatch.setenv("HERMES_QUIET_MODE_FILE", str(q))
    assert rs.quiet_blocks("localhost") is None
    q.write_text(json.dumps({"on": True}))
    assert rs.quiet_blocks("localhost") and rs.quiet_blocks("192.0.2.55") is None


def test_missing_login_points_at_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_REMOTE_SCREEN_CONFIG", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(rs, "get_password", lambda h: None)
    out = json.loads(rs.handle_screenshot({"host": "198.51.100.7"}))
    assert not out["ok"] and "security add-generic-password" in out["error"]


def test_request_phrase_wrapped_mid_sentence():
    # Real macOS 26 Files & Folders prompt, as OCR'd: the phrase wraps after "would like".
    lines = [L("\"Hermes TCC Probe\" would like", 1610, 378, 180, 14), L("to access files in your Desktop", 1612, 396, 180, 14),
             L("folder.", 1610, 412, 40, 12), L("Don't Allow", 1626, 450, 70, 12), L("Allow", 1762, 450, 34, 12)]
    d = rs.find_dialog(lines, ["Hermes"])
    assert d["status"] == "approve" and d["requester"] == "Hermes TCC Probe" and d["button"]["x"] == 1762
