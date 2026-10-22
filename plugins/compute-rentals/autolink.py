"""Evidence-based session linking -- BACKFILL TOOL ONLY (local registry only).

Not called on inventory reads: tool-call text is weak evidence (sessions that
build or debug tooling mention machines they never rented). Live attribution
is explicit: the rent-machine skill calls compute_rentals_attach per rental.

A running machine is attached to a session only when that session -- or one of
its delegated descendant sessions -- issued a tool call whose arguments name
the machine: its provider_id, its display label, or its exact ssh command.
Inventory-reading compute_rentals_* calls are ignored, as are shared physical
nodes (Slurm / internal prepaid), which never belong to a single session.

The previous behaviour attached *every* running machine across all visible
accounts to whichever session asked, so teammates' machines showed up in
every session.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

MIN_ID_LEN = 6
MIN_LABEL_LEN = 8
MAX_DEPTH = 4
# Tools whose arguments mention a machine without using it (editing files,
# notes, searches) are not evidence of renting/operating that machine.
NON_EVIDENCE_TOOLS = frozenset({
    'write_file', 'patch', 'read_file', 'search_files', 'skill_manage', 'skill_view',
    'memory', 'context_notes', 'todo', 'clarify', 'kanban_comment', 'kanban_complete',
    'kanban_create', 'session_search', 'web_search', 'web_extract',
})

_cache: dict = {}
_cache_lock = threading.Lock()


def state_db_path(home: Path, profile: str) -> Path | None:
    home = Path(home)
    candidates = []
    if home.name == profile or (profile == 'default' and home.parent.name != 'profiles'):
        candidates.append(home / 'state.db')
    candidates.append(home / 'profiles' / profile / 'state.db')
    for path in candidates:
        if path.is_file():
            return path
    return None


def machine_tokens(m: dict) -> list[str]:
    # Any status: evidence ties even a stopped machine (still billing storage)
    # to the session that rented it.
    if m.get('resource_kind') == 'physical_node' or m.get('billing_class') == 'internal_prepaid':
        return []
    tokens = []
    pid = m.get('provider_id') or ''
    if len(pid) >= MIN_ID_LEN:
        tokens.append(pid)
    label = m.get('display_name') or ''
    if len(label) >= MIN_LABEL_LEN and label != pid:
        tokens.append(label)
    ssh = m.get('ssh') or ''
    if ssh:
        tokens.append(ssh)
    return tokens


def _token_re(token: str):
    return re.compile(r'(?<![A-Za-z0-9_\-.])' + re.escape(token) + r'(?![A-Za-z0-9_\-])')


def session_family(db, session_id: str) -> list[str]:
    ids, frontier = [session_id], [session_id]
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        q = 'SELECT id FROM sessions WHERE parent_session_id IN (%s)' % ','.join('?' * len(frontier))
        frontier = [r[0] for r in db.execute(q, frontier) if r[0] not in ids]
        ids.extend(frontier)
    return ids


def _call_text(tool_calls_json: str) -> str:
    try:
        calls = json.loads(tool_calls_json)
    except (TypeError, ValueError):
        return ''
    parts = []
    for call in calls if isinstance(calls, list) else []:
        fn = (call or {}).get('function') or {}
        name = str(fn.get('name', ''))
        if name.startswith('compute_rentals_') or name in NON_EVIDENCE_TOOLS:
            continue
        args = fn.get('arguments')
        if isinstance(args, str):
            try:  # unescape JSON-encoded arguments so tokens match literally
                args = json.dumps(json.loads(args), ensure_ascii=False)
            except ValueError:
                pass
        parts.append(str(args))
    return '\n'.join(parts)


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


def session_evidence_text(db_path: Path, session_id: str) -> str:
    """Concatenated tool-call arguments for the session family, cached by max message id."""
    db = _ro_connect(db_path, timeout=1)
    try:
        family = session_family(db, session_id)
        marks = ','.join('?' * len(family))
        high = db.execute('SELECT MAX(id) FROM messages WHERE session_id IN (%s)' % marks, family).fetchone()[0] or 0
        key = (str(db_path), session_id)
        with _cache_lock:
            cached = _cache.get(key)
            if cached and cached[0] == high and cached[1] == len(family) and cached[3] == NON_EVIDENCE_TOOLS:
                return cached[2]
        rows = db.execute(
            "SELECT tool_calls FROM messages WHERE session_id IN (%s) AND role='assistant' "
            "AND tool_calls IS NOT NULL AND tool_calls != ''" % marks, family)
        text = '\n'.join(_call_text(r[0]) for r in rows)
    finally:
        db.close()
    with _cache_lock:
        _cache[key] = (high, len(family), text, NON_EVIDENCE_TOOLS)
    return text


def evidenced_machines(machines, text: str) -> list[str]:
    keys = []
    if not text:
        return keys
    for m in machines:
        if any(_token_re(t).search(text) for t in machine_tokens(m)):
            keys.append(m['key'])
    return keys


def auto_link(service, session_id: str, profile: str, home: Path | None = None) -> list[str]:
    """Attach evidenced running machines to the session; best-effort, never raises."""
    try:
        if home is None:
            home = service.state_dir.parent
        path = state_db_path(home, profile)
        if path is None:
            return []
        text = session_evidence_text(path, session_id)
        with service._lock:
            machines = [m for ms in service._machines.values() for m in ms.values()]
        keys = evidenced_machines(machines, text)
        for key in keys:
            service.ownership.attach(key, service.gateway_id, profile, session_id, source='auto')
        return keys
    except Exception:
        return []
