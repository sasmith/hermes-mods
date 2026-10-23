"""Backend acceptance with actual registry leases and runtime record fixtures."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get('HERMES_AGENT_ROOT') or str(Path.home() / '.hermes' / 'hermes-agent'))
import hermes_bootstrap
sys.path.insert(0, str(Path(__file__).parent))
from test_request_selection import selection, entry
from hermes_cli.active_sessions import try_acquire_active_session
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def remote_lease(home, ready, release):
    lease, refusal = try_acquire_active_session(session_id='durable-A', surface='desktop',
        config={}, registry_home=home, metadata={'live_session_id': 'runtime-A'})
    ready.put((lease.lease_id if lease else None, str(refusal) if refusal else None))
    release.wait(10)
    if lease:
        lease.release()


class Backend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / 'global'
        self.root.mkdir()
        root_patch = patch('hermes_constants.get_default_hermes_root', return_value=self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.scope = set_hermes_home_override(str(self.home))
        self.addCleanup(reset_hermes_home_override, self.scope)
        self.s = selection()
        # The installed constructor, not an invented runtime record schema.
        saved_stdout = sys.stdout
        from tui_gateway import server
        sys.stdout = saved_stdout
        with patch.object(server, '_load_show_reasoning', return_value=False), \
             patch.object(server, '_load_tool_progress_mode', return_value='off'):
            self.record = server._deferred_session_record('durable-A', cols=80, cwd=str(self.home),
                history=[], lease=None, source='desktop', profile_home=self.home)
        self.agent = SimpleNamespace(session_id='durable-A', provider='anthropic', model='claude-fixture',
                                     base_url='https://api.anthropic.com')
        self.record['agent'] = self.agent
        self.server = SimpleNamespace(_sessions={'runtime-A': self.record},
            _sessions_lock=threading.RLock(), _hermes_home=self.home)

    def lease(self):
        lease, refusal = try_acquire_active_session(session_id='durable-A', surface='desktop', config={},
            registry_home=self.home, metadata={'live_session_id': 'runtime-A'})
        self.assertIsNone(refusal)
        self.addCleanup(lease.release)
        self.record['active_session_lease'] = lease
        return lease

    def test_runtime_authority_and_main_identity(self):
        self.lease()
        self.assertTrue(hasattr(self.s, 'resolve_owner'), 'authoritative resolver missing')
        owner = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        self.assertEqual(owner.provider, 'anthropic')
        self.assertEqual(owner.key, self.s.resolve_owner(None, profile_home=self.home,
            server=self.server, agent=self.agent).key)
        for sid, home, agent in [('durable-A', self.home, None), ('missing', self.home, None),
                                 ('runtime-A', self.home / 'foreign', None),
                                 (None, self.home, SimpleNamespace(**vars(self.agent)))]:
            with self.subTest(sid=sid, home=home):
                with self.assertRaises(self.s.SelectionRejected):
                    self.s.resolve_owner(sid, profile_home=home, server=self.server, agent=agent)

    def database(self):
        from hermes_state import SessionDB
        db = SessionDB(self.home / 'state.db')
        self.addCleanup(db.close)
        db.create_session('durable-A', source='desktop', model=self.agent.model)
        return db

    def test_compute_borrowed_rotation_keeps_runtime_key(self):
        lease = self.lease()
        db = self.database()
        before = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        from tui_gateway.session_lifecycle import _install_borrowed_lease
        self.record.pop('active_session_lease')
        _install_borrowed_lease('runtime-A', self.record,
            {'active_session_lease': {'lease_id': lease.lease_id, 'session_id': 'durable-A'},
             'source': 'desktop'})
        db.publish_compression_child(parent_session_id='durable-A', child_session_id='durable-B',
            source='desktop', messages=[{'role': 'user', 'content': 'summary'}], require_compression_lease=False)
        self.agent.session_id = self.record['session_key'] = 'durable-B'
        self.record['active_session_lease'].session_id = 'durable-B'
        child = self.s.resolve_owner(None, profile_home=self.home, server=self.server, agent=self.agent)
        self.assertEqual(before.key, child.key)
        self.assertEqual(child.durable_id, 'durable-B')
        # Parent mirror catches up only after receiving the compute frame.
        from hermes_cli.active_sessions import transfer_active_session
        self.assertTrue(transfer_active_session(lease, session_id='durable-B',
            metadata={'live_session_id': 'runtime-A'}))
        self.assertEqual(before.key, self.s.resolve_owner('runtime-A',
            profile_home=self.home, server=self.server).key)

    def test_fork_cannot_borrow_parent_and_new_chat_gets_new_key(self):
        lease = self.lease()
        db = self.database()
        original = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        db.publish_compression_child(parent_session_id='durable-A', child_session_id='durable-B',
            source='desktop', messages=[{'role': 'user', 'content': 'summary'}], require_compression_lease=False)
        db.create_session('fork', source='desktop', parent_session_id='durable-A',
            model_config={'_branched_from': 'durable-A'})
        from tui_gateway.session_lifecycle import _install_borrowed_lease
        self.record.pop('active_session_lease')
        _install_borrowed_lease('runtime-A', self.record,
            {'active_session_lease': {'lease_id': lease.lease_id, 'session_id': 'durable-A'}})
        self.agent.session_id = self.record['session_key'] = 'fork'
        self.record['active_session_lease'].session_id = 'fork'
        with self.assertRaises(self.s.SelectionRejected):
            self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        lease.release()
        self.record['active_session_lease'] = None
        self.assertNotEqual(original.key, self.s.resolve_owner('runtime-A',
            profile_home=self.home, server=self.server).key)

    def test_pre_first_turn_does_not_require_lease(self):
        owner = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        self.assertEqual(owner.runtime_id, 'runtime-A')
        self.assertFalse((self.home / 'active_sessions.json').exists())

    def credentials(self, rows, home=None):
        target = (home or self.home) / 'auth.json'
        target.write_text(json.dumps({'credential_pool': {'anthropic': rows}}))
        return target

    def test_fresh_borrowed_hydration_uses_account_identity_not_label(self):
        self.assertTrue(hasattr(self.s, 'read_snapshot'), 'read-only hydration missing')
        owner = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        row = entry(source='hermes_pkce', access_token='fixture-B').to_dict()
        path = self.credentials([entry('A').to_dict(), row])
        token_path = self.home / '.anthropic_oauth.json'
        token_path.write_text(json.dumps({'accessToken': 'fixture-B', 'expiresAt': int((time.time()+3600)*1000)}))
        from agent.account_usage_cache import _identity_id_for
        identity = _identity_id_for('anthropic', entry(access_token='fixture-B'))
        before = {p: p.read_bytes() for p in (path, token_path)}
        with patch('agent.credential_pool.load_pool', side_effect=AssertionError('load_pool')), \
             patch('agent.credential_pool.CredentialPool.reclaim', side_effect=AssertionError('reclaim')), \
             patch('hermes_cli.auth._load_auth_store', side_effect=AssertionError('unsafe auth loader')):
            result = self.s.read_snapshot(owner, identity)
        self.assertEqual(result.token, 'fixture-B')
        self.assertEqual(result.entry_id, 'B')
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        token_path.write_text(json.dumps({'accessToken': 'fixture-C', 'expiresAt': int((time.time()+3600)*1000)}))
        with self.assertRaises(self.s.SelectionRejected):
            self.s.read_snapshot(owner, identity)
        with self.assertRaises(self.s.SelectionRejected):
            self.s.read_snapshot(owner, 'B')

    def test_selection_http_cas_and_metadata_only(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api_path = Path(__file__).parents[1] / 'dashboard' / 'plugin_api.py'
        spec = importlib.util.spec_from_file_location('account_usage_api_test', api_path)
        api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = api
        spec.loader.exec_module(api)
        self.assertTrue(hasattr(api, 'selection_service'), 'selection endpoints missing')
        service = self.s.SelectionService(self.home, server=self.server)
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[api.selection_service] = lambda: service
        client = TestClient(app)
        from agent.account_usage_cache import _identity_id_for
        row = entry(source='manual')
        self.credentials([row.to_dict()])
        identity = _identity_id_for('anthropic', row)
        get = client.get('/selection', params={'session_id': 'runtime-A'})
        self.assertEqual(get.status_code, 200, get.text)
        self.assertEqual(get.json()['state'], 'empty')
        body = {'session_id': 'runtime-A', 'provider': 'anthropic', 'account_id': identity,
                'expected_revision': get.json()['revision']}
        put = client.put('/selection', json=body)
        self.assertEqual(put.status_code, 200, put.text)
        self.assertEqual(put.json()['state'], 'pending')
        self.assertNotIn(row.access_token, put.text)
        self.assertNotIn('token', put.text)
        self.assertEqual(client.get('/selection', params={'session_id': 'runtime-A'}).json(), put.json())
        self.assertEqual(client.put('/selection', json=body).status_code, 409)
        body['durable_id'] = 'durable-A'
        self.assertEqual(client.put('/selection', json=body).status_code, 422)
        self.assertEqual(client.get('/selection', params={'session_id': 'durable-A'}).status_code, 400)
        self.assertEqual(client.delete('/selection', params={'session_id': 'runtime-A',
            'expected_revision': 1}).json()['state'], 'cancelled')
        self.assertEqual(client.get('/selection', params={'session_id': 'runtime-A'}).json()['state'], 'cancelled')
        self.assertEqual(client.delete('/selection', params={'session_id': 'runtime-A',
            'expected_revision': 1}).status_code, 409)

    def test_compute_parent_uses_authoritative_mirror_without_building_agent(self):
        self.lease()
        db = self.database()
        from tui_gateway import server
        config = server._runtime_model_config(self.agent)
        db.update_session_meta('durable-A', json.dumps(config), self.agent.model)
        before = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        server._apply_compute_host_metadata_mirror(self.record, {'session_key': 'durable-A',
            'session_info': {'provider': self.agent.provider, 'model': self.agent.model}})
        self.record['_compute_host_active'] = True
        self.record['agent'] = None
        owner = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        self.assertEqual(owner, before)
        self.record['_metadata_mirror']['model'] = 'changed-not-persisted'
        with self.assertRaises(self.s.SelectionRejected):
            self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)

    def test_service_consumes_once_and_never_revives_invalid_selection(self):
        self.assertTrue(hasattr(self.s.SelectionService, 'consume_for_agent'), 'dispatch API missing')
        self.lease()
        row = entry(source='manual')
        path = self.credentials([row.to_dict()])
        from agent.account_usage_cache import _identity_id_for
        identity = _identity_id_for('anthropic', row)
        service = self.s.SelectionService(self.home, server=self.server)
        service.put('runtime-A', provider='anthropic', account_id=identity, expected_revision=0)
        self.assertEqual(service.consume_for_agent(self.agent).entry_id, 'B')
        self.assertIsNone(service.consume_for_agent(self.agent))
        revision = service.get('runtime-A')['revision']
        service.put('runtime-A', provider='anthropic', account_id=identity, expected_revision=revision)
        self.agent.model = 'different-model'
        with self.assertRaises(self.s.SelectionRejected):
            service.consume_for_agent(self.agent)
        self.agent.model = 'claude-fixture'
        self.assertEqual(service.get('runtime-A')['state'], 'rejected')
        self.assertIsNone(service.consume_for_agent(self.agent))
        revision = service.get('runtime-A')['revision']
        service.put('runtime-A', provider='anthropic', account_id=identity, expected_revision=revision)
        row.last_status = 'dead'
        self.credentials([row.to_dict()])
        before = path.read_bytes()
        with self.assertRaises(self.s.SelectionRejected):
            service.consume_for_agent(self.agent)
        self.assertEqual(service.get('runtime-A')['state'], 'rejected')
        self.assertEqual(path.read_bytes(), before)
        row.last_status = 'ok'
        self.credentials([row.to_dict()])
        self.assertIsNone(service.consume_for_agent(self.agent))

    def test_unsupported_provider_change_invalidates_old_intent(self):
        row = entry(source='manual')
        self.credentials([row.to_dict()])
        from agent.account_usage_cache import _identity_id_for
        service = self.s.SelectionService(self.home, server=self.server)
        service.put('runtime-A', provider='anthropic', account_id=_identity_id_for('anthropic', row), expected_revision=0)
        self.agent.provider = 'openrouter'
        with self.assertRaises(self.s.SelectionRejected):
            service.consume_for_agent(self.agent)
        self.agent.provider = 'anthropic'
        self.assertEqual(service.get('runtime-A')['state'], 'rejected')

    def test_disk_rejection_matrix_and_corruption_never_writes(self):
        owner = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        from agent.account_usage_cache import _identity_id_for
        for changes in ({'last_status': 'dead'},
                        {'last_status': 'exhausted', 'last_error_reset_at': time.time()+600},
                        {'model_cooldowns': {'claude-fixture': time.time()+600}},
                        {'expires_at_ms': int((time.time()-60)*1000)},
                        {'access_token': ''}):
            row = entry(source='manual', **changes)
            path = self.credentials([row.to_dict()])
            before = path.read_bytes()
            with self.subTest(changes=changes), self.assertRaises(self.s.SelectionRejected):
                self.s.read_snapshot(owner, _identity_id_for('anthropic', row))
            self.assertEqual(before, path.read_bytes())
        path.write_text('{malformed')
        before = set(self.home.iterdir())
        with self.assertRaises(self.s.SelectionRejected):
            self.s.read_snapshot(owner, 'whatever')
        self.assertEqual(before, set(self.home.iterdir()))
        self.assertEqual(path.read_text(), '{malformed')

    def test_profile_rows_shadow_root_and_borrow_root_singleton_readonly(self):
        owner = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        self.credentials([entry(source='hermes_pkce', access_token='root-fixture').to_dict()], self.root)
        path = self.root / '.anthropic_oauth.json'
        path.write_text(json.dumps({'accessToken': 'root-fixture', 'expiresAt': int((time.time()+3600)*1000)}))
        from agent.account_usage_cache import _identity_id_for
        identity = _identity_id_for('anthropic', entry(access_token='root-fixture'))
        self.assertEqual(self.s.read_snapshot(owner, identity).token, 'root-fixture')
        self.credentials([entry('local', source='manual').to_dict()])
        with self.assertRaises(self.s.SelectionRejected):
            self.s.read_snapshot(owner, identity)

    def test_expired_codex_jwt_is_rejected_without_expiry_columns(self):
        import base64
        from agent.account_usage_cache import _identity_id_for
        self.agent.provider, self.agent.model = 'openai-codex', 'gpt-fixture'
        self.agent.base_url = 'https://chatgpt.com/backend-api/codex'
        owner = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        def token(expiry):
            payload = {'exp': expiry, 'https://api.openai.com/auth': {
                'chatgpt_account_id': 'account-B', 'chatgpt_user_id': 'user-B'}}
            return 'fixture.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=') + '.signature'
        for expiry in (time.time()+3600, time.time()-60):
            row = entry('credential-B', provider='openai-codex', source='device_code',
                base_url=self.agent.base_url, access_token=token(expiry))
            (self.home/'auth.json').write_text(json.dumps({'credential_pool': {'openai-codex': [row.to_dict()]}}))
            identity = _identity_id_for('openai-codex', row)
            if expiry > time.time():
                snap = self.s.read_snapshot(owner, identity)
                self.assertEqual(snap.account_id, identity)
                self.assertEqual(snap.entry_id, 'credential-B')
                self.assertNotEqual(identity, snap.entry_id)
            else:
                with self.assertRaises(self.s.SelectionRejected):
                    self.s.read_snapshot(owner, identity)

    def test_only_selected_borrowed_source_is_read(self):
        from agent.account_usage_cache import _identity_id_for
        selected = entry('selected', source='manual')
        borrowed = entry('external', source='claude_code', access_token='borrowed-fixture')
        self.credentials([selected.to_dict(), borrowed.to_dict()])
        owner = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        with patch('agent.anthropic_credentials.read_claude_code_credentials',
                   side_effect=AssertionError('unselected external source read')):
            self.assertEqual(self.s.read_snapshot(owner, _identity_id_for('anthropic', selected)).entry_id, 'selected')
        with patch('agent.anthropic_credentials.read_claude_code_credentials',
                   return_value={'accessToken': 'borrowed-fixture', 'expiresAt': int((time.time()+3600)*1000)}) as reader:
            self.assertEqual(self.s.read_snapshot(owner, _identity_id_for('anthropic', borrowed)).entry_id, 'external')
            reader.assert_called_once()

    def test_unknown_borrowed_source_cannot_use_legacy_embedded_secret(self):
        from agent.account_usage_cache import _identity_id_for
        row = entry(source='external-plugin')
        payload = dict(vars(row))  # intentionally legacy/unsanitized disk shape
        self.credentials([payload])
        owner = self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
        with self.assertRaises(self.s.SelectionRejected):
            self.s.read_snapshot(owner, _identity_id_for('anthropic', row))

    def test_real_foreign_pid_borrowed_lease_and_spoof_rejection(self):
        import multiprocessing
        from hermes_cli.active_sessions import ActiveSessionLease
        from tui_gateway.session_lifecycle import _install_borrowed_lease
        ctx = multiprocessing.get_context('spawn')
        ready, release = ctx.Queue(), ctx.Event()
        proc = ctx.Process(target=remote_lease, args=(str(self.home), ready, release))
        proc.start()
        try:
            lease_id, refusal = ready.get(timeout=5)
            self.assertIsNone(refusal)
            self.record['active_session_lease'] = ActiveSessionLease(lease_id, 'durable-A', 'desktop')
            with self.assertRaises(self.s.SelectionRejected):
                self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)
            self.record.pop('active_session_lease')
            _install_borrowed_lease('runtime-A', self.record,
                {'active_session_lease': {'lease_id': lease_id, 'session_id': 'durable-A'}, 'source': 'desktop'})
            self.assertEqual(self.s.resolve_owner(None, profile_home=self.home,
                server=self.server, agent=self.agent).runtime_id, 'runtime-A')
            self.record['active_session_lease'].lease_id = 'borrowed:forged'
            with self.assertRaises(self.s.SelectionRejected):
                self.s.resolve_owner(None, profile_home=self.home, server=self.server, agent=self.agent)
        finally:
            release.set()
            proc.join(10)
        self.assertEqual(proc.exitcode, 0)
        self.record['active_session_lease'].lease_id = 'borrowed:' + lease_id
        with self.assertRaises(self.s.SelectionRejected):
            self.s.resolve_owner(None, profile_home=self.home, server=self.server, agent=self.agent)

    def test_provider_mismatch_and_bad_ownership_do_not_create_pending(self):
        service = self.s.SelectionService(self.home, server=self.server)
        with self.assertRaises(self.s.SelectionRejected):
            service.put('runtime-A', provider='openai-codex', account_id='whatever', expected_revision=0)
        self.record['_finalized'] = True
        with self.assertRaises(self.s.SelectionRejected):
            service.get('runtime-A')
        self.assertFalse((self.home/'account_usage_selection.sqlite3').exists())

    def test_ambiguous_compression_siblings_are_not_aliases(self):
        self.lease()
        db = self.database()
        db.publish_compression_child(parent_session_id='durable-A', child_session_id='durable-B',
            source='desktop', messages=[{'role': 'user', 'content': 'summary'}], require_compression_lease=False)
        db.create_session('ambiguous-sibling', source='desktop', parent_session_id='durable-A')
        self.agent.session_id = self.record['session_key'] = 'durable-B'
        with self.assertRaises(self.s.SelectionRejected):
            self.s.resolve_owner('runtime-A', profile_home=self.home, server=self.server)


if __name__ == '__main__':
    unittest.main()
