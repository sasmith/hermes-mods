"""Alternative seams: real core identities/SDK clients, synthetic credentials only."""
import copy
import os
import importlib.util
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get('HERMES_AGENT_ROOT') or str(Path.home() / '.hermes' / 'hermes-agent'))
import hermes_bootstrap  # noqa
from agent.credential_pool import PooledCredential
from agent.account_usage_cache import _identity_id_for

PATH = Path(__file__).resolve().parents[1] / 'selection.py'

def selection():
    spec = importlib.util.spec_from_file_location('account_usage_selection', PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def entry(id='B', **kw):
    args = dict(provider='anthropic', id=id, label='same label', auth_type='oauth',
                priority=0, source='fixture', access_token=f'sk-ant-oat01-fixture-{id}',
                last_status='ok', base_url='https://api.anthropic.com')
    args.update(kw)
    return PooledCredential(**args)


class ReadOnlySelection(unittest.TestCase):
    def test_select_identity_without_touching_any_cooldowns(self):
        s = selection()
        rows = [entry('A', last_status='exhausted', last_status_at=time.time(),
                      last_error_reset_at=time.time() + 86400), entry()]
        before = copy.deepcopy(rows)
        with patch('agent.credential_pool.CredentialPool.reclaim', side_effect=AssertionError('unsafe reclaim')), \
             patch('agent.credential_pool.load_pool', side_effect=AssertionError('unsafe load')):
            result = s.choose_snapshot(rows, provider='anthropic', model='claude-fixture',
                                       account_id=_identity_id_for('anthropic', rows[1]),
                                       base_url='https://api.anthropic.com')
        self.assertEqual(result.entry_id, 'B')
        self.assertEqual(rows, before)
        self.assertNotIn(rows[1].access_token, repr(result))

    def test_reject_cooling_dead_removed_and_wrong_provider_without_writes(self):
        s = selection()
        for changes in ({'last_status': 'dead'},
                        {'last_status': 'exhausted', 'last_status_at': time.time(),
                         'last_error_reset_at': time.time() + 86400},
                        {'model_cooldowns': {'claude-fixture': time.time() + 3600}},
                        {'expires_at_ms': int((time.time() - 60) * 1000)},
                        {'provider': 'openai-codex'}):
            with self.subTest(changes=changes):
                row = entry(**changes)
                before = copy.deepcopy(row)
                with self.assertRaises(s.SelectionRejected):
                    s.choose_snapshot([row], provider='anthropic', model='claude-fixture',
                                      account_id=_identity_id_for('anthropic', row),
                                      base_url='https://api.anthropic.com')
                self.assertEqual(row, before)
        with self.assertRaises(s.SelectionRejected):
            s.choose_snapshot([], provider='anthropic', model='claude-fixture',
                              account_id='removed', base_url='https://api.anthropic.com')

class RequestLocalDispatch(unittest.TestCase):
    def test_anthropic_sdk_sends_selected_key_without_mutating_agent(self):
        import httpx
        import anthropic
        from agent.client_lifecycle import ClientLifecycleMixin
        from types import SimpleNamespace
        s = selection()
        sent = []
        def handle(req):
            sent.append(dict(req.headers))
            return httpx.Response(200, json={'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                'model': 'claude-fixture', 'content': [{'type': 'text', 'text': 'ok'}],
                'stop_reason': 'end_turn', 'stop_sequence': None,
                'usage': {'input_tokens': 1, 'output_tokens': 1}})
        a = SimpleNamespace(provider='anthropic', model='claude-fixture', api_mode='anthropic_messages',
            base_url='https://api.anthropic.com', api_key='fixture-A', _anthropic_api_key='fixture-A',
            _anthropic_base_url='https://api.anthropic.com', _oauth_1m_beta_disabled=False)
        def build(key):
            return anthropic.Anthropic(api_key=key[1], base_url=key[2], max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        a._build_anthropic_client_for_key = build
        original = lambda *args, **kwargs: self.fail('default refresh/client reached')
        wrapper = s.anthropic_factory(original)
        snap = s.CredentialSnapshot('anthropic', a.model, 'account-B', 'B', a.base_url, 'fixture-B')
        def downstream(request):
            with wrapper(a, reason='test') as client:
                return client.messages.create(model=a.model, max_tokens=4,
                    messages=[{'role': 'user', 'content': 'hi'}])
        response = s.dispatch_snapshot(a, snap, downstream, {})
        self.assertEqual(response.content[0].text, 'ok')
        self.assertEqual(sent[0]['x-api-key'], 'fixture-B')
        self.assertEqual(a.api_key, 'fixture-A')
        self.assertEqual(a._anthropic_api_key, 'fixture-A')
        self.assertIsNone(s.current_snapshot(a))

    def test_codex_sdk_sends_matching_bearer_and_account_headers(self):
        import base64
        import json
        import httpx
        import openai
        from types import SimpleNamespace
        s = selection()
        payload = base64.urlsafe_b64encode(json.dumps({'https://api.openai.com/auth': {
            'chatgpt_account_id': 'account-B', 'chatgpt_data_residency': 'eu'}}).encode()).decode().rstrip('=')
        selected_token = f'fixture.{payload}.signature'
        sent = []
        def handle(req):
            sent.append(dict(req.headers))
            return httpx.Response(200, json={'id': 'resp_test', 'object': 'response',
                'created_at': 1, 'model': 'gpt-fixture', 'output': [], 'status': 'completed'})
        a = SimpleNamespace(provider='openai-codex', model='gpt-fixture', api_mode='codex_responses',
            base_url='https://chatgpt.com/backend-api/codex', api_key='fixture-A',
            _client_kwargs={'api_key': 'fixture-A', 'base_url': 'https://chatgpt.com/backend-api/codex',
                'default_headers': {'authorization': 'Bearer fixture-A', 'chatgpt-account-id': 'account-A',
                                    'x-openai-internal-codex-residency': 'us'}})
        a._create_openai_client = lambda kwargs, **ignored: openai.OpenAI(**kwargs,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        original = lambda *args, **kwargs: self.fail('shared client reached')
        wrapper = s.codex_factory(original)
        snap = s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url, selected_token)
        before = copy.deepcopy(a._client_kwargs)
        def downstream(request):
            with wrapper(a, reason='test', api_kwargs=request) as client:
                return client.responses.create(model=a.model, input='hi')
        s.dispatch_snapshot(a, snap, downstream, {})
        self.assertEqual(sent[0]['authorization'], f'Bearer {selected_token}')
        self.assertEqual(sent[0]['chatgpt-account-id'], 'account-B')
        self.assertEqual(sent[0]['x-openai-internal-codex-residency'], 'eu')
        self.assertEqual(a._client_kwargs, before)
        self.assertEqual(a.api_key, 'fixture-A')

    def test_context_survives_parent_exit_but_not_other_request_or_agent(self):
        from agent.chat_completion_helpers import _context_thread_target
        from types import SimpleNamespace
        import threading
        s = selection()
        a = SimpleNamespace(provider='anthropic', model='claude-fixture', base_url='https://api.anthropic.com')
        b = SimpleNamespace(**vars(a))
        snap = s.CredentialSnapshot('anthropic', a.model, 'account-B', 'B', a.base_url, 'fixture-B')
        release, done = threading.Event(), threading.Event()
        seen = []
        def worker():
            release.wait(5)
            seen.append(s.current_snapshot(a))
            seen.append(s.current_snapshot(b))
            done.set()
        def downstream(req):
            thread = threading.Thread(target=_context_thread_target(worker))
            thread.start()
            return thread
        thread = s.dispatch_snapshot(a, snap, downstream, {})
        self.assertIsNone(s.current_snapshot(a))
        release.set()
        self.assertTrue(done.wait(5))
        thread.join()
        self.assertEqual(seen, [snap, None])

    def test_real_anthropic_core_dispatch_uses_selected_oauth_bearer(self):
        import httpx
        from types import SimpleNamespace, MethodType
        from agent.client_lifecycle import ClientLifecycleMixin
        from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
        from agent import anthropic_adapter
        s = selection()
        sent = []
        def send(client, request, **kwargs):
            sent.append(dict(request.headers))
            return httpx.Response(200, request=request, json={'id': 'msg_test', 'type': 'message',
                'role': 'assistant', 'model': 'claude-fixture', 'content': [{'type': 'text', 'text': 'ok'}],
                'stop_reason': 'end_turn', 'stop_sequence': None,
                'usage': {'input_tokens': 1, 'output_tokens': 1}})
        class Agent(ClientLifecycleMixin):
            pass
        a = Agent()
        a.provider, a.model, a.api_mode = 'anthropic', 'claude-fixture', 'anthropic_messages'
        a.base_url = a._anthropic_base_url = 'https://api.anthropic.com'
        a.api_key = a._anthropic_api_key = 'sk-ant-oat01-fixture-A'
        a._oauth_1m_beta_disabled, a._disable_streaming = False, True
        a._capture_anthropic_response_headers = lambda response: None
        a._create_request_anthropic_client = MethodType(s.anthropic_factory(
            ClientLifecycleMixin._create_request_anthropic_client), a)
        snapshot = s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url,
                                        'sk-ant-oat01-fixture-B')
        clients = []
        def make_client(reason, kind):
            c = a._create_request_anthropic_client(reason=reason)
            clients.append(c)
            return c
        def downstream(kwargs):
            return _dispatch_nonstreaming_api_request(a, kwargs, make_client=make_client)
        try:
            with patch.object(httpx.Client, 'send', send), \
                 patch.object(anthropic_adapter, '_custom_provider_extra_headers', return_value={}), \
                 patch.object(anthropic_adapter, '_get_claude_code_version', return_value='0.0.fixture'), \
                 patch.object(a, '_try_refresh_anthropic_client_credentials', side_effect=AssertionError('unsafe resolver')):
                response = s.dispatch_snapshot(a, snapshot, downstream, {'model': a.model,
                    'max_tokens': 8, 'messages': [{'role': 'user', 'content': 'hi'}]})
        finally:
            for c in clients:
                c.close()
        self.assertEqual(response.content[0].text, 'ok')
        self.assertEqual(sent[0]['authorization'], 'Bearer sk-ant-oat01-fixture-B')
        self.assertNotIn('x-api-key', sent[0])
        self.assertEqual(a.api_key, 'sk-ant-oat01-fixture-A')

    def test_actual_pool_recovery_benches_selected_identity_not_original(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from agent.credential_pool import CredentialPool
        from agent import agent_runtime_helpers as recovery
        s = selection()
        rows = [entry('A'), entry('B')]
        with patch('agent.credential_pool.get_pool_strategy', return_value='priority'):
            pool = CredentialPool('anthropic', rows)
        a = SimpleNamespace(provider='anthropic', model='claude-fixture', api_mode='anthropic_messages',
            base_url='https://api.anthropic.com', api_key=rows[0].runtime_api_key,
            _credential_pool=pool, _credential_pool_entry_id='A', _swap_credential=Mock(return_value=True))
        snap = s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url, rows[1].runtime_api_key)
        def failure(req):
            raise RuntimeError('429')
        with self.assertRaises(RuntimeError):
            s.dispatch_snapshot(a, snap, failure, {})
        wrapper = s.failure_identity(recovery._failed_credential_identity)
        with patch.object(recovery, '_failed_credential_identity', wrapper), \
             patch.object(recovery, '_ra', return_value=SimpleNamespace(logger=Mock())), \
             patch.object(pool, '_persist'), \
             patch.object(pool, '_resync_stale_entry', side_effect=lambda e: e), \
             patch.object(pool, '_entry_needs_refresh', return_value=False), \
             patch.object(pool, '_codex_quota_restored_upstream', return_value=False):
            result = recovery.recover_with_credential_pool(a, status_code=429, has_retried_429=True,
                error_context={'reason': 'usage_limit_reached', 'reset_at': time.time() + 3600})
        self.assertTrue(result[0])
        self.assertEqual(a._swap_credential.call_args.args[0].id, 'A')
        self.assertEqual(next(e for e in pool.entries() if e.id == 'A').last_status, 'ok')
        self.assertNotEqual(next(e for e in pool.entries() if e.id == 'B').model_cooldowns, None)
        # Main-agent key was never rewritten just to influence failure attribution.
        self.assertEqual(a.api_key, rows[0].runtime_api_key)
        s.clear_attempt(a)
        self.assertEqual(wrapper(a, pool), (rows[0].runtime_api_key, 'A'))

    def test_selected_client_retains_core_worker_abort_close_ownership(self):
        from agent.chat_completion_helpers import _RequestClientRegistry, _context_thread_target
        from types import SimpleNamespace
        import threading
        s = selection()
        actions = []
        a = SimpleNamespace(provider='anthropic', model='claude-fixture',
                            base_url='https://api.anthropic.com')
        a._abort_request_anthropic_client = lambda c, **kw: actions.append(('abort', threading.get_ident(), c))
        a._close_request_anthropic_client = lambda c, **kw: actions.append(('close', threading.get_ident(), c))
        registry = _RequestClientRegistry(a)
        snapshot = s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url, 'fixture-B')
        entered, release = threading.Event(), threading.Event()
        client = object()
        owner_ids, snapshots = [], []
        def worker():
            owner_ids.append(threading.get_ident())
            registry.set_client(client, kind='anthropic_messages')
            entered.set()
            release.wait(5)
            snapshots.append(s.current_snapshot(a))
            registry.close_once('request_complete')
            registry.close_once('request_complete')
        def downstream(request):
            thread = threading.Thread(target=_context_thread_target(worker))
            thread.start()
            self.assertTrue(entered.wait(5))
            return thread
        thread = s.dispatch_snapshot(a, snapshot, downstream, {})
        try:
            registry.close_once('watchdog_timeout')
            self.assertEqual(actions, [('abort', threading.get_ident(), client)])
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(snapshots, [snapshot])
        self.assertEqual(actions[-1], ('close', owner_ids[0], client))
        self.assertEqual(len(actions), 2)
        self.assertIsNone(registry.client)

    def test_real_execution_middleware_invokes_downstream_once_on_failure(self):
        from hermes_cli.middleware import run_llm_execution_middleware
        from types import SimpleNamespace
        from unittest.mock import Mock
        s = selection()
        a = SimpleNamespace(provider='anthropic', model='claude-fixture', base_url='https://api.anthropic.com')
        snap = s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url, 'fixture-B')
        calls = []
        def middleware(next_call, request, **context):
            return s.dispatch_snapshot(a, snap, next_call, request)
        def terminal(request):
            calls.append(s.current_snapshot(a))
            raise RuntimeError('fixture 429')
        manager = SimpleNamespace(_middleware={'llm_execution': [middleware]}, _report_hook_failure=Mock())
        with patch('hermes_cli.plugins._delivery_manager', return_value=manager):
            with self.assertRaisesRegex(RuntimeError, 'fixture 429'):
                run_llm_execution_middleware({}, terminal)
        self.assertEqual(calls, [snap])
        manager._report_hook_failure.assert_not_called()
        self.assertIsNone(s.current_snapshot(a))

    def test_request_model_must_match_selected_model(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        s = selection()
        a = SimpleNamespace(provider='anthropic', model='claude-fixture', base_url='https://api.anthropic.com')
        snap = s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url, 'fixture-B')
        downstream = Mock()
        with self.assertRaises(s.SelectionRejected):
            s.dispatch_snapshot(a, snap, downstream, {'model': 'different-model'})
        downstream.assert_not_called()
        self.assertIsNone(s.current_snapshot(a))

    def test_selection_rejects_request_identity_overrides_before_downstream(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        s = selection()
        a = SimpleNamespace(provider='openai-codex', model='gpt-fixture',
                            base_url='https://chatgpt.com/backend-api/codex')
        snap = s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url, 'fixture-B')
        for header in ('Authorization', 'X-API-Key', 'ChatGPT-Account-ID',
                       'OpenAI-Organization', 'OpenAI-Project', 'X-OpenAI-Internal-Codex-Residency'):
            with self.subTest(header=header):
                downstream = Mock()
                with self.assertRaises(s.SelectionRejected):
                    s.dispatch_snapshot(a, snap, downstream,
                                        {'extra_headers': {header: 'fixture-A'}})
                downstream.assert_not_called()
                self.assertIsNone(s.current_snapshot(a))

    def test_anthropic_factory_preserves_supported_setup_token_auth(self):
        from agent.client_lifecycle import ClientLifecycleMixin
        from agent import anthropic_adapter
        s = selection()
        class Agent(ClientLifecycleMixin):
            pass
        a = Agent()
        a.provider, a.model = 'anthropic', 'claude-fixture'
        a.base_url = 'https://api.anthropic.com'
        a._oauth_1m_beta_disabled = False
        factory = s.anthropic_factory(ClientLifecycleMixin._create_request_anthropic_client)
        for selected in ('cc-fixture-B', 'eyJfixture-B', 'sk-ant-setup-fixture-B'):
            with self.subTest(kind=selected.split('-')[0]):
                snap = s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url, selected)
                def downstream(request):
                    with factory(a, reason='fixture') as client:
                        self.assertEqual(client.default_headers['Authorization'], f'Bearer {selected}')
                with patch.object(anthropic_adapter, '_custom_provider_extra_headers', return_value={}), \
                     patch.object(anthropic_adapter, '_get_claude_code_version', return_value='0.0.fixture'):
                    s.dispatch_snapshot(a, snap, downstream, {})

    def test_anthropic_factory_rejects_configured_identity_override(self):
        from agent.client_lifecycle import ClientLifecycleMixin
        from agent import anthropic_adapter
        s = selection()
        class Agent(ClientLifecycleMixin):
            pass
        a = Agent()
        a.provider, a.model, a.api_mode = 'anthropic', 'claude-fixture', 'anthropic_messages'
        a.base_url = a._anthropic_base_url = 'https://api.anthropic.com'
        a.api_key = a._anthropic_api_key = 'sk-ant-oat01-fixture-A'
        a._oauth_1m_beta_disabled = False
        snap = s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url,
                                    'sk-ant-oat01-fixture-B')
        factory = s.anthropic_factory(ClientLifecycleMixin._create_request_anthropic_client)
        clients = []
        def downstream(request):
            clients.append(factory(a, reason='fixture'))
        try:
            with patch.object(anthropic_adapter, '_custom_provider_extra_headers',
                              return_value={'Authorization': 'Bearer fixture-A'}), \
                 patch.object(anthropic_adapter, '_get_claude_code_version', return_value='0.0.fixture'):
                with self.assertRaises(s.SelectionRejected):
                    s.dispatch_snapshot(a, snap, downstream, {})
        finally:
            for client in clients:
                client.close()

    def test_failure_propagates_once_and_no_context_leaks_to_fallback(self):
        from types import SimpleNamespace
        s = selection()
        a = SimpleNamespace(provider='anthropic', model='claude-fixture', base_url='https://api.anthropic.com')
        snap = s.CredentialSnapshot('anthropic', a.model, 'account-B', 'B', a.base_url, 'fixture-B')
        calls = []
        def downstream(req):
            calls.append(s.current_snapshot(a))
            raise RuntimeError('synthetic provider failure')
        with self.assertRaisesRegex(RuntimeError, 'synthetic provider failure'):
            s.dispatch_snapshot(a, snap, downstream, {})
        self.assertEqual(calls, [snap])
        self.assertIsNone(s.current_snapshot(a))


