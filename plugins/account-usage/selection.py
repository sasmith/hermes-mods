"""Request-local account selection and guarded execution integration.

Never invoke pool loading/seeding/reclaim on a manual selection. No tokens are
serialized; a dispatch snapshot belongs to exactly one execution context.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextvars import ContextVar
from functools import wraps
import time

# Context is copied by core into request workers. Resetting the caller's context
# does not erase a snapshot held by an abandoned worker. Never patch an instance
# method temporarily or rely on the mutable pool.current() pointer.
_dispatch = ContextVar('account_usage_dispatch', default=None)
_install_failures = {}
# Profile homes whose plugin load overran core's deadline in THIS process: no
# middleware will ever consume a queued choice here, so GET rejects it visibly.
_load_abandoned = {}
_unloaded = []  # set once any integration unloads; install() then gc.collect()s

# install() proves each seam's live code object came from the audited file. A
# cold compile of those files costs seconds under load (it used to push plugin
# load past core's 10 s deadline), so the code fingerprints of each audited file
# are cached per (interpreter, file digest). A miss or mismatch falls back to the
# compile, which stays authoritative; the cache can only make a check faster.
_FINGERPRINT_VERSION = 1


def _fingerprint_cache_path(home):
    from pathlib import Path
    return Path(home) / 'cache' / 'account-usage' / 'code-fingerprints.json'


def _canonical(value):
    import types
    if isinstance(value, types.CodeType):
        return ('code', _code_fingerprint(value))
    if isinstance(value, tuple):
        return ('tuple', tuple(_canonical(v) for v in value))
    if isinstance(value, frozenset):  # repr order follows string hash randomization
        return ('frozenset', tuple(sorted(repr(_canonical(v)) for v in value)))
    return (type(value).__qualname__, repr(value))


def _code_fingerprint(code):
    """Process-independent digest of everything code equality compares (and more)."""
    import hashlib
    parts = (code.co_name, code.co_qualname, code.co_argcount, code.co_posonlyargcount,
             code.co_kwonlyargcount, code.co_flags, code.co_firstlineno, code.co_code,
             code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars,
             code.co_linetable, code.co_exceptiontable,
             tuple(_canonical(c) for c in code.co_consts))
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def _cache_key(relative, digest):
    import sys
    return f'{_FINGERPRINT_VERSION}|{sys.implementation.cache_tag}|{sys.version}|{relative}|{digest}'
_IDENTITY_HEADERS = frozenset({'authorization', 'x-api-key', 'chatgpt-account-id',
    'x-openai-internal-codex-residency', 'openai-organization', 'openai-project'})


def reject_identity_headers(headers):
    if any(str(key).lower() in _IDENTITY_HEADERS for key in (headers or {})):
        raise SelectionRejected('Account selection cannot override explicit request identity headers')


def current_snapshot(agent):
    bound = _dispatch.get()
    return bound[1] if bound is not None and bound[0] is agent else None


def clear_attempt(agent):
    # Called at EVERY next execution boundary, including unselected retries.
    agent.__dict__.pop('_account_usage_failed_attempt', None)


def failure_identity(original):
    @wraps(original)
    def identity(agent, pool):
        snapshot = getattr(agent, '_account_usage_failed_attempt', None)
        if (snapshot is not None and pool.provider == snapshot.provider
                and (agent.provider, agent.model, agent.base_url.rstrip('/')) ==
                (snapshot.provider, snapshot.model, snapshot.base_url.rstrip('/'))):
            return snapshot.token, snapshot.entry_id
        return original(agent, pool)
    return identity


def dispatch_snapshot(agent, snapshot, next_call, request):
    clear_attempt(agent)
    reject_identity_headers(request.get('extra_headers'))
    if request.get('model', snapshot.model) != snapshot.model:
        raise SelectionRejected('Request model changed before dispatch')
    if (agent.provider, agent.model, agent.base_url.rstrip('/')) != (
            snapshot.provider, snapshot.model, snapshot.base_url.rstrip('/')):
        raise SelectionRejected('Provider, model, or endpoint changed before dispatch')
    token = _dispatch.set((agent, snapshot))
    try:
        return next_call(request)
    except BaseException as exc:
        # Attribution is separate from client credentials. A local preflight
        # refusal is not a failed account and must never bench that credential.
        from hermes_cli.middleware import _DownstreamExecutionError
        cause = exc
        while isinstance(cause, _DownstreamExecutionError):
            cause = cause.original
        if not isinstance(cause, SelectionRejected):
            agent._account_usage_failed_attempt = snapshot
        raise
    finally:
        _dispatch.reset(token)


def anthropic_factory(original):
    """Build a genuinely request-local client; no singleton resolver or shared swap.

    Client is intentionally not in the reuse slot: the core request registry
    retains responsibility for abort and owner-thread close. Unselected calls
    use the original method byte-for-byte.
    """
    @wraps(original)
    def create(agent, *, reason):
        snapshot = current_snapshot(agent)
        if snapshot is None:
            return original(agent, reason=reason)
        from agent.client_lifecycle import get_provider_request_timeout
        key = ('direct', snapshot.token, snapshot.base_url,
               get_provider_request_timeout(snapshot.provider, snapshot.model),
               bool(getattr(agent, '_oauth_1m_beta_disabled', False)))
        client = agent._build_anthropic_client_for_key(key)
        # The core builder allows configured headers to override SDK auth. Check
        # the constructed client (not a second config read) before returning it
        # to the request registry. No socket has been opened at this point.
        headers = {str(k).lower(): v for k, v in client.default_headers.items()}
        from agent.anthropic_credentials import _is_oauth_token
        expected = ('authorization', f'Bearer {snapshot.token}') if _is_oauth_token(
            snapshot.token) else ('x-api-key', snapshot.token)
        other = 'x-api-key' if expected[0] == 'authorization' else 'authorization'
        if headers.get(expected[0]) != expected[1] or isinstance(headers.get(other), str):
            client.close()
            raise SelectionRejected('Configured Anthropic headers conflict with the selected account')
        return client
    return create


def codex_factory(original):
    @wraps(original)
    def create(agent, *, reason, api_kwargs=None):
        snapshot = current_snapshot(agent)
        if snapshot is None:
            return original(agent, reason=reason, api_kwargs=api_kwargs)
        from agent.codex_headers import codex_cloudflare_headers
        kwargs = dict(agent._client_kwargs)
        if any(kwargs.get(key) for key in ('organization', 'project', 'http_client')):
            raise SelectionRejected('Selected account cannot use an inherited organization, project, or custom HTTP client')
        identity_headers = {'authorization', 'x-api-key', 'chatgpt-account-id',
                            'x-openai-internal-codex-residency', 'openai-organization', 'openai-project'}
        headers = {k: v for k, v in (kwargs.get('default_headers') or {}).items()
                   if k.lower() not in identity_headers}
        headers.update(codex_cloudflare_headers(snapshot.token, base_url=snapshot.base_url))
        kwargs.update(api_key=snapshot.token, base_url=snapshot.base_url,
                      max_retries=0, default_headers=headers)
        client = agent._create_openai_client(kwargs, reason=reason, shared=False)
        actual = {str(k).lower(): v for k, v in client.default_headers.items()}
        expected = {str(k).lower(): v for k, v in codex_cloudflare_headers(snapshot.token, base_url=snapshot.base_url).items()}
        conflicts = actual.get('authorization') != f'Bearer {snapshot.token}'
        conflicts |= any(actual.get(k) != v for k, v in expected.items() if k in identity_headers)
        conflicts |= any(isinstance(actual.get(k), str) and bool(actual[k])
                         for k in ('x-api-key', 'openai-organization', 'openai-project'))
        if conflicts:
            client.close()
            raise SelectionRejected('Configured Codex headers conflict with the selected account')
        return client
    return create


class SelectionRejected(ValueError):
    """Safe, user-visible rejection (never include credential material)."""


class SelectionConflict(SelectionRejected):
    """The selection changed since this tab last read it."""


class PendingStore:
    """Process-safe CAS and consume on one local host (including compute children).

    The three-part key MUST be supplied by an authoritative ownership resolver:
    (canonical profile home, durable session ID, provider). This class alone does
    not authenticate a runtime session or transfer ownership after compression.
    Tombstones preserve revision monotonicity after cancel/consume (no ABA race).
    """
    def __init__(self, path):
        from pathlib import Path
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from contextlib import closing
        with closing(self._connection()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS selection (
                profile TEXT NOT NULL, session TEXT NOT NULL, provider TEXT NOT NULL,
                revision INTEGER NOT NULL, model TEXT, account_id TEXT, state TEXT NOT NULL,
                PRIMARY KEY(profile, session, provider))''')

    def _connection(self):
        import sqlite3
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _read(db, key):
        row = db.execute('SELECT revision, model, account_id, state FROM selection '
                         'WHERE profile=? AND session=? AND provider=?', key).fetchone()
        return dict(row) if row else dict(revision=0, model=None, account_id=None, state='empty')

    def read(self, key):
        from contextlib import closing
        with closing(self._connection()) as db, db:
            return self._read(db, key)

    def has_pending(self, profile):
        """Whether ANY chat in *profile* has a queued selection (no owner needed)."""
        from contextlib import closing
        with closing(self._connection()) as db, db:
            return db.execute("SELECT 1 FROM selection WHERE profile=? AND state='pending' LIMIT 1",
                              (profile,)).fetchone() is not None

    @staticmethod
    def _write(db, key, row):
        db.execute('INSERT OR REPLACE INTO selection VALUES (?,?,?,?,?,?,?)',
                   (*key, row['revision'], row['model'], row['account_id'], row['state']))

    def _change(self, key, expected_revision, *, model=None, account_id=None):
        from contextlib import closing
        with closing(self._connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = self._read(db, key)
            if old['revision'] != expected_revision:
                raise SelectionConflict('Selection changed in another tab; refresh before retrying')
            row = dict(revision=old['revision'] + 1, model=model, account_id=account_id,
                       state='pending' if account_id is not None else 'cancelled')
            self._write(db, key, row)
            return row

    def replace(self, key, *, expected_revision, model, account_id):
        if not model or not account_id:
            raise SelectionRejected('Model and account identity are required')
        return self._change(key, expected_revision, model=model, account_id=account_id)

    def cancel(self, key, *, expected_revision):
        return self._change(key, expected_revision)

    def invalidate_route(self, key, *, model):
        """Terminally reject old routes within this runtime conversation only."""
        from contextlib import closing
        with closing(self._connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE selection SET state='rejected', revision=revision+1 "
                "WHERE profile=? AND session=? AND state='pending' AND (provider<>? OR model<>?)",
                (*key, model)).rowcount
            return bool(changed)

    def reject_pending(self, key):
        """Terminally reject this key's queued choice (visible on the next read)."""
        from contextlib import closing
        with closing(self._connection()) as db, db:
            return bool(db.execute("UPDATE selection SET state='rejected', revision=revision+1 "
                "WHERE profile=? AND session=? AND provider=? AND state='pending'", key).rowcount)

    def reject_consumed(self, key, *, revision):
        # Do not clobber a newer click which arrived while credential I/O ran.
        from contextlib import closing
        with closing(self._connection()) as db, db:
            db.execute("UPDATE selection SET state='rejected', revision=revision+1 "
                "WHERE profile=? AND session=? AND provider=? AND revision=? AND state='consumed'", (*key, revision))

    def consume(self, key, *, model):
        from contextlib import closing
        with closing(self._connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = self._read(db, key)
            if row['state'] != 'pending' or row['model'] != model:
                return None
            self._write(db, key, {**row, 'revision': row['revision'] + 1, 'state': 'consumed'})
            return row


@dataclass(frozen=True)
class CredentialSnapshot:
    provider: str
    model: str
    account_id: str
    entry_id: str
    base_url: str
    token: str = field(repr=False)


def choose_snapshot(entries, *, provider, model, account_id, base_url):
    """Validate an authoritative, already-hydrated read-only row snapshot.

    Caller supplies fresh persisted rows, NOT load_pool's seeded/mutated view.
    Ambiguous routes and expired/unhydrated credentials fail closed. No refresh,
    quota probe, priority change, cooldown reset or pool-current mutation occurs.
    """
    from agent.account_usage_cache import _identity_id_for
    from agent.credential_pool import _exhausted_until, _parse_absolute_timestamp
    from agent.credential_pool_model_cooldowns import model_cooldown_until
    from hermes_cli.route_identity import normalize_route_base_url

    if provider not in {'anthropic', 'openai-codex'} or not model:
        raise SelectionRejected('Unsupported provider or model')
    rows = list(entries)
    matched = [e for e in rows if e.provider == provider
               and _identity_id_for(provider, e) == account_id]
    if not matched:
        raise SelectionRejected('Account was removed or its identity changed; refresh usage')
    now = time.time()
    sole = len([e for e in rows if e.last_status != 'dead']) <= 1
    viable = []
    for e in matched:
        route = e.runtime_base_url or base_url
        if normalize_route_base_url(route) != normalize_route_base_url(base_url):
            continue
        if e.last_status == 'dead':
            continue
        until = _exhausted_until(e, sole_credential=sole)
        if until is not None and until > now or model_cooldown_until(e, model) is not None:
            raise SelectionRejected('Account is cooling down for this model')
        if e.expires_at_ms is not None and e.expires_at_ms / 1000 <= now + 30:
            continue
        if e.expires_at:
            expiry = _parse_absolute_timestamp(e.expires_at)
            if expiry is None or expiry <= now + 30:
                continue
        if e.runtime_api_key:
            # Expiry columns may be missing on imported device-code rows. JWT
            # claims are useful only as a rejection hint, never as auth proof.
            from agent.credential_pool import _decode_jwt_claims
            import math
            expiry = _decode_jwt_claims(e.runtime_api_key).get('exp')
            if expiry is not None:
                try:
                    expiry = float(expiry)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(expiry) or expiry <= now + 30:
                    continue
            viable.append(e)
    if not viable:
        raise SelectionRejected('Account is unavailable, expired, or uses a different endpoint')
    # IDs, never labels; deterministic across processes without editing priorities.
    chosen = min(viable, key=lambda e: (e.priority, e.id))
    return CredentialSnapshot(provider, model, account_id, chosen.id,
                              chosen.runtime_base_url or base_url, chosen.runtime_api_key)


@dataclass(frozen=True)
class SessionOwner:
    profile_home: str
    runtime_id: str
    durable_id: str
    conversation_id: str
    provider: str
    model: str
    base_url: str

    @property
    def key(self):
        # A new chat in the same UI runtime must not inherit a previous intent.
        # A second runtime resuming the durable conversation must not steal it.
        import json
        return (self.profile_home, json.dumps([self.runtime_id, self.conversation_id]), self.provider)


def _runtime_server(server=None):
    import sys
    server = server if server is not None else sys.modules.get('tui_gateway.server')
    if server is None or not hasattr(server, '_sessions_lock'):
        raise SelectionRejected('Account selection requires the chat-owning gateway; reconnect this chat')
    return server


def _ro_connect(path, timeout=10):
    """Read-only open. mode=ro fails on a WAL db while its -wal/-shm is absent (writers delete them on last close),
    so fall back to mode=rw + query_only, which reads the same data and never writes rows."""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        c.execute("select 1 from sqlite_master limit 1")
        return c
    except sqlite3.OperationalError:
        c = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=timeout)
        c.execute("PRAGMA query_only=ON")
        return c


