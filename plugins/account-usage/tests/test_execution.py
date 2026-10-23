"""Installed execution integration; no real credentials or provider traffic."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from test_selection_backend import Backend
from test_request_selection import entry
from types import SimpleNamespace
from unittest.mock import patch, Mock
import unittest


class Execution(unittest.TestCase):
    def setUp(self):
        import gc
        gc.collect()
        Backend.setUp(self)
        from hermes_logging import _reset_queued_handlers
        self.addCleanup(_reset_queued_handlers)
    credentials = Backend.credentials
    lease = Backend.lease
    database = Backend.database

    def test_register_upgrades_pre_execution_cached_module_without_replacing_api_reference(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('account_usage_upgrade', Path(__file__).parents[1] / '__init__.py')
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        cached = self.s
        del cached.ExecutionIntegration
        cleanup, middleware = [], []
        ctx = SimpleNamespace(register_hook=lambda *args: None,
            register_middleware=lambda kind, cb: middleware.append(cb), on_unload=cleanup.append)
        self.addCleanup(lambda: [fn() for fn in reversed(cleanup)])
        plugin.register(ctx)
        self.assertIs(sys.modules['account_usage_selection'], cached)
        self.assertTrue(middleware[0].__self__.active)

    def test_codex_factory_refuses_inherited_project_or_custom_transport(self):
        from agent.client_lifecycle import ClientLifecycleMixin
        s = self.s
        a = SimpleNamespace(provider='openai-codex', model='gpt-fixture', base_url='https://chatgpt.com/backend-api/codex')
        snap = s.CredentialSnapshot(a.provider, a.model, 'B', 'B', a.base_url, 'fixture-B')
        factory = s.codex_factory(lambda *args, **kw: None)
        a._create_openai_client = Mock(return_value=SimpleNamespace())
        for custom in ({'organization': 'wrong'}, {'project': 'wrong'}, {'http_client': object()}):
            a._client_kwargs = custom
            with self.assertRaises(s.SelectionRejected):
                s.dispatch_snapshot(a, snap, lambda req: factory(a, reason='fixture', api_kwargs=req), {})
        a._create_openai_client.assert_not_called()
        import openai, os
        clients = []
        def create(kwargs, **ignored):
            client = openai.OpenAI(**kwargs)
            clients.append(client)
            return client
        a._create_openai_client = create
        a._client_kwargs = {}
        try:
            with patch.dict(os.environ, {'OPENAI_ORG_ID': 'wrong-org'}):
                with self.assertRaises(s.SelectionRejected):
                    s.dispatch_snapshot(a, snap, lambda req: factory(a, reason='fixture', api_kwargs=req), {})
            self.assertTrue(clients[0].is_closed())
        finally:
            for client in clients:
                client.close()

    def test_guard_failure_keeps_observer_but_refuses_new_selection(self):
        import importlib.util
        from agent.client_lifecycle import ClientLifecycleMixin
        from agent.account_usage_cache import _identity_id_for
        spec = importlib.util.spec_from_file_location('account_usage_failed_guard', Path(__file__).parents[1] / '__init__.py')
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        hooks, middleware, cleanup = {}, {}, []
        ctx = SimpleNamespace(register_hook=lambda n, cb: hooks.setdefault(n, cb),
            register_middleware=lambda n, cb: middleware.setdefault(n, cb), on_unload=cleanup.append)
        self.addCleanup(lambda: [fn() for fn in reversed(cleanup)])
        with patch.object(ClientLifecycleMixin, '_create_request_openai_client', lambda *args, **kw: None), \
                self.assertLogs('account_usage_failed_guard', level='WARNING'):
            plugin.register(ctx)
        self.assertIn('post_api_request', hooks)
        self.credentials([entry('B', source='manual').to_dict()])
        with self.assertRaisesRegex(self.s.SelectionRejected, 'compatibility'):
            self.s.SelectionService(self.home, server=self.server).put('runtime-A', provider='anthropic',
                account_id=_identity_id_for('anthropic', entry('B')), expected_revision=0)

    def test_normal_plugin_manager_discovers_installs_and_unloads(self):
        import shutil, os
        from hermes_cli.plugins import PluginManager
        from agent.client_lifecycle import ClientLifecycleMixin
        source = Path(__file__).parents[1]
        target = self.home / 'plugins/account-usage'
        (target / 'dashboard').mkdir(parents=True)
        for name in ('__init__.py', 'plugin.yaml', 'selection.py', 'execution-compatibility.json', 'dashboard/plugin_api.py'):
            shutil.copyfile(source / name, target / name)
        (self.home / 'config.yaml').write_text('plugins:\n  enabled: [account-usage]\n')
        original = ClientLifecycleMixin._create_request_anthropic_client
        manager = PluginManager()
        self.addCleanup(manager.unload)
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
            manager.discover_and_load()
            self.assertIn('account-usage', manager._plugins)
            self.assertEqual(len(manager._middleware.get('llm_execution', [])), 1)
            self.assertIsNot(ClientLifecycleMixin._create_request_anthropic_client, original)
            manager.unload('account-usage')
        self.assertIs(ClientLifecycleMixin._create_request_anthropic_client, original)
        self.assertFalse(manager._middleware.get('llm_execution'))

    def test_unload_keeps_factory_for_a_late_worker_snapshot(self):
        import gc
        from agent.client_lifecycle import ClientLifecycleMixin
        integration = self.s.ExecutionIntegration(observe=lambda *args: None)
        original = ClientLifecycleMixin._create_request_anthropic_client
        integration.install()
        self.addCleanup(integration.unload)
        snap = self.s.CredentialSnapshot('anthropic', 'claude-fixture', 'B', 'B', 'https://api.anthropic.com', 'fixture-B')
        self.assertTrue(hasattr(integration, 'retain_snapshot'), 'worker lifetime protection missing')
        integration.retain_snapshot(snap)
        wrapper = ClientLifecycleMixin._create_request_anthropic_client
        integration.unload()
        self.assertIs(ClientLifecycleMixin._create_request_anthropic_client, wrapper)
        del snap
        gc.collect()
        self.assertIs(ClientLifecycleMixin._create_request_anthropic_client, original)

    def test_consume_io_failure_is_visible_and_never_dispatches(self):
        from agent.turn_api_call import perform_api_call
        a = self.agent
        a.platform, a.api_mode, a._disable_streaming = 'desktop', 'anthropic_messages', False
        a._has_stream_consumers = lambda: True
        a._has_pending_redirect = lambda: False
        a._interruptible_streaming_api_call = Mock(return_value='wrong-account')
        integration = self.s.ExecutionIntegration(observe=lambda *args: None)
        integration.install()
        self.addCleanup(integration.unload)
        manager = SimpleNamespace(_middleware={'llm_execution': [integration.middleware]}, _report_hook_failure=Mock())
        with patch.dict(sys.modules, {'tui_gateway.server': self.server}), patch('hermes_cli.plugins._delivery_manager', return_value=manager), \
                patch.object(self.s.SelectionService, 'consume_for_agent', side_effect=[OSError('fixture storage failure'), None]):
            for attempt in range(2):
                with self.assertRaises(self.s.SelectionRejected):
                    perform_api_call(a, api_kwargs={}, _original_api_kwargs={}, _llm_middleware_trace=[], _moa_prepared_request=None,
                        _retry=SimpleNamespace(), thinking_spinner=None, retry_count=attempt, api_call_count=attempt,
                        api_request_id='error', effective_task_id='t', turn_id='turn', interrupted=False)
        a._interruptible_streaming_api_call.assert_not_called()
        manager._report_hook_failure.assert_not_called()

    def test_disabled_integration_fails_closed_only_when_something_is_queued(self):
        # Disabled = core drifted, so owner resolution may break. With nothing
        # queued that must not reject every turn; with a queued intent it must.
        from agent.account_usage_cache import _identity_id_for
        from hermes_cli.middleware import _DownstreamExecutionError
        integration = self.s.ExecutionIntegration(observe=lambda *args: None)
        integration.disable()
        self.addCleanup(self.s._install_failures.pop, integration.home, None)
        next_call = Mock(return_value='sent')
        def run():
            with patch.dict(sys.modules, {'tui_gateway.server': self.server}), \
                    patch.object(integration, 'calling_agent', return_value=self.agent), \
                    patch.object(self.s, 'owner_scope', side_effect=RuntimeError('core drift')):
                return integration.middleware(next_call=next_call, request='req', turn_id='turn')
        self.assertEqual(run(), 'sent')
        self.credentials([entry('B', source='manual').to_dict()])
        # Intent queued while healthy, then core drifted.
        self.s._install_failures.pop(integration.home, None)
        self.s.SelectionService(self.home, server=self.server).put(
            'runtime-A', provider='anthropic', account_id=_identity_id_for('anthropic', entry('B')), expected_revision=0)
        integration.disable()
        with self.assertRaises(_DownstreamExecutionError):
            run()
        self.assertEqual(next_call.call_count, 1)

    def test_install_refuses_in_memory_factory_drift_before_any_patch(self):
        from agent.client_lifecycle import ClientLifecycleMixin
        original = ClientLifecycleMixin._create_request_anthropic_client
        integration = self.s.ExecutionIntegration(observe=lambda *args: None)
        with patch.object(ClientLifecycleMixin, '_create_request_openai_client', lambda *args, **kw: None):
            with self.assertRaises(self.s.SelectionRejected):
                integration.install()
            self.assertIs(ClientLifecycleMixin._create_request_anthropic_client, original)
        integration.unload()

    def test_route_away_and_back_invalidates_pending(self):
        from agent import agent_runtime_helpers as runtime
        from agent.account_usage_cache import _identity_id_for
        self.credentials([entry('B', source='manual').to_dict()])
        service = self.s.SelectionService(self.home, server=self.server)
        service.put('runtime-A', provider='anthropic', account_id=_identity_id_for('anthropic', entry('B')), expected_revision=0)
        integration = self.s.ExecutionIntegration(observe=lambda *args: None)
        # Exercise the wrapper around the route boundary without constructing a
        # real provider client during the model switch.
        self.assertTrue(hasattr(integration, 'route_wrapper'), 'route reconciliation missing')
        def switch(agent, model):
            agent.model = model
        switch = integration.route_wrapper(switch)
        with patch.dict(sys.modules, {'tui_gateway.server': self.server}):
            switch(self.agent, 'different')
            switch(self.agent, 'claude-fixture')
        self.assertEqual(service.get('runtime-A')['state'], 'rejected')
        self.assertIsNone(service.consume_for_agent(self.agent))

    def test_plugin_registration_and_selected_observation_does_not_revert(self):
        import importlib.util, json
        from agent.client_lifecycle import ClientLifecycleMixin
        spec = importlib.util.spec_from_file_location('account_usage_fixture_plugin', Path(__file__).parents[1] / '__init__.py')
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        hooks, middleware, cleanup = {}, {}, []
        ctx = SimpleNamespace(register_hook=lambda name, cb: hooks.setdefault(name, cb),
            register_middleware=lambda name, cb: middleware.setdefault(name, cb), on_unload=cleanup.append)
        original = ClientLifecycleMixin._create_request_anthropic_client
        plugin.register(ctx)
        self.addCleanup(lambda: [fn() for fn in reversed(cleanup)])
        self.assertIn('llm_execution', middleware)
        self.assertIsNot(ClientLifecycleMixin._create_request_anthropic_client, original)
        a = self.agent
        a._credential_pool = SimpleNamespace(entries=lambda: [entry('A'), entry('B')], current=lambda: entry('A'))
        a._credential_pool_entry_id = 'A'
        snap = self.s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url, 'fixture-B')
        a._account_usage_request_id = 'request-B'
        with patch.object(plugin, '_find_agent', return_value=a):
            hooks['post_api_request'](session_id=a.session_id, provider=a.provider, api_request_id='request-A')
        plugin._observe_selected(a, snap)
        unrelated = SimpleNamespace(**vars(a))
        unrelated.__dict__.pop('_account_usage_observed_request', None)
        with patch.object(plugin, '_find_agent', return_value=unrelated):
            hooks['post_api_request'](session_id=a.session_id, provider=a.provider, api_request_id='request-B')
        record = json.loads((self.home / 'account_usage_last.json').read_text())['anthropic']
        self.assertEqual(record['entry_id'], 'B')
        self.assertEqual(record['account_id'], 'account-B')
        self.assertNotIn('fixture-B', json.dumps(record))
        with patch.object(plugin, '_find_agent', return_value=a):
            hooks['post_api_request'](session_id=a.session_id, provider=a.provider, api_request_id='next-A')
        self.assertEqual(json.loads((self.home / 'account_usage_last.json').read_text())['anthropic']['entry_id'], 'A')
        for fn in reversed(cleanup):
            fn()
        self.assertIs(ClientLifecycleMixin._create_request_anthropic_client, original)

    # Reuse fixture setup, not the backend suite's test cases.
    def test_selected_recovery_rotates_without_swap_and_retry_is_request_local(self):
        from agent import agent_runtime_helpers as runtime
        from agent.credential_pool import CredentialPool
        from agent.error_classifier import FailoverReason
        from unittest.mock import Mock
        a = self.agent
        a.api_key = 'fixture-original'
        a._credential_pool_entry_id = 'A'
        a._swap_credential = Mock(side_effect=AssertionError('unsafe swap'))
        rows = [entry('A', source='manual'), entry('B', source='manual')]
        pool = CredentialPool('anthropic', rows)
        a._credential_pool = pool
        snapshot = self.s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url, rows[1].runtime_api_key)
        integration = self.s.ExecutionIntegration(observe=lambda *args: None)
        integration.install()
        self.addCleanup(integration.unload)
        a._account_usage_failed_attempt = snapshot
        a._account_usage_turn = 'turn'
        with patch.object(pool, '_persist'), patch('agent.credential_pool.get_env_prefer_dotenv', return_value=''):
            recovered, retry = runtime.recover_with_credential_pool(a, status_code=429, has_retried_429=False,
                classified_reason=FailoverReason.rate_limit, error_context={'retry_after': 60})
        self.assertTrue(recovered)
        self.assertFalse(retry)
        self.assertEqual(a._account_usage_retry_snapshot[1].entry_id, 'A')
        from agent.credential_pool_model_cooldowns import model_cooldown_until
        updated = {row.id: row for row in pool.entries()}
        self.assertIsNotNone(model_cooldown_until(updated['B'], a.model))
        self.assertIsNone(model_cooldown_until(updated['A'], a.model))
        self.assertEqual(a.api_key, 'fixture-original')
        a._swap_credential.assert_not_called()
        a.platform, a.api_mode, a._disable_streaming = 'desktop', 'anthropic_messages', False
        a._has_stream_consumers = lambda: True
        a._has_pending_redirect = lambda: False
        sent = []
        a._interruptible_streaming_api_call = lambda req, **kw: sent.append(self.s.current_snapshot(a)) or 'ok'
        manager = SimpleNamespace(_middleware={'llm_execution': [integration.middleware]}, _report_hook_failure=Mock())
        from agent.turn_api_call import perform_api_call
        with patch.dict(sys.modules, {'tui_gateway.server': self.server}), patch('hermes_cli.plugins._delivery_manager', return_value=manager):
            perform_api_call(a, api_kwargs={'model': a.model}, _original_api_kwargs={},
                _llm_middleware_trace=[], _moa_prepared_request=None, _retry=SimpleNamespace(), thinking_spinner=None,
                retry_count=1, api_call_count=2, api_request_id='retry', effective_task_id='t', turn_id='turn', interrupted=False)
        self.assertEqual(sent[0].entry_id, 'A')
        self.assertFalse(hasattr(a, '_account_usage_retry_snapshot'))

    def test_selected_recovery_uses_core_pool_failure_reason(self):
        # Same failure_reason as core's recover_with_credential_pool: a usage-window 402
        # benches only the refused model, an unverified billing body the short cooldown.
        from agent import agent_runtime_helpers as runtime
        from agent.credential_pool import CredentialPool, FAILURE_REASON_BILLING_UNVERIFIED
        from agent.credential_pool_model_cooldowns import FAILURE_REASON_USAGE_WINDOW
        from agent.error_classifier import FailoverReason
        a = self.agent
        rows = [entry('A', source='manual'), entry('B', source='manual')]
        pool = CredentialPool('anthropic', rows)
        a._credential_pool = pool
        snapshot = self.s.CredentialSnapshot(a.provider, a.model, 'account-B', 'B', a.base_url, rows[1].runtime_api_key)
        integration = self.s.ExecutionIntegration(observe=lambda *args: None)
        integration.install()
        self.addCleanup(integration.unload)
        cases = [({'usage_window': True, 'reset_at': 4102444800.0}, False, FAILURE_REASON_USAGE_WINDOW),
                 ({}, True, FAILURE_REASON_BILLING_UNVERIFIED), ({}, False, 'billing')]
        for context, unverified, expected in cases:
            a._account_usage_failed_attempt = snapshot
            with patch.object(pool, 'mark_exhausted_and_rotate', return_value=None) as rotate:
                self.assertEqual(runtime.recover_with_credential_pool(a, status_code=402, has_retried_429=False,
                    classified_reason=FailoverReason.billing, error_context=context,
                    billing_unverified=unverified), (False, False))
            self.assertEqual(rotate.call_args.kwargs['failure_reason'], expected)
            self.assertEqual(rotate.call_args.kwargs['credential_id'], 'B')

    def test_full_turn_unavailable_selection_never_sends_another_account(self):
        import httpx
        from run_agent import AIAgent
        from hermes_cli.plugins import PluginManager
        from agent.account_usage_cache import _identity_id_for
        rows = [entry('A', source='manual'), entry('B', source='manual')]
        self.credentials([e.to_dict() for e in rows])
        with patch('agent.model_metadata.get_model_context_length', return_value=128000):
            a = AIAgent(api_key='fixture-A', base_url='https://api.anthropic.com', model='claude-fixture',
                provider='anthropic', quiet_mode=True, skip_context_files=True, skip_memory=True,
                enabled_toolsets=[], max_iterations=1, platform='desktop', session_id='durable-A')
        self.agent = self.record['agent'] = a
        service = self.s.SelectionService(self.home, server=self.server)
        service.put('runtime-A', provider='anthropic', account_id=_identity_id_for('anthropic', rows[1]), expected_revision=0)
        rows[1].last_status = 'dead'
        self.credentials([e.to_dict() for e in rows])
        integration = self.s.ExecutionIntegration(observe=lambda *args: None)
        integration.install()
        self.addCleanup(integration.unload)
        manager = PluginManager()
        manager._middleware['llm_execution'] = [integration.middleware]
        with patch.dict(sys.modules, {'tui_gateway.server': self.server}), patch('hermes_cli.plugins._delivery_manager', return_value=manager), \
                patch.object(httpx.Client, 'send', side_effect=AssertionError('provider must not be called')) as send, \
                patch('agent.turn_api_error.interruptible_backoff_sleep', return_value=False):
            result = a.run_conversation('hi')
        send.assert_not_called()
        self.assertIn('Selected account is no longer available', str(result))
        self.assertEqual(service.get('runtime-A')['state'], 'rejected')

    def test_real_codex_sse_selected_429_then_core_recovery_sends_other_account(self):
        import base64, json, httpx, openai
        from run_agent import AIAgent
        from agent.turn_api_call import perform_api_call
        from agent.turn_recovery import recover_after_classification
        from agent.turn_retry_state import TurnRetryState
        from agent.error_classifier import classify_api_error
        from agent.credential_pool import CredentialPool
        from agent.account_usage_cache import _identity_id_for
        def token(account):
            payload = base64.urlsafe_b64encode(json.dumps({'https://api.openai.com/auth': {
                'chatgpt_account_id': account, 'chatgpt_data_residency': 'eu'}}).encode()).decode().rstrip('=')
            return 'fixture.' + payload + '.signature'
        rows = [entry(id, provider='openai-codex', source='manual', access_token=token(id),
                      base_url='https://chatgpt.com/backend-api/codex') for id in ('A', 'B')]
        (self.home / 'auth.json').write_text(json.dumps({'credential_pool': {'openai-codex': [e.to_dict() for e in rows]}}))
        with patch('agent.model_metadata.get_model_context_length', return_value=128000):
            a = AIAgent(api_key=token('A'), base_url='https://chatgpt.com/backend-api/codex', model='gpt-fixture',
                provider='openai-codex', quiet_mode=True, skip_context_files=True, skip_memory=True,
                enabled_toolsets=[], max_iterations=1, platform='desktop', session_id='durable-A')
        self.agent = self.record['agent'] = a
        observed = []
        integration = self.s.ExecutionIntegration(observe=lambda a, snapshot: observed.append(snapshot.entry_id))
        integration.install()
        self.addCleanup(integration.unload)
        self.addCleanup(self.s.clear_attempt, a)
        service = self.s.SelectionService(self.home, server=self.server)
        manager = SimpleNamespace(_middleware={'llm_execution': [integration.middleware]}, _report_hook_failure=Mock())
        sent = []
        def send(client, request, **kw):
            headers = dict(request.headers)
            sent.append(headers)
            if headers.get('chatgpt-account-id') == 'B':
                if soft:
                    response = {'id': 'resp_failed', 'object': 'response', 'created_at': 1, 'model': a.model,
                        'status': 'failed', 'output': [], 'error': {'code': 'usage_limit_reached', 'message': 'usage limit reached'}}
                    data = 'event: response.failed\ndata: '+json.dumps({'type': 'response.failed', 'response': response})+'\n\n'
                    return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'}, content=data.encode())
                return httpx.Response(429, request=request, json={'error': {'message': 'usage limit reached', 'type': 'usage_limit_reached', 'resets_in_seconds': 60}})
            response = {'id': 'resp_fixture', 'object': 'response', 'created_at': 1, 'model': a.model,
                'status': 'completed', 'output': [{'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                'status': 'completed', 'content': [{'type': 'output_text', 'text': 'ok', 'annotations': []}]}],
                'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
            data = 'event: response.completed\ndata: '+json.dumps({'type': 'response.completed', 'response': response})+'\n\n'
            return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'}, content=data.encode())
        with patch.dict(sys.modules, {'tui_gateway.server': self.server}), patch('hermes_cli.plugins._delivery_manager', return_value=manager), patch.object(httpx.Client, 'send', send):
            for stream, soft in ((False, False), (True, False), (True, True)):
                a._disable_streaming = not stream
                a._credential_pool = pool = CredentialPool('openai-codex', rows)
                a._credential_pool_entry_id = 'A'
                retry = TurnRetryState()
                service.put('runtime-A', provider='openai-codex', account_id=_identity_id_for('openai-codex', rows[1]),
                            expected_revision=service.get('runtime-A')['revision'])
                def attempt(number):
                    return perform_api_call(a, api_kwargs={'model': a.model, 'instructions': 'Fixture test', 'input': [{'role': 'user', 'content': 'hi'}]},
                        _original_api_kwargs={}, _llm_middleware_trace=[], _moa_prepared_request=None, _retry=retry, thinking_spinner=None,
                        retry_count=number, api_call_count=number, api_request_id=str(number), effective_task_id='t', turn_id='turn', interrupted=False)
                if soft:
                    failure = attempt(0).response
                else:
                    with self.assertRaises(openai.RateLimitError) as caught:
                        attempt(0)
                    classified = classify_api_error(caught.exception, provider=a.provider, model=a.model, base_url=a.base_url)
                with patch.object(pool, '_persist'), patch.object(pool, '_codex_quota_restored_upstream', return_value=False), \
                        patch.object(pool, '_resync_stale_entry', side_effect=lambda e: e), patch.object(pool, '_entry_needs_refresh', return_value=False), \
                        patch.object(a, '_swap_credential', side_effect=AssertionError('unsafe live swap')):
                    if soft:
                        from agent.turn_response_check import retry_invalid_response
                        verdict = retry_invalid_response(a, response=failure, error_details=['response.status=failed'], _retry=retry,
                            thinking_spinner=None, messages=[], api_messages=[], api_kwargs=None, active_system_prompt=None,
                            conversation_history=None, retry_count=0, max_retries=3, compression_attempts=0, api_call_count=1,
                            api_request_id='0', api_start_time=0.0, api_duration=0.1, effective_task_id='t', turn_id='turn')
                        self.assertEqual(verdict.action, 'continue')
                        self.assertEqual(a._account_usage_retry_snapshot[1].entry_id, 'A')
                    else:
                        self.assertEqual(recover_after_classification(a, caught.exception, classified, retry,
                            status_code=429, error_context=classified.error_context, messages=[], api_messages=[]), (True, True))
                    self.assertIsNotNone(attempt(1).response)
        self.assertEqual([h.get('chatgpt-account-id') for h in sent], ['B', 'A', 'B', 'A', 'B', 'A'])
        self.assertTrue(all(h['authorization'] == 'Bearer '+token(h['chatgpt-account-id']) for h in sent))
        self.assertTrue(all(h.get('x-openai-internal-codex-residency') == 'eu' for h in sent))
        self.assertEqual(observed, ['A', 'A', 'A'])
        self.assertEqual(a.api_key, token('A'))
        self.assertIsNone(self.s.current_snapshot(a))

    def test_real_anthropic_worker_wire_in_both_modes(self):
        import httpx, json
        from run_agent import AIAgent
        from agent.turn_api_call import perform_api_call
        from agent.account_usage_cache import _identity_id_for
        from agent import anthropic_adapter
        self.credentials([entry('A', source='manual').to_dict(), entry('B', source='manual').to_dict()])
        with patch('agent.model_metadata.get_model_context_length', return_value=128000):
            a = AIAgent(api_key='fixture-A', base_url='https://api.anthropic.com', model='claude-fixture',
                provider='anthropic', quiet_mode=True, skip_context_files=True, skip_memory=True,
                enabled_toolsets=[], max_iterations=1, platform='desktop', session_id='durable-A')
        self.agent = self.record['agent'] = a
        observed, sent = [], []
        integration = self.s.ExecutionIntegration(observe=lambda a, s: observed.append(s.entry_id))
        integration.install()
        self.addCleanup(integration.unload)
        service = self.s.SelectionService(self.home, server=self.server)
        manager = SimpleNamespace(_middleware={'llm_execution': [integration.middleware]}, _report_hook_failure=Mock())
        message = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'model': a.model,
                   'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn',
                   'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        def send(client, request, **kw):
            body = json.loads(request.content)
            sent.append((dict(request.headers), body.get('stream', False)))
            if not body.get('stream'):
                return httpx.Response(200, request=request, json=message)
            events = [('message_start', {'type': 'message_start', 'message': {**message, 'content': [], 'stop_reason': None}}),
                ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'ok'}}),
                ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 1}}),
                ('message_stop', {'type': 'message_stop'})]
            data = ''.join('event: '+name+'\ndata: '+json.dumps(event)+'\n\n' for name, event in events)
            return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'}, content=data.encode())
        with patch.dict(sys.modules, {'tui_gateway.server': self.server}), patch('hermes_cli.plugins._delivery_manager', return_value=manager), \
                patch.object(httpx.Client, 'send', send), patch.object(anthropic_adapter, '_custom_provider_extra_headers', return_value={}), \
                patch.object(anthropic_adapter, '_get_claude_code_version', return_value='0.0.fixture'):
            for stream in (False, True):
                a._disable_streaming = not stream
                service.put('runtime-A', provider='anthropic', account_id=_identity_id_for('anthropic', entry('B')),
                            expected_revision=service.get('runtime-A')['revision'])
                result = perform_api_call(a, api_kwargs={'model': a.model, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': 'hi'}]},
                    _original_api_kwargs={}, _llm_middleware_trace=[], _moa_prepared_request=None, _retry=SimpleNamespace(), thinking_spinner=None,
                    retry_count=0, api_call_count=0, api_request_id=str(stream), effective_task_id='t', turn_id='turn', interrupted=False)
                self.assertIsNotNone(result.response)
        self.assertEqual([is_stream for _, is_stream in sent], [False, True])
        self.assertTrue(all(headers.get('authorization') == 'Bearer sk-ant-oat01-fixture-B' for headers, _ in sent))
        self.assertEqual(observed, ['B', 'B'])
        self.assertEqual(a.api_key, 'fixture-A')

    def test_late_middleware_cannot_override_selected_wire_identity(self):
        from agent import chat_completion_helpers as helpers
        from agent.turn_api_call import perform_api_call
        from agent.account_usage_cache import _identity_id_for
        from types import MethodType
        self.credentials([entry('B', source='manual').to_dict()])
        service = self.s.SelectionService(self.home, server=self.server)
        service.put('runtime-A', provider='anthropic', account_id=_identity_id_for('anthropic', entry('B')), expected_revision=0)
        integration = self.s.ExecutionIntegration(observe=lambda *args: None)
        integration.install()
        self.addCleanup(integration.unload)
        a = self.agent
        a.platform, a.api_mode, a._disable_streaming = 'desktop', 'anthropic_messages', False
        a._has_stream_consumers = lambda: True
        a._has_pending_redirect = lambda: False
        a._interruptible_streaming_api_call = lambda req, **kw: helpers.interruptible_streaming_api_call(a, req, **kw)
        def hostile(next_call, request, **context):
            return next_call({**request, 'extra_headers': {'Authorization': 'Bearer other'}})
        manager = SimpleNamespace(_middleware={'llm_execution': [integration.middleware, hostile]}, _report_hook_failure=Mock())
        with patch.dict(sys.modules, {'tui_gateway.server': self.server}), patch('hermes_cli.plugins._delivery_manager', return_value=manager):
            with self.assertRaisesRegex(self.s.SelectionRejected, 'identity headers'):
                perform_api_call(a, api_kwargs={'model': a.model}, _original_api_kwargs={},
                    _llm_middleware_trace=[], _moa_prepared_request=None, _retry=SimpleNamespace(), thinking_spinner=None,
                    retry_count=0, api_call_count=0, api_request_id='late', effective_task_id='t', turn_id='turn', interrupted=False)
        manager._report_hook_failure.assert_not_called()
        self.assertFalse(hasattr(a, '_account_usage_failed_attempt'))

    def test_compute_child_borrowed_lease_executes_selected_request(self):
        from tui_gateway.session_lifecycle import _install_borrowed_lease
        lease = self.lease()
        self.record.pop('active_session_lease')
        _install_borrowed_lease('runtime-A', self.record,
            {'active_session_lease': {'lease_id': lease.lease_id, 'session_id': 'durable-A'}, 'source': 'desktop'})
        self.test_turn_boundary_consumes_once_and_records_selected_success()

    def test_other_chat_same_session_label_cannot_take_selection(self):
        from agent.account_usage_cache import _identity_id_for
        from agent.turn_api_call import perform_api_call
        self.credentials([entry('B', source='manual').to_dict()])
        service = self.s.SelectionService(self.home, server=self.server)
        service.put('runtime-A', provider='anthropic', account_id=_identity_id_for('anthropic', entry('B')), expected_revision=0)
        other = SimpleNamespace(**vars(self.agent))
        self.server._sessions['runtime-other'] = dict(self.record, agent=other)
        other.platform, other.api_mode, other._disable_streaming = 'desktop', 'anthropic_messages', False
        other._has_stream_consumers = lambda: True
        other._has_pending_redirect = lambda: False
        sent = []
        other._interruptible_streaming_api_call = lambda req, **kw: sent.append(self.s.current_snapshot(other)) or 'normal'
        integration = self.s.ExecutionIntegration(observe=lambda *args: None)
        integration.install()
        self.addCleanup(integration.unload)
        manager = SimpleNamespace(_middleware={'llm_execution': [integration.middleware]}, _report_hook_failure=Mock())
        with patch.dict(sys.modules, {'tui_gateway.server': self.server}), patch('hermes_cli.plugins._delivery_manager', return_value=manager):
            perform_api_call(other, api_kwargs={}, _original_api_kwargs={}, _llm_middleware_trace=[], _moa_prepared_request=None,
                _retry=SimpleNamespace(), thinking_spinner=None, retry_count=0, api_call_count=0,
                api_request_id='other', effective_task_id='t', turn_id='turn', interrupted=False)
        self.assertEqual(sent, [None])
        self.assertEqual(service.get('runtime-A')['state'], 'pending')

    def test_turn_boundary_consumes_once_and_records_selected_success(self):
        from agent.account_usage_cache import _identity_id_for
        from agent.turn_api_call import perform_api_call
        self.credentials([entry('A', source='manual').to_dict(), entry('B', source='manual').to_dict()])
        service = self.s.SelectionService(self.home, server=self.server)
        service.put('runtime-A', provider='anthropic', account_id=_identity_id_for('anthropic', entry('B')), expected_revision=0)
        self.assertTrue(hasattr(self.s, 'ExecutionIntegration'), 'execution installation missing')
        observed, sent = [], []
        integration = self.s.ExecutionIntegration(observe=lambda a, snap: observed.append(snap.entry_id))
        integration.install()
        self.addCleanup(integration.unload)
        self.agent.platform = 'desktop'
        self.agent.api_mode = 'anthropic_messages'
        self.agent._disable_streaming = False
        self.agent._has_stream_consumers = lambda: True
        self.agent._has_pending_redirect = lambda: False
        self.agent._interruptible_streaming_api_call = lambda req, **kw: sent.append(self.s.current_snapshot(self.agent)) or 'ok'
        manager = SimpleNamespace(_middleware={'llm_execution': [integration.middleware]}, _report_hook_failure=Mock())
        with patch.dict(sys.modules, {'tui_gateway.server': self.server}), patch('hermes_cli.plugins._delivery_manager', return_value=manager):
            for n in range(3):
                if n == 2:
                    self.agent._account_usage_retry_snapshot = ('turn', sent[0])
                    self.agent.provider = 'openrouter'  # normal core provider fallback
                result = perform_api_call(self.agent, api_kwargs={'model': self.agent.model}, _original_api_kwargs={},
                    _llm_middleware_trace=[], _moa_prepared_request=None, _retry=SimpleNamespace(), thinking_spinner=None,
                    retry_count=0, api_call_count=n, api_request_id=str(n), effective_task_id='t', turn_id='turn', interrupted=False)
                self.assertEqual(result.response, 'ok')
        self.assertEqual([s.entry_id if s else None for s in sent], ['B', None, None])
        self.assertEqual(observed, ['B'])
        self.agent.provider = 'anthropic'
        self.assertEqual(service.get('runtime-A')['state'], 'consumed')
        manager._report_hook_failure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
