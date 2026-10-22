"""remote-screen: see and drive another Mac's screen over macOS Screen Sharing (VNC), with no window on
this Mac. Main use: approving permission dialogs ("X would like to access ...") that block unattended
work on a headless/remote Mac.

Tools (toolset `remote_screen`):
- remote_screen_hosts     configured hosts, reachability, whether a login is stored, setup command
- remote_screenshot       grab the remote screen (image goes to the model; full-res PNG saved), optional OCR
- remote_input            click / double / right click / move / scroll / type / key combo, by coordinates
                          or by on-screen text (OCR); returns a fresh screenshot
- remote_approve_dialog   screenshot -> OCR -> find a permission request naming an ALLOWLISTED app ->
                          click its approve button -> re-check it is gone. Refuses password prompts and
                          anything not on the allowlist. Before/after screenshots + a JSON log are kept.

Clicks arrive through Apple's screensharingd, i.e. as a remote user's real input, which macOS accepts on
dialogs that ignore synthetic (CGEvent / Accessibility) clicks. Some approvals still refuse remote input
(kernel/system extensions); the tool reports when a click had no effect.

Config: <hermes home>/remote-screen.yaml (see README). Logins: macOS Keychain item
service=hermes-remote-screen, account=<user>@<address> (or `password_env`). Never typed by the agent.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

if __package__:
    from . import rfb
else:  # loaded by file path (pytest)
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("remote_screen_rfb", Path(__file__).with_name("rfb.py"))
    rfb = _ilu.module_from_spec(_spec)
    import sys as _sys
    _sys.modules[_spec.name] = rfb  # dataclasses look the module up by name
    _spec.loader.exec_module(rfb)

if __package__:
    from . import screen_guard
else:
    _gspec = _ilu.spec_from_file_location("remote_screen_guard", Path(__file__).with_name("screen_guard.py"))
    screen_guard = _ilu.module_from_spec(_gspec)
    _gspec.loader.exec_module(screen_guard)
TURNS = screen_guard.TURNS
pre_tool_call = screen_guard.pre_tool_call
pre_llm_call = screen_guard.pre_llm_call

log = logging.getLogger("remote-screen")
TOOLSET = "remote_screen"
KEYCHAIN_SERVICE = "hermes-remote-screen"
MAX_IMAGE_WIDTH = 1600

DEFAULT_APPROVE_BUTTONS = ["Allow", "OK", "Always Allow", "Allow Full Access", "Allow While Using App", "Open"]
# Phrases macOS uses in permission requests.
REQUEST_RE = re.compile(
    r"\bwould like\b|\bwants (?:to|access)\b|\bwants\s*$|is trying to|requesting access|"
    r"access to (?:your |the )?(?:photos|contacts|calendars?|reminders|files|folder|camera|microphone|"
    r"desktop|documents|downloads|data|location|keychain)", re.I)
PASSWORD_RE = re.compile(r"\bpassword\b|enter your password|touch id", re.I)


# ---------------------------------------------------------------- paths / config

def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))


def config_path() -> Path:
    return Path(os.environ.get("HERMES_REMOTE_SCREEN_CONFIG") or hermes_home() / "remote-screen.yaml")


def cache_dir(host: str = "") -> Path:
    d = hermes_home() / "cache" / "remote-screen"
    if host:
        d = d / re.sub(r"[^A-Za-z0-9_.-]", "_", host)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        pass
    try:
        from ruamel.yaml import YAML  # type: ignore
        return YAML(typ="safe", pure=True).load(text)
    except ImportError:
        pass
    try:
        return json.loads(text)
    except ValueError as e:
        raise ValueError(f"{config_path()}: no YAML parser (PyYAML/ruamel.yaml) and not JSON: {e}") from e


def load_config() -> dict:
    p = config_path()
    try:
        data = _parse_yaml(p.read_text()) or {}
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected a mapping")
    data.setdefault("hosts", {})
    data.setdefault("approve", {})
    return data


def resolve_host(name: str, cfg: dict | None = None) -> dict:
    """Host name (config key) or raw address -> {name, address, port, user, password_env, screenshare}."""
    cfg = cfg if cfg is not None else load_config()
    name = (name or "").strip()
    if not name:
        raise ValueError("host is required")
    hosts = cfg.get("hosts") or {}
    entry = hosts.get(name)
    if entry is None:
        for k, v in hosts.items():
            if isinstance(v, dict) and name in (v.get("address"), *(v.get("aliases") or [])):
                name, entry = k, v
                break
    entry = dict(entry or {})
    addr = str(entry.get("address") or name)
    port = int(entry.get("port") or 5900)
    if ":" in addr and addr.count(":") == 1:
        addr, p = addr.split(":")
        port = int(p)
    return {"name": name, "address": addr, "port": port,
            "user": str(entry.get("user") or os.environ.get("USER") or ""),
            "password_env": entry.get("password_env"),
            "screenshare": str(entry.get("screenshare_address") or addr),
            "label": str(entry.get("label") or name)}


def keychain_account(h: dict) -> str:
    return f"{h['user']}@{h['address']}"


def setup_command(h: dict) -> str:
    return (f"security add-generic-password -U -s {KEYCHAIN_SERVICE} -a '{keychain_account(h)}' "
            f"-l 'Hermes remote-screen: {h['address']}' -w")


def get_password(h: dict) -> str | None:
    env = h.get("password_env")
    if env and os.environ.get(env):
        return os.environ[env]
    if not shutil.which("security"):
        return None
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", keychain_account(h),
                        "-w"], capture_output=True, text=True, timeout=20)
    return r.stdout.rstrip("\n") if r.returncode == 0 and r.stdout else None


def _is_local(addr: str) -> bool:
    if addr in ("localhost", "127.0.0.1", "::1"):
        return True
    names = {socket.gethostname().lower(), platform.node().lower()}
    names |= {n.split(".")[0] for n in names}
    if addr.lower() in names or addr.lower().split(".")[0] in names:
        return True
    try:
        ip = socket.gethostbyname(addr)
        return ip.startswith("127.") or ip in {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        return False


def quiet_blocks(addr: str) -> str | None:
    """Quiet mode (movie mode) only cares about THIS Mac's screen: refuse input aimed at it."""
    p = Path(os.environ.get("HERMES_QUIET_MODE_FILE") or Path.home() / ".hermes" / "quiet-mode.json")
    try:
        st = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not st.get("on") or (st.get("until") and time.time() >= float(st["until"])):
        return None
    if _is_local(addr):
        return "Quiet mode is on and this host is this Mac's own screen; skip this step (or target another Mac)."
    return None