def _continuation_chains(home, ids):
    """Read-only compression aliases; explicit branch/reset/delegate edges stop.

    Uses the installed core fork classifier, matching session-turn lease semantics.
    No SessionDB constructor/migration, and no inference from titles or timestamps.
    """
    import json
    import sqlite3
    from pathlib import Path
    from contextlib import closing
    from hermes_state_messages import SessionMessagesMixin
    path = Path(home) / 'state.db'
    if not path.exists():
        return {sid: [sid] for sid in ids}
    with closing(_ro_connect(path, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        def row(sid):
            result = db.execute('SELECT id,parent_session_id,source,model_config,end_reason '
                                'FROM sessions WHERE id=?', (sid,)).fetchone()
            return dict(result) if result else None
        chains = {}
        for sid in ids:
            chain, current = [sid], row(sid)
            while current:
                cfg = current.get('model_config')
                if isinstance(cfg, str):
                    try:
                        current['model_config'] = json.loads(cfg)
                    except ValueError:
                        raise SelectionRejected('Chat continuation metadata is unreadable') from None
                parent_id = current.get('parent_session_id')
                if not parent_id or SessionMessagesMixin._is_explicit_fork_child_row(
                        None, current, include_reset=True):
                    break
                parent = row(parent_id)
                if not parent or parent.get('end_reason') != 'compression':
                    break
                children = db.execute('SELECT id,parent_session_id,source,model_config,end_reason '
                                      'FROM sessions WHERE parent_session_id=?', (parent_id,)).fetchall()
                continuation_ids = []
                for child in children:
                    child = dict(child)
                    if isinstance(child.get('model_config'), str):
                        try:
                            child['model_config'] = json.loads(child['model_config'])
                        except ValueError:
                            raise SelectionRejected('Chat continuation metadata is unreadable') from None
                    if not SessionMessagesMixin._is_explicit_fork_child_row(None, child, include_reset=True):
                        continuation_ids.append(child['id'])
                if continuation_ids != [current['id']]:
                    raise SelectionRejected('Chat has ambiguous compression continuations')
                if parent_id in chain or len(chain) >= 256:
                    raise SelectionRejected('Chat continuation metadata is ambiguous')
                chain.append(parent_id)
                current = parent
            chains[sid] = chain
        return chains


def _mirror_route(home, session):
    """Parent has no live agent under compute isolation. Cross-check its core
    IPC mirror against the child's persisted runtime route; never build/resolve
    an agent or invoke default credential resolvers just to answer a click.
    """
    import json
    import sqlite3
    from contextlib import closing
    from pathlib import Path
    from types import SimpleNamespace
    mirror = session.get('_metadata_mirror') or {}
    key = str(session.get('session_key') or '')
    path = Path(home) / 'state.db'
    if not key or not path.exists():
        raise SelectionRejected('Compute chat metadata is not ready; retry after the first turn')
    with closing(_ro_connect(path, timeout=5)) as db:
        row = db.execute('SELECT model,model_config FROM sessions WHERE id=?', (key,)).fetchone()
    if not row:
        raise SelectionRejected('Compute chat metadata is unavailable')
    config = json.loads(row[1] or '{}')
    if (not isinstance(config, dict) or not config.get('base_url')
            or mirror.get('provider') != config.get('provider') or mirror.get('model') != row[0]):
        raise SelectionRejected('Compute chat route is changing; retry after metadata settles')
    return SimpleNamespace(session_id=key, provider=mirror['provider'], model=mirror['model'],
                           base_url=config['base_url'])


def _resolve_owner_locked(runtime_id, *, profile_home, server, agent=None):
    import os
    from pathlib import Path
    from hermes_cli import active_sessions as registry
    home = str(Path(profile_home).resolve())
    if agent is not None:
        matches = [(sid, row) for sid, row in server._sessions.items() if row.get('agent') is agent]
        if len(matches) != 1 or runtime_id is not None and runtime_id != matches[0][0]:
            raise SelectionRejected('Account selection is only available to the exact main chat agent')
        runtime_id, session = matches[0]
    else:
        session = server._sessions.get(runtime_id)
    if not session or session.get('_finalized') or session.get('_lease_taken_over'):
        raise SelectionRejected('Chat is not owned by this runtime; reconnect and retry')
    launch_home = getattr(server, '_hermes_home', None)
    actual_home = session.get('profile_home') or launch_home
    if actual_home is None or str(Path(actual_home).resolve()) != home:
        raise SelectionRejected('Chat belongs to a different profile')
    main = session.get('agent')
    mirrored = bool(session.get('_compute_host_active'))
    if mirrored and agent is not None:
        raise SelectionRejected('Requests belong to the compute child, not its parent mirror')
    if mirrored:
        main = _mirror_route(home, session)
    if main is None:
        raise SelectionRejected('Chat model is still loading; retry when it is ready')
    if session.get('pending_model_switch'):
        raise SelectionRejected('Chat model change is pending; retry after it completes')
    durable = str(session.get('session_key') or '')
    agent_id = str(getattr(main, 'session_id', '') or '')
    if not durable or not agent_id or session.get('source') not in {'desktop', 'tui'}:
        raise SelectionRejected('Account selection requires a live main chat')
    # Do not call registry_snapshot: its dead-entry pruning writes the registry.
    entries = registry._read_entries(registry._state_path(home), strict=True)
    lease = session.get('active_session_lease')
    holder = None
    if lease is not None:
        borrowed = not lease.enabled and str(lease.lease_id).startswith('borrowed:')
        if lease.released or not lease.enabled and not borrowed:
            raise SelectionRejected('Chat ownership lease is not current')
        lease_id = lease.lease_id.removeprefix('borrowed:') if borrowed else lease.lease_id
        matches = [e for e in entries if e.get('lease_id') == lease_id]
        if len(matches) != 1:
            raise SelectionRejected('Chat ownership lease is unavailable')
        holder = matches[0]
        if (not borrowed and holder.get('pid') != os.getpid()
                or holder.get('metadata', {}).get('live_session_id') != runtime_id
                or registry._pid_liveness(holder.get('pid'), holder.get('process_start_time')) is not True):
            raise SelectionRejected('Chat has a different runtime owner')
    elif session.get('running') or any(e.get('session_id') == durable for e in entries):
        raise SelectionRejected('Chat ownership cannot be proven')
    ids = {durable, agent_id}
    if holder is not None:
        ids.update((str(holder['session_id']), str(lease.session_id)))
    chains = _continuation_chains(home, ids)
    # Parent mirror and child may briefly name different segments. Every id
    # must be on ONE ancestor chain, not merely share a root (forks are distinct).
    tip = max(ids, key=lambda sid: len(chains[sid]))
    if not ids.issubset(chains[tip]):
        raise SelectionRejected('Chat continuation does not belong to this runtime lease')
    durable = tip
    conversation_id = chains[tip][-1]
    # Before the first turn core intentionally has no registry lease. The live,
    # profile-checked server record is authoritative then; do not claim a slot.
    provider, model, base_url = (getattr(main, k, '') for k in ('provider', 'model', 'base_url'))
    if not provider or not model or not base_url:
        raise SelectionRejected('Account selection is unavailable for this chat route')
    return SessionOwner(home, runtime_id, durable, conversation_id, provider, model, base_url)


from contextlib import contextmanager


@contextmanager
def owner_scope(runtime_id=None, *, profile_home, server=None, agent=None):
    """Resolve and hold the owning runtime registry lock through a short operation.

    Execution passes the exact main agent, NOT a session id inferred from a stack.
    REST passes only its runtime id and the request-scoped server profile home.
    Never hold this scope through provider I/O or downstream execution.
    """
    server = _runtime_server(server)
    with server._sessions_lock:
        yield _resolve_owner_locked(runtime_id, profile_home=profile_home, server=server, agent=agent)


def resolve_owner(runtime_id=None, *, profile_home, server=None, agent=None):
    with owner_scope(runtime_id, profile_home=profile_home, server=server, agent=agent) as owner:
        return owner


def _read_json(path):
    """Never use auth._load_auth_store: malformed JSON makes it write a backup."""
    import json
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        raise SelectionRejected('Credential metadata is unreadable; repair it before selecting an account') from None
    if not isinstance(data, dict):
        raise SelectionRejected('Credential metadata is invalid')
    return data


def read_snapshot(owner, account_id):
    """Fresh disk rows plus read-only borrowed-source hydration; no seed/refresh.

    Must run in the request/turn's existing home AND secret scope. Never returns
    tokens to REST. Removed sources remain removed; only persisted rows qualify.
    """
    from pathlib import Path
    from hermes_constants import get_hermes_home, get_default_hermes_root
    from agent.credential_pool import PooledCredential, get_env_prefer_dotenv
    if str(Path(get_hermes_home()).resolve()) != owner.profile_home:
        raise SelectionRejected('Credential profile scope does not match this chat')
    home = Path(owner.profile_home)
    local = _read_json(home / 'auth.json')
    source_home, store = home, local
    def provider_rows(data):
        pool = data.get('credential_pool', {})
        if not isinstance(pool, dict):
            raise SelectionRejected('Credential pool metadata is invalid')
        rows = pool.get(owner.provider, [])
        if not isinstance(rows, list):
            raise SelectionRejected('Credential pool metadata is invalid')
        return rows
    rows = provider_rows(store)
    if not rows:
        root = Path(get_default_hermes_root()).resolve()
        if root != home:
            source_home, store = root, _read_json(root / 'auth.json')
            rows = provider_rows(store)
    suppressed = set(local.get('suppressed_sources', {}).get(owner.provider, []))
    suppressed.update(store.get('suppressed_sources', {}).get(owner.provider, []))
    entries, seen = [], set()
    borrowed = {}
    for raw in rows:
        if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not raw['id'] or raw['id'] in seen:
            raise SelectionRejected('Credential row identities are missing or ambiguous')
        seen.add(raw['id'])
        source = raw.get('source', 'manual')
        if source in suppressed:
            continue
        payload = dict(raw)
        from agent.account_usage_cache import _identity_id_for
        from agent.credential_persistence import is_borrowed_credential_source
        persisted = PooledCredential.from_dict(owner.provider, payload)
        fingerprint = raw.get('secret_fingerprint', '')
        persisted_identity = _identity_id_for(owner.provider, persisted)
        if not persisted.runtime_api_key and isinstance(fingerprint, str) and fingerprint.startswith('sha256:'):
            persisted_identity = 'fp:' + fingerprint.removeprefix('sha256:')
        needs_hydration = (is_borrowed_credential_source(source, owner.provider)
                           or owner.provider == 'anthropic' and source == 'hermes_pkce')
        if needs_hydration and persisted_identity != account_id:
            # Retain status for sole-credential cooldown policy, but do not read
            # another account's Keychain/env/singleton just to handle this click.
            payload['access_token'] = ''
            entries.append(PooledCredential.from_dict(owner.provider, payload))
            continue
        if owner.provider == 'anthropic' and source in {'hermes_pkce', 'claude_code'}:
            if source not in borrowed:
                if source == 'hermes_pkce':
                    borrowed[source] = _read_json(source_home / '.anthropic_oauth.json')
                else:
                    # Core reader respects adopt_external_logins, reads Keychain/file,
                    # and never refreshes or mirrors credentials.
                    from agent.anthropic_credentials import read_claude_code_credentials
                    borrowed[source] = read_claude_code_credentials() or {}
            creds = borrowed[source]
            payload.update(access_token=creds.get('accessToken', ''),
                           expires_at_ms=creds.get('expiresAt'), expires_at=None)
        elif isinstance(source, str) and source.startswith('env:'):
            payload['access_token'] = get_env_prefer_dotenv(source[4:])
            if str(payload['access_token']).startswith(('op://', 'bw://')):
                payload['access_token'] = ''
        elif needs_hydration:
            payload['access_token'] = ''
        # Unknown borrowed sources never use a legacy embedded secret or default
        # resolver that could silently select a sibling.
        entries.append(PooledCredential.from_dict(owner.provider, payload))
    return choose_snapshot(entries, provider=owner.provider, model=owner.model,
                           account_id=account_id, base_url=owner.base_url)


class ExecutionIntegration:
    """Guarded synchronous execution adapter. No cross-thread/cached agent lookup."""
    def __init__(self, observe):
        self.observe = observe
        from pathlib import Path
        from hermes_constants import get_hermes_home
        from agent import turn_api_call
        self.home = str(Path(get_hermes_home()).resolve())
        self.call_code = turn_api_call.perform_api_call.__code__
        self.error = None
        self.patches = []
        self.active = False
        import threading
        self._lifetime_lock = threading.RLock()
        self._snapshots = set()

    def retain_snapshot(self, snapshot):
        import weakref
        key = id(snapshot)
        with self._lifetime_lock:
            if key not in self._snapshots:
                self._snapshots.add(key)
                weakref.finalize(snapshot, self._release_snapshot, key)

    def _release_snapshot(self, key):
        with self._lifetime_lock:
            self._snapshots.discard(key)
            if not self.active:
                self._restore_if_idle()

    def install(self):
        if _unloaded:
            import gc
            gc.collect()  # release completed exception tracebacks from a prior unload
        import hashlib
        import json
        import inspect
        from pathlib import Path
        from agent import client_lifecycle, turn_api_call, agent_runtime_helpers, chat_completion_helpers
        root = Path(client_lifecycle.__file__).resolve().parents[1]
        expected = json.loads(Path(__file__).with_name('execution-compatibility.json').read_text())
        for relative, digest in expected.items():
            if hashlib.sha256((root / relative).read_bytes()).hexdigest() != digest:
                raise SelectionRejected('Account selection compatibility changed; re-audit the execution plugin')
        self.call_code = turn_api_call.perform_api_call.__code__
        targets = [(client_lifecycle.ClientLifecycleMixin, '_create_request_anthropic_client', anthropic_factory),
                   (client_lifecycle.ClientLifecycleMixin, '_create_request_openai_client', codex_factory),
                   (agent_runtime_helpers, 'recover_with_credential_pool', self.recovery_wrapper),
                   (agent_runtime_helpers, '_failed_credential_identity', failure_identity),
                   (agent_runtime_helpers, 'switch_model', self.route_wrapper),
                   (chat_completion_helpers, 'interruptible_api_call', self.terminal_wrapper),
                   (chat_completion_helpers, 'interruptible_streaming_api_call', self.terminal_wrapper)]
        # Validate every target BEFORE installing anything. Refuse other wrappers.
        compiled = {}
        cache_path = _fingerprint_cache_path(self.home)
        try:
            cache = json.loads(cache_path.read_text())
            if not isinstance(cache, dict) or not isinstance(cache.get('files'), dict):
                raise ValueError
        except (OSError, ValueError):
            cache = {'files': {}}
        dirty = []
        def codes(code):
            import types
            yield code
            for child in code.co_consts:
                if isinstance(child, types.CodeType):
                    yield from codes(child)
        def verified(original):
            if not inspect.isfunction(original) or hasattr(original, '__wrapped__'):
                return False
            path = Path(original.__code__.co_filename).resolve()
            try:
                relative = str(path.relative_to(root))
            except ValueError:
                return False
            if relative not in expected:
                return False
            # The file's bytes were pinned above; the cache maps that digest to
            # the fingerprints of every code object compiling it yields.
            key = _cache_key(relative, expected[relative])
            known = cache['files'].get(key)
            if isinstance(known, list) and _code_fingerprint(original.__code__) in known:
                return True
            if relative not in compiled:
                compiled[relative] = compile(path.read_bytes(), str(path), 'exec', dont_inherit=True)
                cache['files'][key] = sorted({_code_fingerprint(c) for c in codes(compiled[relative])})
                dirty.append(key)
            return any(code == original.__code__ for code in codes(compiled[relative]))
        if not verified(turn_api_call.perform_api_call):
            raise SelectionRejected('Account selection execution boundary is customized')
        for obj, name, factory in targets:
            if not verified(getattr(obj, name)):
                raise SelectionRejected(f'Account selection execution seam is already customized: {name}')
        if dirty:
            pins = {f'|{relative}|{digest}' for relative, digest in expected.items()}
            self._save_fingerprints(cache_path, cache, pins)
        # Cover already-imported aliases as well as the defining module. Core's
        # class forwarders resolve these module attributes dynamically.
        import sys
        aliases = []
        originals = [(getattr(obj, name), factory) for obj, name, factory in targets]
        for module_name, module in list(sys.modules.items()):
            if module is None or not (module_name.startswith('agent.') or module_name == 'run_agent'):
                continue
            for name, value in list(vars(module).items()):
                for original, factory in originals:
                    if value is original and not any(obj is module and attr == name for obj, attr, _ in targets):
                        aliases.append((module, name, factory))
        wrappers = {}
        for obj, name, factory in targets + aliases:
            original = getattr(obj, name)
            wrapper = wrappers.setdefault(original, factory(original))
            self.patches.append((obj, name, original, wrapper))
            setattr(obj, name, wrapper)
        self.active = True
        _install_failures.pop(self.home, None)

    @staticmethod
    def _save_fingerprints(path, cache, pins):
        # Merge with what other processes wrote meanwhile, keep entries for the
        # current pins only (any interpreter), drop stale core versions. A write
        # failure only costs the next process a compile.
        import json
        import os
        try:
            try:
                old = json.loads(path.read_text()).get('files', {})
            except (OSError, ValueError, AttributeError):
                old = {}
            merged = {**(old if isinstance(old, dict) else {}), **cache['files']}
            files = {k: v for k, v in merged.items()
                     if isinstance(v, list) and any(k.endswith(pin) for pin in pins)}
            path.parent.mkdir(parents=True, exist_ok=True)
            import threading
            tmp = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
            tmp.write_text(json.dumps({'files': files}))
            os.replace(tmp, path)
        except Exception:
            pass

    def disable(self):
        self.error = 'Account selection compatibility check failed; reload after re-auditing the plugin'
        _install_failures[self.home] = self.error

    def abandon(self):
        """Core abandoned this plugin's load (deadline overrun): its middleware and
        on_unload are never registered, so undo every patch now and refuse queued
        and new selections in this process instead of silently ignoring them."""
        message = ('Account selection is unavailable: the account-usage plugin did not finish '
                   'loading in this Hermes process; restart Hermes and choose again')
        _install_failures[self.home] = message
        _load_abandoned[self.home] = message
        self.error = message
        self.unload()

    def route_wrapper(self, original):
        @wraps(original)
        def switch(agent, *args, **kwargs):
            import sys
            from pathlib import Path
            from hermes_constants import get_hermes_home
            server = sys.modules.get('tui_gateway.server')
            home = str(Path(get_hermes_home()).resolve())
            runtime_id = None
            if server is not None:
                with server._sessions_lock:
                    matches = [sid for sid, row in server._sessions.items()
                        if row.get('agent') is agent and str(Path(row.get('profile_home') or server._hermes_home).resolve()) == home]
                    if len(matches) == 1:
                        runtime_id = matches[0]
            before = tuple(getattr(agent, k, '') for k in ('provider', 'model', 'base_url'))
            try:
                return original(agent, *args, **kwargs)
            finally:
                after = tuple(getattr(agent, k, '') for k in ('provider', 'model', 'base_url'))
                path = Path(home) / 'account_usage_selection.sqlite3'
                if runtime_id is not None and before != after and path.exists():
                    # Rejection only, never infer a new dispatch owner. This also
                    # works while pending_model_switch makes dispatch ineligible.
                    from contextlib import closing
                    store = PendingStore(path)
                    with closing(store._connection()) as db, db:
                        db.execute("UPDATE selection SET state='rejected', revision=revision+1 "
                            "WHERE profile=? AND json_extract(session, '$[0]')=? AND state='pending'",
                            (home, runtime_id))
        return switch

    def terminal_wrapper(self, original):
        @wraps(original)
        def terminal(agent, api_kwargs, **kwargs):
            snapshot = current_snapshot(agent)
            if snapshot is not None:
                reject_identity_headers(api_kwargs.get('extra_headers'))
                extra = api_kwargs.get('extra_body') or {}
                if api_kwargs.get('model', snapshot.model) != snapshot.model or extra.get('model', snapshot.model) != snapshot.model:
                    raise SelectionRejected('Request model changed after account selection')
                if (agent.provider, agent.model, agent.base_url.rstrip('/')) != (snapshot.provider, snapshot.model, snapshot.base_url.rstrip('/')):
                    raise SelectionRejected('Request route changed after account selection')
            return original(agent, api_kwargs, **kwargs)
        return terminal

    def recovery_wrapper(self, original):
        @wraps(original)
        def recover(agent, *, status_code, has_retried_429, classified_reason=None,
                    error_context=None, billing_unverified=False):
            snapshot = getattr(agent, '_account_usage_failed_attempt', None)
            if snapshot is None:
                return original(agent, status_code=status_code, has_retried_429=has_retried_429,
                    classified_reason=classified_reason, error_context=error_context,
                    billing_unverified=billing_unverified)
            # A manual choice is one attempt, never a sticky same-account retry.
            # Normal pool accounting/rotation is retained, but the next credential
            # is another request-local snapshot, not an unsafe live-agent swap.
            from agent.error_classifier import FailoverReason
            from agent.agent_runtime_helpers import _STATUS_TO_FAILOVER_REASON
            from agent.account_usage_cache import _identity_id_for
            reason = classified_reason or _STATUS_TO_FAILOVER_REASON.get(status_code)
            pool = getattr(agent, '_credential_pool', None)
            if (pool is None or pool.provider != snapshot.provider or
                    (agent.provider, agent.model, agent.base_url.rstrip('/')) !=
                    (snapshot.provider, snapshot.model, snapshot.base_url.rstrip('/'))):
                return False, has_retried_429
            if reason not in {FailoverReason.rate_limit, FailoverReason.billing,
                              FailoverReason.auth, FailoverReason.model_entitlement}:
                return False, has_retried_429
            failure = reason.value
            if reason == FailoverReason.billing and billing_unverified:
                from agent.credential_pool import FAILURE_REASON_BILLING_UNVERIFIED
                failure = FAILURE_REASON_BILLING_UNVERIFIED
            default_status = {FailoverReason.billing: 402, FailoverReason.auth: 401,
                              FailoverReason.rate_limit: 429, FailoverReason.model_entitlement: 400}[reason]
            row = pool.mark_exhausted_and_rotate(status_code=status_code if status_code is not None else default_status,
                error_context=error_context, api_key_hint=snapshot.token, credential_id=snapshot.entry_id,
                failure_reason=failure, model=snapshot.model)
            clear_attempt(agent)
            if row is None:
                return False, has_retried_429
            try:
                next_snapshot = choose_snapshot(pool.entries(), provider=snapshot.provider,
                    model=snapshot.model, account_id=_identity_id_for(snapshot.provider, row),
                    base_url=snapshot.base_url)
            except SelectionRejected:
                return False, has_retried_429
            agent._account_usage_retry_snapshot = (getattr(agent, '_account_usage_turn', None), next_snapshot)
            return True, False
        # Audited peer guards may verify exact ownership; never an unwrapping
        # permission. The patch ledger and pinned live code must also match.
        recover._account_usage_owner = self
        return recover

    def unload(self):
        with self._lifetime_lock:
            self.active = False
            if not _unloaded:
                _unloaded.append(True)
            self._restore_if_idle()

    def _restore_if_idle(self):
        # A timed-out worker can start its factory AFTER middleware has returned.
        # Keep transparent adapters until every retained context snapshot dies.
        if self._snapshots:
            return
        for obj, name, original, wrapper in reversed(self.patches):
            if getattr(obj, name) is wrapper:
                setattr(obj, name, original)
        self.patches.clear()

    def calling_agent(self):
        import sys
        frame = sys._getframe(1)
        try:
            while frame is not None:
                if frame.f_code is self.call_code:
                    return frame.f_locals.get('agent')
                frame = frame.f_back
            return None
        finally:
            del frame

    def middleware(self, *, next_call, request, **context):
        from hermes_constants import get_hermes_home
        from pathlib import Path
        if str(Path(get_hermes_home()).resolve()) != self.home or not self.active and not self.error:
            return next_call(request)
        agent = self.calling_agent()
        if agent is None or getattr(agent, 'is_subagent', False):
            return next_call(request)
        # Only registry-owned main agents participate; CLI/auxiliary agents pass
        # through. Never infer identity from session-id equality or other threads.
        import sys
        server = sys.modules.get('tui_gateway.server')
        if server is None:
            return next_call(request)
        with server._sessions_lock:
            owned = any(row.get('agent') is agent for row in server._sessions.values())
        if not owned:
            return next_call(request)
        from hermes_constants import get_hermes_home
        from hermes_cli.middleware import _DownstreamExecutionError
        if self.error:
            # No factories installed: never silently ignore a previously queued
            # intent. Unselected chats remain usable; cancel stays available.
            # Disabled means core drifted, so owner_scope (which walks core
            # session/registry internals) may itself fail. Only consult it when
            # this profile has SOME queued intent; otherwise a resolution error
            # would fail-closed every turn of every chat with nothing queued.
            path = Path(self.home) / 'account_usage_selection.sqlite3'
            try:
                queued = path.exists() and PendingStore(path).has_pending(self.home)
            except Exception:
                queued = True
            if not queued:
                return next_call(request)
            try:
                with owner_scope(profile_home=self.home, agent=agent) as owner:
                    pending = PendingStore(path).read(owner.key)['state'] == 'pending'
            except Exception:
                pending = True
            if pending:
                raise _DownstreamExecutionError(SelectionRejected(self.error))
            return next_call(request)
        turn = context.get('turn_id')
        refused = getattr(agent, '_account_usage_refused_turn', None)
        if refused is not None and refused[0] == turn:
            raise _DownstreamExecutionError(SelectionRejected(refused[1]))
        agent.__dict__.pop('_account_usage_refused_turn', None)
        clear_attempt(agent)
        automatic = agent.__dict__.pop('_account_usage_retry_snapshot', None)
        agent._account_usage_turn = context.get('turn_id')
        agent._account_usage_request_id = context.get('api_request_id')
        agent.__dict__.pop('_account_usage_observed_request', None)
        try:
            snapshot = SelectionService(get_hermes_home()).consume_for_agent(agent)
            if snapshot is None and automatic is not None and automatic[0] == context.get('turn_id'):
                candidate = automatic[1]
                if (agent.provider, agent.model, agent.base_url.rstrip('/')) == (
                        candidate.provider, candidate.model, candidate.base_url.rstrip('/')):
                    snapshot = candidate
            if snapshot is None:
                return next_call(request)
            self.retain_snapshot(snapshot)
            result = dispatch_snapshot(agent, snapshot, next_call, request)
        except SelectionRejected as exc:
            # Core fails OPEN on ordinary callback errors. Explicit refusal must
            # use its guarded downstream-error envelope, even before next_call.
            agent._account_usage_refused_turn = (turn, str(exc))
            raise _DownstreamExecutionError(exc) from None
        except _DownstreamExecutionError as exc:
            cause = exc.original
            while isinstance(cause, _DownstreamExecutionError):
                cause = cause.original
            if isinstance(cause, SelectionRejected):
                agent._account_usage_refused_turn = (turn, str(cause))
            raise
        except Exception:
            message = 'Account selection could not be checked; retry after the local service recovers'
            agent._account_usage_refused_turn = (turn, message)
            raise _DownstreamExecutionError(SelectionRejected(message)) from None
        # HTTP 200 can still carry a failed Responses result. Core classifies it
        # in retry_invalid_response AFTER this middleware returns. Preserve its
        # request identity for that recovery path and do not mark it successful.
        status = result.get('status') if isinstance(result, dict) else getattr(result, 'status', None)
        error = result.get('error') if isinstance(result, dict) else getattr(result, 'error', None)
        if result is None or status in {'failed', 'cancelled'} or error:
            agent._account_usage_failed_attempt = snapshot
            return result
        try:
            self.observe(agent, snapshot)
        except Exception:
            pass  # observation cannot turn a successful send into a retry
        return result


class SelectionService:
    """Server-side API operations; construct per request in its existing profile scope."""
    def __init__(self, profile_home, *, server=None):
        from pathlib import Path
        self.home = str(Path(profile_home).resolve())
        self.server = server

    def store(self):
        from pathlib import Path
        return PendingStore(Path(self.home) / 'account_usage_selection.sqlite3')

    @staticmethod
    def metadata(owner, row):
        return dict(session_id=owner.runtime_id, provider=owner.provider,
                    model=owner.model, **{k: row[k] for k in ('revision', 'account_id', 'state')})

    def get(self, runtime_id):
        with owner_scope(runtime_id, profile_home=self.home, server=self.server) as owner:
            store = self.store()
            invalidated = store.invalidate_route(owner.key, model=owner.model)
            abandoned = _load_abandoned.get(self.home)
            if abandoned and store.reject_pending(owner.key):
                return {**self.metadata(owner, store.read(owner.key)), 'error': abandoned}
            result = self.metadata(owner, store.read(owner.key))
            if invalidated or result['state'] == 'rejected':
                result['error'] = abandoned or 'Account selection was rejected; choose an available account for the current route'
            return result

    def consume_for_agent(self, agent):
        """One-shot claim + fresh hydration, before request dispatch (never HTTP).

        None means no pending selection. SelectionRejected is a visible terminal
        refusal, not permission to send on some other account. Caller must leave
        this method before invoking downstream or constructing request clients.
        """
        with owner_scope(profile_home=self.home, server=self.server, agent=agent) as owner:
            store = self.store()
            if store.invalidate_route(owner.key, model=owner.model):
                raise SelectionRejected('Chat provider or model changed; account selection was rejected')
            row = store.consume(owner.key, model=owner.model)
            if row is None:
                return None
            try:
                return read_snapshot(owner, row['account_id'])
            except Exception:
                store.reject_consumed(owner.key, revision=row['revision'] + 1)
                raise SelectionRejected('Selected account is no longer available; refresh usage and choose again') from None

    def put(self, runtime_id, *, provider, account_id, expected_revision):
        if self.home in _install_failures:
            raise SelectionRejected(_install_failures[self.home])
        with owner_scope(runtime_id, profile_home=self.home, server=self.server) as owner:
            if provider != owner.provider:
                raise SelectionRejected('Selected provider does not match this chat')
            read_snapshot(owner, account_id)
            row = self.store().replace(owner.key, expected_revision=expected_revision,
                                       model=owner.model, account_id=account_id)
            return self.metadata(owner, row)

    def delete(self, runtime_id, *, expected_revision):
        with owner_scope(runtime_id, profile_home=self.home, server=self.server) as owner:
            return self.metadata(owner, self.store().cancel(owner.key, expected_revision=expected_revision))
