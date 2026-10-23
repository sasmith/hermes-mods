"""Real PluginManager composition; synthetic credentials and isolated homes only."""
import gc
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).parent))
from test_selection_backend import Backend


class Combined(unittest.TestCase):
    def setUp(self):
        gc.collect()
        import tempfile
        temp = patch.object(tempfile, 'tempdir', str(Path.home() / '.hermes' / 'cache' / 'scratch'))
        temp.start()
        self.addCleanup(temp.stop)
        Backend.setUp(self)
        from hermes_logging import _reset_queued_handlers
        self.addCleanup(_reset_queued_handlers)
        # Warm core while this home has no enabled plugins: avoid recursive
        # singleton discovery during run_agent's own module initialization.
        import run_agent
        source = Path(__file__).resolve().parents[2]
        for name in ('account-usage', 'fast-rate-limit-fallback'):
            shutil.copytree(source / name, self.home / 'plugins' / name,
                ignore=shutil.ignore_patterns('tests', '__pycache__', 'node_modules'))
        (self.home / 'config.yaml').write_text('plugins:\n  enabled: [account-usage, fast-rate-limit-fallback]\n')
        env = patch.dict(os.environ, {'HERMES_HOME': str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def load(self, order=('account-usage', 'fast-rate-limit-fallback')):
        from hermes_cli.plugins import PluginManager
        manager = PluginManager()
        self.addCleanup(manager.unload)
        # Only ordering is forced: real discovery, gates, registration, ownership.
        with patch('hermes_cli.plugins.resolve_plugin_load_order', side_effect=lambda manifests: [n for n in order if n in manifests]):
            manager.discover_and_load()
        return manager

    def assert_active(self, manager):
        from agent import agent_runtime_helpers as h
        for name in ('account-usage', 'fast-rate-limit-fallback'):
            self.assertIsNone(manager._plugins[name].error)
        integration = manager._middleware['llm_execution'][0].__self__
        self.assertTrue(integration.active, integration.error)
        self.assertTrue(getattr(h._recover_rate_limit, '_fast_rate_limit_fallback', False))
        return integration

    def test_discovery_both_orders_and_independent_unload_reload(self):
        from agent import agent_runtime_helpers as h
        for order in [('account-usage', 'fast-rate-limit-fallback'), ('fast-rate-limit-fallback', 'account-usage')]:
            for first in order:
                with self.subTest(order=order, unload=first):
                    original_recover, original_rate = h.recover_with_credential_pool, h._recover_rate_limit
                    manager = self.load(order)
                    self.assert_active(manager)
                    recover, rate = h.recover_with_credential_pool, h._recover_rate_limit
                    manager.discover_and_load()
                    self.assertIs(h.recover_with_credential_pool, recover)
                    manifest = manager._plugins[first].manifest
                    manager.unload(first)
                    self.assertIs(h.recover_with_credential_pool, original_recover if first == 'account-usage' else recover)
                    self.assertIs(h._recover_rate_limit, original_rate if first == 'fast-rate-limit-fallback' else rate)
                    manager._load_plugin(manifest)
                    self.assert_active(manager)
                    self.assertIs(h._recover_rate_limit, rate) if first == 'account-usage' else self.assertIs(h.recover_with_credential_pool, recover)
                    manager.discover_and_load(force=True)
                    self.assert_active(manager)
                    manager.unload()
                    self.assertIs(h.recover_with_credential_pool, original_recover)
                    self.assertIs(h._recover_rate_limit, original_rate)

    def test_foreign_wrappers_and_captured_core_drift_refused(self):
        from agent import agent_runtime_helpers as h
        from functools import wraps
        manager = self.load()
        integration = self.assert_active(manager)
        fast = manager._plugins['fast-rate-limit-fallback'].module
        manager.unload('fast-rate-limit-fallback')
        original = h.recover_with_credential_pool
        @wraps(original)
        def foreign(*args, **kwargs):
            return original(*args, **kwargs)
        # wraps copies owner and __wrapped__; neither permits admission.
        for changed in (foreign, lambda *a, **kw: (False, False)):
            callbacks = []
            from types import SimpleNamespace
            with patch.object(h, 'recover_with_credential_pool', changed):
                fast.register(SimpleNamespace(on_unload=callbacks.append))
            self.assertEqual(callbacks, [])
            self.assertFalse(hasattr(h._recover_rate_limit, '_fast_rate_limit_fallback'))
        base = original.__wrapped__
        code = base.__code__
        try:
            # Same source location/signature, changed live behavior.
            base.__code__ = code.replace(co_consts=code.co_consts + ('drift',))
            with self.assertRaisesRegex(ValueError, 'captured core'):
                fast._audited_recovery_base(original)
        finally:
            base.__code__ = code
        # The pinned adapter file must not admit even a source-only change.
        with patch.object(fast.Path, 'read_bytes', return_value=b'changed adapter'):
            with self.assertRaisesRegex(ValueError, 'adapter changed'):
                fast._audited_recovery_base(original)
        # Exact code but no active ledger ownership is rejected too.
        with patch.object(integration, 'active', False):
            with self.assertRaisesRegex(ValueError, 'unowned'):
                fast._audited_recovery_base(original)
        manager.unload()
        # Account's own guard still refuses a foreign recovery seam.
        with patch.object(h, 'recover_with_credential_pool', foreign):
            with self.assertRaises(self.s.SelectionRejected):
                self.s.ExecutionIntegration(observe=lambda *a: None).install()

    def test_warm_fingerprint_caches_skip_seam_compiles_and_still_catch_drift(self):
        """A cold compile of the audited seam files took seconds under load and pushed both
        plugins' loads past core's 10 s deadline. With both caches warm (same home, same file
        bytes) neither plugin compiles them; a drifted live code object still misses the cache,
        so the compile stays authoritative."""
        from agent import agent_runtime_helpers as h
        guards = {'_audited_recovery_base', 'verified'}  # FRL's / account-usage's compile sites
        compiled, recording = [], [False]

        def audit(event, args):
            if recording[0] and event == 'compile':
                compiled.append((str(args[1]), sys._getframe(1).f_code.co_name))
        sys.addaudithook(audit)
        manager = self.load()
        self.assert_active(manager)
        manager.unload()
        cache = self.home / 'cache' / 'fast-rate-limit-fallback' / 'code-fingerprints.json'
        self.assertTrue(cache.is_file())
        recording[0] = True
        try:
            manager = self.load()
        finally:
            recording[0] = False
        self.assert_active(manager)
        self.assertEqual([c for c in compiled if c[1] in guards], [])
        fast = manager._plugins['fast-rate-limit-fallback'].module
        original = h.recover_with_credential_pool
        base = original.__wrapped__
        code = base.__code__
        try:
            base.__code__ = code.replace(co_consts=code.co_consts + ('drift',))
            with self.assertRaisesRegex(ValueError, 'captured core'):
                fast._audited_recovery_base(original, fast._Fingerprints(self.home))
        finally:
            base.__code__ = code
        self.assertIs(fast._audited_recovery_base(original, fast._Fingerprints(self.home)), base)

    def test_selected_and_unselected_long_reset_rotation_and_fallback(self):
        import time
        from types import SimpleNamespace
        from run_agent import AIAgent
        from agent.credential_pool import CredentialPool
        from agent.account_usage_cache import _identity_id_for
        from test_request_selection import entry
        class LongReset(Exception):
            status_code = 429
            def __init__(self):
                super().__init__('This request would exceed your account rate limit. Please try again later.')
                self.body = {'error': {'message': str(self), 'reset_at': time.time() + 36000}}
                self.response = SimpleNamespace(headers={'retry-after': '36000'})
        manager = self.load()
        self.assert_active(manager)
        delivery = patch('hermes_cli.plugins.get_plugin_manager', return_value=manager)
        delivery.start()
        self.addCleanup(delivery.stop)
        for selected in (True, False):
            for rotate in (True, False):
                with self.subTest(selected=selected, rotate=rotate):
                    rows = [entry('A', source='manual'), entry('B', source='manual',
                        base_url='https://api.anthropic.com' if rotate else 'https://relay.example/anthropic')]
                    Backend.credentials(self, [e.to_dict() for e in rows])
                    with patch('agent.model_metadata.get_model_context_length', return_value=128000):
                        a = AIAgent(api_key=rows[0].runtime_api_key, provider='anthropic', model='claude-fixture',
                            base_url='https://api.anthropic.com', quiet_mode=True, skip_context_files=True,
                            skip_memory=True, enabled_toolsets=[], max_iterations=1, platform='desktop',
                            session_id='durable-A', fallback_model=[{'provider': 'zai', 'model': 'test-fallback'}])
                    self.record['agent'] = a
                    a._disable_streaming = True
                    a._credential_pool = pool = CredentialPool('anthropic', rows)
                    a._credential_pool_entry_id = 'A'
                    a._api_max_retries = 2
                    if selected:
                        a.api_key = 'fixture-unchanged-default'
                        a._credential_pool_entry_id = 'unchanged-default'
                        service = self.s.SelectionService(self.home, server=self.server)
                        service.put('runtime-A', provider='anthropic', account_id=_identity_id_for('anthropic', rows[0]),
                            expected_revision=service.get('runtime-A')['revision'])
                    sent = []
                    def api_call(kwargs):
                        snap = self.s.current_snapshot(a)
                        sent.append((a.provider, snap.entry_id if snap else a._credential_pool_entry_id))
                        if len(sent) == 1:
                            raise LongReset()
                        if a.provider == 'anthropic':
                            return SimpleNamespace(content=[SimpleNamespace(type='text', text='worked')],
                                stop_reason='end_turn', model=a.model, usage=None)
                        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='worked', tool_calls=None),
                            finish_reason='stop')], model=a.model, usage=None)
                    fallback = SimpleNamespace(api_key='fixture-fallback', base_url='https://api.z.ai/api/paas/v4',
                        _custom_headers=None, default_headers=None)
                    with patch.dict(sys.modules, {'tui_gateway.server': self.server}), \
                            patch('hermes_cli.plugins._delivery_manager', return_value=manager), \
                            patch.object(a, '_interruptible_api_call', side_effect=api_call), \
                            patch.object(a, '_persist_session'), patch.object(a, '_save_trajectory'), \
                            patch.object(a, '_cleanup_task_resources'), patch.object(pool, '_persist'), \
                            patch('agent.process_bootstrap.OpenAI', return_value=Mock()), \
                            patch('agent.turn_api_error.interruptible_backoff_sleep') as sleep, \
                            patch('agent.auxiliary_client.resolve_provider_client', return_value=(fallback, 'test-fallback')), \
                            patch('hermes_cli.model_normalize.normalize_model_for_provider', side_effect=lambda m, p: m), \
                            patch('agent.model_metadata.get_model_context_length', return_value=128000), \
                            patch('hermes_cli.config.load_config', return_value={'fallback': {'min_switch_reset_seconds': 0}}):
                        result = a.run_conversation('hello')
                    self.assertTrue(result['completed'], result)
                    self.assertEqual(result['final_response'], 'worked')
                    self.assertEqual(len(sent), 2)
                    self.assertEqual(sent[0], ('anthropic', 'A'))
                    self.assertEqual(sent[1][0], 'anthropic' if rotate else 'zai')
                    if rotate:
                        self.assertEqual(sent[1][1], 'B')
                    if selected and rotate:
                        self.assertEqual(a.api_key, 'fixture-unchanged-default')
                        self.assertEqual(a._credential_pool_entry_id, 'unchanged-default')
                    self.assertGreater(pool.entries()[0].model_cooldowns['claude-fixture'], time.time())
                    self.assertFalse(pool.entries()[1].model_cooldowns)
                    sleep.assert_not_called()
                    self.s.clear_attempt(a)
                    a.__dict__.pop('_account_usage_retry_snapshot', None)
                    del a
                    gc.collect()

    def test_unselected_short_unknown_and_exact_cutoff_unchanged(self):
        # Core (fork f8ff20e57b) rotates on the first 429 once the window exceeds
        # LIVE_RETRY_WAIT_CAP_S (60s); the plugin's >600s bump only re-enters that
        # same rotate branch. Unknown and short windows still retry once. Unpatched
        # upstream core has no 60s branch: there only the plugin's >600s bump rotates.
        import inspect
        from agent import agent_runtime_helpers as h
        from agent.retry_utils import LIVE_RETRY_WAIT_CAP_S
        manager = self.load()
        self.assert_active(manager)
        probe = Mock(return_value=True)
        pool = Mock()
        pool.entries.return_value = []
        pool.current.return_value = None
        with patch('agent.fallback_cooldown.time.time', return_value=2_000_000_000):
            inspect.unwrap(h._recover_rate_limit)(pool, has_retried_429=False,
                error_context={'reset_at': 2_000_000_061}, api_key_hint=None, credential_id=None,
                rotate_and_swap=probe)
        cap = int(LIVE_RETRY_WAIT_CAP_S) if probe.called else 600
        for delay in (None, 30, cap, cap + 1, 599, 600, 601):
            pool = Mock()
            pool.entries.return_value = []
            pool.current.return_value = None
            rotate = Mock(return_value=True)
            with patch('agent.fallback_cooldown.time.time', return_value=2_000_000_000):
                result = h._recover_rate_limit(pool, has_retried_429=False,
                    error_context={} if delay is None else {'reset_at': 2_000_000_000 + delay},
                    api_key_hint=None, credential_id=None, rotate_and_swap=rotate)
            long_window = delay is not None and delay > cap
            self.assertEqual(result, (True, False) if long_window else (False, True), delay)
            self.assertEqual(rotate.call_count, int(long_window), delay)


if __name__ == '__main__':
    unittest.main()
