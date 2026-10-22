"""skill-triggers: inject a governing skill's key section into a tool result when its trigger fires."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MARK = "[skill-triggers]"


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


# ---------------------------------------------------------------- config
def _parse(text: str) -> dict:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        pass
    try:
        from ruamel.yaml import YAML  # type: ignore
        return YAML(typ="safe", pure=True).load(text) or {}
    except ImportError:
        pass
    try:
        return json.loads(text)
    except ValueError as e:
        raise ValueError("skill-triggers needs PyYAML or ruamel.yaml to read YAML config") from e


def load_rows(user_path: Path | None = None, default_path: Path | None = None) -> list[dict]:
    default_path = default_path or HERE / "triggers.yaml"
    user_path = user_path or _home() / "skill-triggers.yaml"
    rows: dict[str, dict] = {}
    for p in (default_path, user_path):
        if not p.is_file():
            continue
        try:
            d = _parse(p.read_text())
        except Exception:
            continue
        for r in d.get("triggers") or []:
            if isinstance(r, dict) and r.get("id") and r.get("skill"):
                rows[str(r["id"])] = r
        for gone in d.get("disabled") or []:
            rows.pop(str(gone), None)
    return list(rows.values())


# ---------------------------------------------------------------- matching
def _args(args: Any) -> dict:
    if isinstance(args, dict):
        return args
    try:
        d = json.loads(args or "{}")
        return d if isinstance(d, dict) else {}
    except (TypeError, ValueError):
        return {}


def row_matches(row: dict, tool_name: str, args: dict, result: str) -> bool:
    tools = row.get("tools")
    if tools and tool_name not in tools:
        return False
    cmd_re, res_re = row.get("command_regex"), row.get("result_regex")
    if not (tools or cmd_re or res_re):
        return False
    if cmd_re:
        cmd = args.get("command") if tool_name == "terminal" else args.get("code") or args.get("command")
        if not isinstance(cmd, str) or not re.search(cmd_re, cmd):
            return False
    if res_re and not re.search(res_re, result or ""):
        return False
    return True


# ---------------------------------------------------------------- skills
def _skill_dirs() -> list[Path]:
    dirs = [_home() / "skills"]
    try:
        from hermes_cli.config import load_config
        for d in (load_config().get("skills") or {}).get("external_dirs") or []:
            dirs.append(Path(os.path.expanduser(str(d))))
    except Exception:
        pass
    mods = Path.home() / "code" / "hermes-mods" / "skills"
    if mods.is_dir():
        dirs.append(mods)
    return dirs


def find_skill(name: str, dirs: list[Path] | None = None) -> Path | None:
    for base in dirs if dirs is not None else _skill_dirs():
        if base.is_dir():
            for p in base.glob(f"**/{name}/SKILL.md"):
                return p
    return None


def extract_section(text: str, want: str) -> str | None:
    """A markdown section (heading prefix match, to the next heading of same/higher level) or a **bold** lead-in paragraph."""
    lines = text.splitlines()
    w = want.strip().lower()
    for i, ln in enumerate(lines):
        m = re.match(r"^(#+)\s+(.*?)\s*$", ln)
        if m and m.group(2).lower().startswith(w):
            level, out = len(m.group(1)), [ln]
            for nxt in lines[i + 1:]:
                h = re.match(r"^(#+)\s", nxt)
                if h and len(h.group(1)) <= level:
                    break
                out.append(nxt)
            return "\n".join(out).strip()
    for i, ln in enumerate(lines):
        if ln.lstrip().lower().startswith("**" + w):
            out = []
            for nxt in lines[i:]:
                if not nxt.strip():
                    break
                out.append(nxt)
            return "\n".join(out).strip()
    return None


# ---------------------------------------------------------------- session state
_lock = threading.Lock()
_viewed: dict[str, set[str]] = {}
_fired: dict[str, set[str]] = {}


def _ro_connect(path, timeout=10):
    """Read-only open. mode=ro fails on a WAL db while its -wal/-shm is absent (writers delete them on last close),
    so fall back to mode=rw + query_only, which reads the same data and never writes rows."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        c.execute("select 1 from sqlite_master limit 1")
        return c
    except sqlite3.OperationalError:
        c = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=timeout)
        c.execute("PRAGMA query_only=ON")
        return c


def _viewed_in_db(session_id: str, skill: str) -> bool:
    try:
        con = _ro_connect(_home() / 'state.db', timeout=1)
        try:
            for (tc,) in con.execute(
                    "select tool_calls from messages where session_id=? and tool_calls like '%skill_view%'", (session_id,)):
                for c in json.loads(tc or "[]"):
                    fn = c.get("function") or {}
                    if fn.get("name") == "skill_view" and _args(fn.get("arguments")).get("name") == skill:
                        return True
        finally:
            con.close()
    except Exception:
        pass
    return False


def skill_viewed(session_id: str, skill: str) -> bool:
    with _lock:
        if skill in _viewed.get(session_id, ()):
            return True
    return _viewed_in_db(session_id, skill)


# ---------------------------------------------------------------- hook
def build_injection(row: dict, dirs: list[Path] | None = None) -> str | None:
    path = find_skill(str(row["skill"]), dirs)
    if not path:
        return None
    text = path.read_text()
    parts = [s for s in (extract_section(text, w) for w in row.get("sections") or []) if s]
    if not parts:
        return None
    return (f"\n\n{MARK} You have not loaded skill `{row['skill']}` this session, and this call is governed by it. "
            f"Its rules (shown once):\n\n" + "\n\n".join(parts))


def transform_tool_result(tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "", **_):
    try:
        a = _args(args)
        if tool_name == "skill_view" and session_id and a.get("name"):
            with _lock:
                _viewed.setdefault(session_id, set()).add(str(a["name"]))
            return None
        if not isinstance(result, str) or MARK in result:
            return None
        sid = session_id or "_"
        extra = []
        for row in load_rows():
            if not row_matches(row, tool_name, a, result):
                continue
            with _lock:
                if row["id"] in _fired.get(sid, ()):
                    continue
            if skill_viewed(sid, str(row["skill"])):
                continue
            inj = build_injection(row)
            if inj:
                with _lock:
                    _fired.setdefault(sid, set()).add(row["id"])
                extra.append(inj)
        return result + "".join(extra) if extra else None
    except Exception:
        return None


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", transform_tool_result)
