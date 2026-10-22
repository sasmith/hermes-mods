"""Minimal blocking RFB (VNC) client for macOS Screen Sharing: log in with a macOS account, grab the
framebuffer, send pointer and key events. Stdlib + `cryptography` (AES/RSA for Apple's auth types).

Supported security types, in order of preference:
  30  Apple "Diffie-Hellman" auth (ARD): DH key agreement, MD5(shared) as an AES-128-ECB key that
      encrypts 64-byte username + 64-byte password blocks.
  33  Apple RSA auth: fetch the server's RSA key, send the credential blocks AES-ECB encrypted with a
      random key that is itself RSA(PKCS#1 v1.5)-encrypted.
  2   classic VNC password (only if `password` is set and nothing better is offered).
Written from the protocol descriptions (RFC 6143 + public notes on Apple's types 30/33).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import select
import socket
import struct
import time
import zlib
from dataclasses import dataclass, field

# ---------------------------------------------------------------- keysyms

KEYSYMS = {
    "return": 0xFF0D, "enter": 0xFF0D, "tab": 0xFF09, "escape": 0xFF1B, "esc": 0xFF1B,
    "backspace": 0xFF08, "delete": 0xFFFF, "forwarddelete": 0xFFFF, "space": 0x20,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
    "shift": 0xFFE1, "ctrl": 0xFFE3, "control": 0xFFE3,
    # macOS Screen Sharing maps X11 Meta/Super to Command and Alt to Option.
    "cmd": 0xFFE7, "command": 0xFFE7, "meta": 0xFFE7, "super": 0xFFEB,
    "alt": 0xFFE9, "option": 0xFFE9, "opt": 0xFFE9,
    **{f"f{i}": 0xFFBE + i - 1 for i in range(1, 13)},
}
MODIFIERS = {"shift", "ctrl", "control", "cmd", "command", "meta", "super", "alt", "option", "opt"}


def char_keysym(ch: str) -> int:
    cp = ord(ch)
    if ch == "\n":
        return KEYSYMS["return"]
    if ch == "\t":
        return KEYSYMS["tab"]
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp


def parse_combo(combo: str) -> list[int]:
    """'cmd+shift+4' -> keysyms (modifiers first, key last). Raises ValueError on unknown names."""
    parts = [p for p in combo.replace(" ", "").split("+") if p] if combo != "+" else ["+"]
    if not parts:
        raise ValueError("empty key combo")
    out = []
    for p in parts:
        low = p.lower()
        if low in KEYSYMS:
            out.append(KEYSYMS[low])
        elif len(p) == 1:
            out.append(char_keysym(p.lower() if len(parts) > 1 else p))
        else:
            raise ValueError(f"unknown key {p!r}")
    return out


# ---------------------------------------------------------------- auth helpers

def _cred_block(s: str) -> bytes:
    b = s.encode("utf-8")[:63] + b"\x00"
    return b + os.urandom(64 - len(b))


def _aes_ecb(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(data) + enc.finalize()


def ard_dh_response(generator: int, prime: bytes, server_pub: bytes, username: str, password: str,
                    priv: int | None = None) -> tuple[bytes, bytes]:
    """Type-30 client message parts: (ciphertext(128), client_pub(keylen))."""
    keylen = len(prime)
    p = int.from_bytes(prime, "big")
    priv = priv or (secrets.randbits(keylen * 8) % (p - 2)) + 1
    pub = pow(generator, priv, p).to_bytes(keylen, "big")
    shared = pow(int.from_bytes(server_pub, "big"), priv, p).to_bytes(keylen, "big")
    key = hashlib.md5(shared).digest()
    return _aes_ecb(key, _cred_block(username) + _cred_block(password)), pub


class AuthError(PermissionError):
    pass


# ---------------------------------------------------------------- client

@dataclass
class Frame:
    width: int
    height: int
    bgrx: bytearray = field(repr=False)  # width*height*4, B,G,R,X

    def to_image(self):
        from PIL import Image
        return Image.frombuffer("RGB", (self.width, self.height), bytes(self.bgrx), "raw", "BGRX", 0, 1)


class RFBClient:
    """Blocking client. Use as a context manager: `with RFBClient(host, user=..., password=...) as c:`."""

    PREFERRED = (30, 33, 2)

    UPDATE_GRACE_S = 2.0  # quiet time after an update before a partial framebuffer counts as complete

    def __init__(self, host: str, port: int = 5900, user: str = "", password: str = "", timeout: float = 15.0):
        self.host, self.port, self.user, self.password, self.timeout = host, port, user, password, timeout
        self.sock: socket.socket | None = None
        self.width = self.height = 0
        self.name = ""
        self.security_type = 0
        self._z = zlib.decompressobj()
        self.buttons = 0

    # -- io
    def _recv(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(min(n - len(buf), 1 << 20))
            if not chunk:
                raise ConnectionError("VNC server closed the connection")
            buf += chunk
        return bytes(buf)

    def _u(self, fmt: str):
        return struct.unpack(">" + fmt, self._recv(struct.calcsize(">" + fmt)))[0]

    def _send(self, data: bytes) -> None:
        self.sock.sendall(data)

    # -- lifecycle
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def connect(self) -> "RFBClient":
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        version = self._recv(12)
        if not version.startswith(b"RFB "):
            raise ConnectionError(f"not a VNC server: {version!r}")
        # Apple's server speaks 3.889; echoing it unlocks types 30/33.
        self._send(version if version == b"RFB 003.889\n" else b"RFB 003.008\n")
        n = self._u("B")
        if n == 0:
            raise ConnectionError(self._recv(self._u("I")).decode("utf-8", "replace"))
        offered = list(self._recv(n))
        for t in self.PREFERRED:
            if t in offered and (t != 2 or self.password):
                break
        else:
            raise AuthError(f"no supported security type (server offers {offered})")
        self.security_type = t
        self._send(bytes([t]))
        getattr(self, f"_auth_{t}")()
        result = self._u("I")
        if result != 0:
            reason = ""
            try:
                reason = self._recv(self._u("I")).decode("utf-8", "replace")
            except Exception:
                pass
            raise AuthError(f"VNC login failed for {self.user or '(no user)'}@{self.host}"
                            + (f": {reason}" if reason else " (wrong username/password?)"))
        self._send(b"\x01")  # ClientInit: shared session (don't kick a viewing user off)
        self.width, self.height = self._u("H"), self._u("H")
        self._recv(16)  # server pixel format (we override it)
        self.name = self._recv(self._u("I")).decode("utf-8", "replace")
        # SetPixelFormat: 32bpp, depth 24, little-endian, true colour, 8 bits/channel, R<<16 G<<8 B -> BGRX bytes.
        self._send(struct.pack(">B3xBBBBHHHBBB3x", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        # SetEncodings: zlib, raw, DesktopSize pseudo-encoding.
        encs = [6, 0, -223]
        self._send(struct.pack(">BxH", 2, len(encs)) + b"".join(struct.pack(">i", e) for e in encs))
        return self

    def _auth_30(self) -> None:
        if not (self.user and self.password):
            raise AuthError("server wants a macOS username and password")
        gen = self._u("H")
        keylen = self._u("H")
        prime, server_pub = self._recv(keylen), self._recv(keylen)
        ct, pub = ard_dh_response(gen, prime, server_pub, self.user, self.password)
        self._send(ct + pub)

    def _auth_33(self) -> None:
        if not (self.user and self.password):
            raise AuthError("server wants a macOS username and password")
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        # Request the host key: length, version 1, "RSA1", empty payload.
        self._send(struct.pack(">IH", 10, 1) + b"RSA1" + b"\x00\x00\x00\x00")
        self._u("I")  # packet length
        self._u("H")  # version
        der = self._recv(self._u("I"))
        self._recv(1)
        key = load_der_public_key(der)
        aes = os.urandom(16)
        body = (struct.pack(">H", 1) + b"RSA1" + struct.pack(">H", 1)
                + _aes_ecb(aes, _cred_block(self.user) + _cred_block(self.password))
                + struct.pack(">H", 1) + key.encrypt(aes, padding.PKCS1v15()))
        self._send(struct.pack(">I", len(body)) + body)
        self._recv(4)

    def _auth_2(self) -> None:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
        from cryptography.hazmat.primitives.ciphers import Cipher, modes
        challenge = self._recv(16)
        k = self.password.encode("latin-1", "replace")[:8].ljust(8, b"\x00")
        k = bytes(int(f"{b:08b}"[::-1], 2) for b in k)  # VNC's bit-reversed DES key
        enc = Cipher(TripleDES(k * 3), modes.ECB()).encryptor()
        self._send(enc.update(challenge) + enc.finalize())

    # -- framebuffer
    def screenshot(self, settle_s: float = 0.0) -> Frame:
        if settle_s:
            time.sleep(settle_s)
        buf = bytearray(self.width * self.height * 4)
        frame = Frame(self.width, self.height, buf)
        self._send(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))
        covered = 0
        need = self.width * self.height
        deadline = time.time() + max(self.timeout, 30)
        updates = 0
        while covered < need:
            if time.time() > deadline:
                raise TimeoutError("timed out waiting for the full framebuffer")
            # A multi-display Mac reports the bounding box of all displays as the framebuffer but
            # only ever sends rects for the displays themselves, so the dead corners never arrive.
            # Once an update has landed, a quiet socket at a message boundary means "that's all".
            if updates and not select.select([self.sock], [], [], self.UPDATE_GRACE_S)[0]:
                break
            mtype = self._u("B")
            if mtype == 0:
                self._recv(1)
                for _ in range(self._u("H")):
                    x, y, w, h, enc = struct.unpack(">HHHHi", self._recv(12))
                    if enc == -223:  # DesktopSize: restart with the new geometry
                        self.width, self.height = w, h
                        return self.screenshot()
                    if enc == 0:
                        data = self._recv(w * h * 4)
                    elif enc == 6:
                        data = self._z.decompress(self._recv(self._u("I")))
                    else:
                        raise ConnectionError(f"unexpected encoding {enc}")
                    if len(data) < w * h * 4:
                        raise ConnectionError("short rectangle")
                    row = w * 4
                    for r in range(h):
                        o = ((y + r) * self.width + x) * 4
                        frame.bgrx[o:o + row] = data[r * row:(r + 1) * row]
                    covered += w * h
                updates += 1
            elif mtype == 1:  # colour map (unused with true colour)
                self._recv(3)
                self._recv(self._u("H") * 6)
            elif mtype == 2:  # bell
                pass
            elif mtype == 3:  # server cut text
                self._recv(3)
                self._recv(self._u("I"))
            else:
                raise ConnectionError(f"unexpected server message {mtype}")
        return frame

    # -- input
    def pointer(self, x: int, y: int, buttons: int | None = None) -> None:
        if buttons is not None:
            self.buttons = buttons
        x = max(0, min(int(x), self.width - 1))
        y = max(0, min(int(y), self.height - 1))
        self._send(struct.pack(">BBHH", 5, self.buttons, x, y))

    def click(self, x: int, y: int, button: int = 1, count: int = 1, hold_s: float = 0.06) -> None:
        mask = {1: 1, 2: 2, 3: 4}.get(button, 1)
        self.pointer(x, y, 0)
        time.sleep(0.15)  # let the hover land first; some dialogs ignore instant clicks
        for _ in range(max(1, count)):
            self.pointer(x, y, mask)
            time.sleep(hold_s)
            self.pointer(x, y, 0)
            time.sleep(0.08)

    def scroll(self, x: int, y: int, amount: int) -> None:
        mask = 8 if amount < 0 else 16  # wheel up / down
        self.pointer(x, y, 0)
        for _ in range(abs(amount)):
            self.pointer(x, y, mask)
            self.pointer(x, y, 0)
            time.sleep(0.02)

    def key(self, keysym: int, down: bool) -> None:
        self._send(struct.pack(">BBxxI", 4, 1 if down else 0, keysym))

    def press(self, combo: str) -> None:
        syms = parse_combo(combo)
        for s in syms:
            self.key(s, True)
            time.sleep(0.02)
        for s in reversed(syms):
            self.key(s, False)
            time.sleep(0.02)

    def type_text(self, text: str, delay_s: float = 0.015) -> None:
        for ch in text:
            s = char_keysym(ch)
            shift = ch.isupper() or ch in '~!@#$%^&*()_+{}|:"<>?'
            if shift:
                self.key(KEYSYMS["shift"], True)
            self.key(s, True)
            self.key(s, False)
            if shift:
                self.key(KEYSYMS["shift"], False)
            time.sleep(delay_s)