# ---------------------------------------------------------------- connection

def connect(h: dict) -> "rfb.RFBClient":
    pw = get_password(h)
    if not pw:
        raise PermissionError(
            f"No stored login for {keychain_account(h)}. Store it once (the user types the password; never the "
            f"agent): run_in_user_terminal with command: {setup_command(h)}")
    return rfb.RFBClient(h["address"], h["port"], user=h["user"], password=pw).connect()


def _save_frame(h: dict, frame, tag: str) -> Path:
    path = cache_dir(h["name"]) / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.png"
    frame.to_image().save(path)
    _prune(cache_dir(h["name"]), keep=60)
    return path


def _prune(d: Path, keep: int) -> None:
    files = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        p.unlink(missing_ok=True)


def _state_path(h: dict) -> Path:
    return cache_dir(h["name"]) / "last.json"


def _write_state(h: dict, **kw) -> None:
    _state_path(h).write_text(json.dumps(kw))


def _read_state(h: dict) -> dict:
    try:
        return json.loads(_state_path(h).read_text())
    except (OSError, ValueError):
        return {}


def _model_image(frame, max_width: int) -> tuple[str, float, tuple[int, int]]:
    img = frame.to_image()
    scale = 1.0
    if img.width > max_width:
        scale = max_width / img.width
        img = img.resize((max_width, round(img.height * scale)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode(), scale, img.size


# ---------------------------------------------------------------- OCR

def _ocr_binary() -> Path | None:
    src = Path(__file__).with_name("ocr.swift")
    out = cache_dir() / "remote-screen-ocr"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    swiftc = shutil.which("swiftc")
    if not swiftc or platform.system() != "Darwin":
        return None
    r = subprocess.run([swiftc, "-O", str(src), "-o", str(out)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        log.warning("remote-screen: OCR helper build failed: %s", r.stderr[-500:])
        return None
    return out


def ocr(png: Path) -> list[dict]:
    b = _ocr_binary()
    if not b:
        raise RuntimeError("OCR needs macOS with swiftc (Xcode command line tools)")
    r = subprocess.run([str(b), str(png)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"OCR failed: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout or "[]")


def ocr_region(png: Path, box: tuple[int, int, int, int], zoom: int = 3) -> list[dict]:
    """OCR a crop upscaled `zoom`x (small dialog buttons, e.g. white-on-blue "Allow", are missed at full-screen
    scale), with boxes mapped back to screen pixels."""
    from PIL import Image
    im = Image.open(png).convert("RGB")
    x0, y0 = max(0, box[0]), max(0, box[1])
    x1, y1 = min(im.width, box[2]), min(im.height, box[3])
    if x1 - x0 < 20 or y1 - y0 < 20:
        return []
    crop = im.crop((x0, y0, x1, y1))
    crop = crop.resize((crop.width * zoom, crop.height * zoom), Image.LANCZOS)
    tmp = png.with_name(png.stem + f"-zoom{x0}_{y0}.png")
    crop.save(tmp)
    try:
        lines = ocr(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return [{**ln, "x": x0 + ln["x"] // zoom, "y": y0 + ln["y"] // zoom, "w": ln["w"] // zoom,
             "h": ln["h"] // zoom, "zoomed": True} for ln in lines]


def read_dialogs(png: Path) -> list[dict]:
    """Full-screen OCR plus a zoomed re-read around every likely permission prompt."""
    lines = ocr(png)
    anchors = [ln for ln in lines if REQUEST_RE.search(ln["text"]) or _norm(ln["text"]) == "dont allow"]
    extra: list[dict] = []
    seen: list[tuple[int, int]] = []
    for a in anchors:
        cx, cy = _center(a)
        if any(abs(cx - sx) < 200 and abs(cy - sy) < 200 for sx, sy in seen):
            continue
        seen.append((cx, cy))
        extra += ocr_region(png, (cx - 400, cy - 250, cx + 400, cy + 350))
    return extra + lines


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower().replace("’", "'").replace("'", "")).strip()


def find_text(lines: list[dict], text: str, exact: bool = False) -> list[dict]:
    t = _norm(text)
    hits = [ln for ln in lines if (_norm(ln["text"]) == t if exact else t in _norm(ln["text"]))]
    return sorted(hits, key=lambda ln: (_norm(ln["text"]) != t, -ln.get("conf", 0)))


def _center(ln: dict) -> tuple[int, int]:
    return ln["x"] + ln["w"] // 2, ln["y"] + ln["h"] // 2


def requester_text(req: dict, lines: list[dict]) -> str:
    """The part of a prompt naming WHO asks: the request line up to the matched phrase, plus the line right
    above it when the name wraps ("“Some Long" / "App” would like to ..."). Background text never counts."""
    m = REQUEST_RE.search(req["text"])
    head = req["text"][:m.start()] if m else req["text"]
    lh = max(req["h"], 8)
    above = [ln for ln in lines if 0 < req["y"] - ln["y"] <= 1.8 * lh and abs(ln["x"] - req["x"]) <= 2 * lh
             and ln["h"] <= 2 * lh and not REQUEST_RE.search(ln["text"])]
    if above and not re.search(r"[“\"][^”\"]+[”\"]", head):
        head = max(above, key=lambda ln: ln["y"])["text"] + " " + head
    q = re.findall(r"[“\"]([^”\"]+)[”\"]", head)
    return (q[0] if q else head).strip(" \"“”'")


def find_dialog(lines: list[dict], apps: list[str], buttons: list[str] | None = None) -> dict:
    """Pure decision logic. Returns {status, ...}:
       none       - no permission request on screen
       approve    - request naming an allowlisted app + an approve button (button, request, app)
       blocked    - request found but app not allowlisted / no button / password prompt (reason)"""
    buttons = buttons or DEFAULT_APPROVE_BUTTONS
    requests = [ln for ln in lines if REQUEST_RE.search(ln["text"])]
    if not requests:
        return {"status": "none"}
    blocked = None
    for req in sorted(requests, key=lambda ln: (not ln.get("zoomed"), ln["y"])):
        rx, ry = _center(req)
        span = max(300, 25 * req["h"])
        region = [ln for ln in lines if -8 * req["h"] <= ln["y"] - req["y"] <= span and abs(_center(ln)[0] - rx) <= span]
        texts: list[str] = []
        for ln in sorted(region, key=lambda ln: (ln["y"], ln["x"])):
            if abs(ln["x"] - req["x"]) <= 4 * req["h"] and ln["text"] not in texts:
                texts.append(ln["text"])
        text = " ".join(texts)
        who = requester_text(req, lines)
        app = next((a for a in apps if a and _norm(a) in _norm(who)), None)
        if any(PASSWORD_RE.search(ln["text"]) for ln in region):
            blocked = blocked or {"status": "blocked", "reason": "password", "requester": who, "dialog": text}
            continue
        if not app:
            blocked = blocked or {"status": "blocked", "reason": "app_not_allowlisted", "requester": who, "dialog": text}
            continue
        wanted = {_norm(b): i for i, b in enumerate(buttons)}
        cands = [ln for ln in region if _norm(ln["text"]) in wanted and ln["y"] > req["y"]]
        if not cands:
            blocked = blocked or {"status": "blocked", "reason": "no_approve_button", "requester": who, "dialog": text}
            continue
        btn = min(cands, key=lambda ln: (wanted[_norm(ln["text"])], not ln.get("zoomed"),
                                          abs(_center(ln)[1] - ry) + abs(_center(ln)[0] - rx) / 4))
        return {"status": "approve", "app": app, "requester": who, "dialog": text, "request": req, "button": btn}
    return blocked or {"status": "none"}


# ---------------------------------------------------------------- tools

def _err(msg: str, **kw) -> str:
    return json.dumps({"ok": False, "error": msg, **kw})


def _screenshare_hint(h: dict) -> str:
    return f'::screenshare{{host="{h["screenshare"]}" label="{h["label"]}"}}'


def _image_result(h: dict, frame, payload: dict, max_width: int = MAX_IMAGE_WIDTH) -> dict:
    path = _save_frame(h, frame, payload.get("tag", "shot"))
    b64, scale, (w, hgt) = _model_image(frame, max_width)
    _write_state(h, scale=scale, png=str(path), width=frame.width, height=frame.height, at=time.time())
    payload.update(ok=True, host=h["name"], screenshot=str(path), screen_size=[frame.width, frame.height],
                   image_size=[w, hgt],
                   coordinates="Pass x/y as pixels of the image shown here; the tool rescales to the screen.")
    payload.pop("tag", None)
    text = json.dumps(payload)
    return {"_multimodal": True, "text_summary": text,
            "content": [{"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}


def handle_hosts(args: dict, **_kw) -> str:
    try:
        cfg = load_config()
    except Exception as e:
        return _err(str(e))
    names = list(cfg["hosts"]) or []
    if args.get("host"):
        names = [args["host"]]
    out = []
    for n in names:
        h = resolve_host(n, cfg)
        reachable = False
        try:
            with socket.create_connection((h["address"], h["port"]), timeout=3) as s:
                reachable = s.recv(4) == b"RFB "
        except OSError:
            pass
        out.append({"host": h["name"], "address": h["address"], "user": h["user"], "screen_sharing": reachable,
                    "login_stored": bool(get_password(h)), "setup_command": setup_command(h),
                    "screenshare_button": _screenshare_hint(h)})
    return json.dumps({"ok": True, "config": str(config_path()), "hosts": out,
                       "approve_apps": cfg["approve"].get("apps") or []})


def handle_screenshot(args: dict, **_kw) -> Any:
    try:
        h = resolve_host(args.get("host", ""))
        with connect(h) as c:
            frame = c.screenshot(settle_s=0.2)
        payload: dict = {"tag": "shot"}
        if args.get("ocr"):
            path = _save_frame(h, frame, "ocr")
            payload["text"] = [{"text": ln["text"], "x": ln["x"], "y": ln["y"], "w": ln["w"], "h": ln["h"]}
                               for ln in ocr(path)]
            payload["text_coordinates"] = "OCR boxes are in full screen pixels (screen_size)."
        return _image_result(h, frame, payload, int(args.get("max_width") or MAX_IMAGE_WIDTH))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def handle_input(args: dict, **_kw) -> Any:
    try:
        h = resolve_host(args.get("host", ""))
        if (why := quiet_blocks(h["address"])):
            return _err(why, code="quiet_mode")
        action = str(args.get("action") or "click").lower()
        with connect(h) as c:
            x = y = None
            if args.get("target_text"):
                frame = c.screenshot()
                png = _save_frame(h, frame, "target")
                hits = find_text(ocr(png), str(args["target_text"]), exact=bool(args.get("exact")))
                if not hits:
                    return _err(f"text {args['target_text']!r} not found on screen", screenshot=str(png))
                x, y = _center(hits[0])
            elif args.get("x") is not None and args.get("y") is not None:
                scale = float(_read_state(h).get("scale") or 1.0)
                if args.get("screen_pixels"):
                    scale = 1.0
                x, y = round(float(args["x"]) / scale), round(float(args["y"]) / scale)
            if action in ("click", "double_click", "right_click", "move", "scroll") and x is None:
                return _err("give x/y (from the last remote_screenshot image) or target_text")
            if action == "click":
                c.click(x, y)
            elif action == "double_click":
                c.click(x, y, count=2)
            elif action == "right_click":
                c.click(x, y, button=3)
            elif action == "move":
                c.pointer(x, y, 0)
            elif action == "scroll":
                c.scroll(x, y, int(args.get("amount") or 3))
            elif action == "type":
                c.type_text(str(args.get("text") or ""))
            elif action == "key":
                for combo in str(args.get("keys") or "").split():
                    c.press(combo)
                    time.sleep(0.1)
            else:
                return _err(f"unknown action {action!r}")
            if args.get("capture_after", True):
                frame = c.screenshot(settle_s=float(args.get("settle_s") or 0.8))
                return _image_result(h, frame, {"tag": action, "action": action,
                                                "screen_xy": [x, y] if x is not None else None})
        return json.dumps({"ok": True, "host": h["name"], "action": action, "screen_xy": [x, y]})
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


def handle_approve(args: dict, **_kw) -> Any:
    try:
        cfg = load_config()
        h = resolve_host(args.get("host", ""), cfg)
        if (why := quiet_blocks(h["address"])):
            return _err(why, code="quiet_mode")
        apps = list(cfg["approve"].get("apps") or [])
        if args.get("app"):
            want = _norm(str(args["app"]))
            apps = [a for a in apps if _norm(a) == want or want in _norm(a)]
            if not apps:
                return _err(f"{args['app']!r} is not in approve.apps in {config_path()}; ask the user to add it "
                            "or approve it themselves.", screenshare_button=_screenshare_hint(h))
        if not apps:
            return _err(f"approve.apps is empty in {config_path()}; nothing may be auto-approved.")
        buttons = list(cfg["approve"].get("buttons") or DEFAULT_APPROVE_BUTTONS)
        logd = cache_dir(h["name"]) / "approvals"
        logd.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with connect(h) as c:
            before = c.screenshot(settle_s=0.2)
            bpath = logd / f"{stamp}-before.png"
            before.to_image().save(bpath)
            decision = find_dialog(read_dialogs(bpath), apps, buttons)
            record = {"at": stamp, "host": h["name"], "decision": {k: v for k, v in decision.items()}, "before": str(bpath)}
            if decision["status"] != "approve" or args.get("dry_run"):
                (logd / f"{stamp}.json").write_text(json.dumps(record, indent=2))
                res = {"ok": decision["status"] in ("none", "approve"), "host": h["name"], **decision,
                       "dry_run": bool(args.get("dry_run")), "before": str(bpath)}
                if decision["status"] == "blocked":
                    res["next"] = ("Not auto-approvable. Ask the user to handle it and put this button next to "
                                   f"the ask: {_screenshare_hint(h)}")
                return json.dumps(res)
            bx, by = _center(decision["button"])
            c.click(bx, by)
            after = c.screenshot(settle_s=1.2)
            apath = logd / f"{stamp}-after.png"
            after.to_image().save(apath)
            still = find_dialog(read_dialogs(apath), apps, buttons)
            gone = not (still["status"] == "approve" and _norm(still["request"]["text"]) == _norm(decision["request"]["text"]))
            record.update(clicked=[bx, by], after=str(apath), dismissed=gone)
            (logd / f"{stamp}.json").write_text(json.dumps(record, indent=2))
            payload = {"tag": "approved", "status": "approved" if gone else "click_ignored", "app": decision["app"], "requester": decision.get("requester"),
                       "dialog": decision["dialog"], "button": decision["button"]["text"], "clicked": [bx, by],
                       "before": str(bpath), "log": str(logd / f"{stamp}.json")}
            if not gone:
                payload["next"] = ("The dialog is still there: macOS may refuse remote clicks for this prompt. Ask "
                                   f"the user to click it and include: {_screenshare_hint(h)}")
            return _image_result(h, after, payload)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- schemas

_HOST = {"type": "string", "description": "Host name from remote-screen.yaml (or a raw address with a stored login)."}

SCHEMAS = {
    "remote_screen_hosts": {
        "name": "remote_screen_hosts",
        "description": ("List Macs this agent can see/control over Screen Sharing (remote-screen.yaml): reachable?, "
                        "login stored?, the one-time setup command for a missing login, and the ::screenshare "
                        "button directive for the user."),
        "parameters": {"type": "object", "properties": {"host": _HOST}},
    },
    "remote_screenshot": {
        "name": "remote_screenshot",
        "description": ("Screenshot another Mac over Screen Sharing (VNC). No window opens on this Mac. Returns the "
                        "image plus the saved full-res PNG path. ocr=true adds on-screen text with boxes."),
        "parameters": {"type": "object", "required": ["host"], "properties": {
            "host": _HOST, "ocr": {"type": "boolean"},
            "max_width": {"type": "integer", "description": "Downscale width for the returned image (default 1600)."}}},
    },
    "remote_input": {
        "name": "remote_input",
        "description": ("Click, move, scroll, type or press keys on another Mac over Screen Sharing. Coordinates are "
                        "pixels of the latest remote_screenshot image; or pass target_text to click on-screen text "
                        "(OCR). keys: space-separated combos like 'cmd+space return'. Returns a fresh screenshot. "
                        "Never type passwords or codes: hand those to the user with the ::screenshare button."),
        "parameters": {"type": "object", "required": ["host", "action"], "properties": {
            "host": _HOST,
            "action": {"type": "string", "enum": ["click", "double_click", "right_click", "move", "scroll", "type", "key"]},
            "x": {"type": "number"}, "y": {"type": "number"},
            "screen_pixels": {"type": "boolean", "description": "x/y are full screen pixels (e.g. OCR boxes)."},
            "target_text": {"type": "string", "description": "Click the centre of this on-screen text instead of x/y."},
            "exact": {"type": "boolean", "description": "target_text must match the whole OCR line."},
            "text": {"type": "string"}, "keys": {"type": "string"},
            "amount": {"type": "integer", "description": "scroll steps; negative = up"},
            "capture_after": {"type": "boolean", "description": "default true"},
            "settle_s": {"type": "number"}}},
    },
    "remote_approve_dialog": {
        "name": "remote_approve_dialog",
        "description": ("Approve a macOS permission prompt on another Mac (\"X would like to access ...\" → Allow/OK) "
                        "when X is on the user's allowlist (approve.apps in remote-screen.yaml). Screenshots, OCRs, "
                        "clicks the approve button, then verifies the prompt is gone. Refuses password/Touch ID "
                        "prompts and non-allowlisted apps (then ask the user, with the ::screenshare button). "
                        "dry_run=true only reports what it would click."),
        "parameters": {"type": "object", "required": ["host"], "properties": {
            "host": _HOST, "app": {"type": "string", "description": "Only approve a prompt from this allowlisted app."},
            "dry_run": {"type": "boolean"}}},
    },
}

HANDLERS = {"remote_screen_hosts": handle_hosts, "remote_screenshot": handle_screenshot,
            "remote_input": handle_input, "remote_approve_dialog": handle_approve}
EMOJI = {"remote_screen_hosts": "🖥️", "remote_screenshot": "📸", "remote_input": "🖱️", "remote_approve_dialog": "✅"}


def _deps_ok() -> bool:
    try:
        import cryptography  # noqa: F401
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def register(ctx) -> None:
    # The guard needs no cryptography/Pillow: always on, even where the tools are not registered.
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    if not _deps_ok():
        log.info("remote-screen: cryptography/Pillow missing; tools not registered")
        return
    for name, schema in SCHEMAS.items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=HANDLERS[name], emoji=EMOJI[name],
                          description=schema["description"].split(".")[0])