def child_consume(path, key, gate, results):
    gate.wait(10)
    results.put(selection().PendingStore(path).consume(key, model='claude-fixture'))


class AtomicPending(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = selection()
        self.store = self.s.PendingStore(Path(self.temp.name) / 'pending.sqlite3')
        self.key = ('profile-A', 'durable-A', 'anthropic')

    def test_once_cancel_replacement_and_profile_isolation(self):
        r = self.store.read(self.key)
        self.assertEqual(r['revision'], 0)
        first = self.store.replace(self.key, expected_revision=0, model='claude-fixture', account_id='B')
        second = self.store.replace(self.key, expected_revision=first['revision'], model='claude-fixture', account_id='C')
        with self.assertRaises(self.s.SelectionConflict):
            self.store.cancel(self.key, expected_revision=first['revision'])
        self.assertEqual(self.store.read(self.key), second)
        self.assertIsNone(self.store.consume(('profile-B', 'durable-A', 'anthropic'), model='claude-fixture'))
        self.assertIsNone(self.store.consume(('profile-A', 'durable-B', 'anthropic'), model='claude-fixture'))
        self.assertIsNone(self.store.consume(self.key, model='wrong-model'))
        self.assertEqual(self.store.consume(self.key, model='claude-fixture')['account_id'], 'C')
        self.assertIsNone(self.store.consume(self.key, model='claude-fixture'))
        rev = self.store.read(self.key)['revision']
        self.store.replace(self.key, expected_revision=rev, model='claude-fixture', account_id='B')
        self.store.cancel(self.key, expected_revision=rev + 1)
        self.assertIsNone(self.store.consume(self.key, model='claude-fixture'))

    def test_two_processes_consume_only_once(self):
        import multiprocessing
        self.store.replace(self.key, expected_revision=0, model='claude-fixture', account_id='B')
        ctx = multiprocessing.get_context('spawn')
        gate = ctx.Event()
        results = ctx.Queue()
        children = [ctx.Process(target=child_consume, args=(self.store.path, self.key, gate, results))
                    for _ in range(2)]
        for p in children:
            p.start()
        gate.set()
        values = [results.get(timeout=5) for p in children]
        for p in children:
            p.join(5)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(sum(v is not None for v in values), 1)
        self.assertNotIn('token', self.store.path.read_bytes().decode('latin1'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
