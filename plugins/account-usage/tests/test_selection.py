"""Safety gates for the approved Account Usage selection seams.

Uses REAL installed core methods with only disk/network boundaries replaced.
Never loads the user's credential store and never performs provider requests.
Characterization pins the unsafe core seams (reclaim, _swap_credential) the
plugin must never use; Contract pins the plugin's own path on the same fixtures.
"""
from contextlib import ExitStack
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.environ.get('HERMES_AGENT_ROOT') or str(Path.home() / '.hermes' / 'hermes-agent'))
import hermes_bootstrap  # noqa: F401
from agent.credential_pool import CredentialPool, PooledCredential
from agent.client_lifecycle import ClientLifecycleMixin


def probe_reclaim(target='ready'):
    """Reclaim a READY account while another account has a FUTURE cooldown."""
    now = time.time()
    entries = [
        PooledCredential(provider='openai-codex', id='ready', label='duplicate label',
                         auth_type='oauth', priority=0, source='fixture',
                         access_token='fixture-ready-not-a-secret', last_status='ok'),
        PooledCredential(provider='openai-codex', id='cooling', label='duplicate label',
                         auth_type='oauth', priority=1, source='fixture',
                         access_token='fixture-cooling-not-a-secret', last_status='exhausted',
                         last_status_at=now, last_error_reset_at=now + 86400),
    ]
    with patch('agent.credential_pool.get_pool_strategy', return_value='priority'):
        pool = CredentialPool('openai-codex', entries)
    with ExitStack() as stack:
        # Isolate external I/O only. reclaim/_available_entries/_adopt are real.
        stack.enter_context(patch.object(pool, '_resync_stale_entry', side_effect=lambda e: e))
        stack.enter_context(patch.object(pool, '_entry_needs_refresh', return_value=False))
        probe = stack.enter_context(patch.object(pool, '_codex_quota_restored_upstream', return_value=True))
        persist = stack.enter_context(patch.object(pool, '_persist'))
        chosen = pool.reclaim(target, model='gpt-fixture')
        return {
            'chosen': chosen.id if chosen else None,
            'cooldown_status': next(e.last_status for e in pool.entries() if e.id == 'cooling'),
            'probe_calls': probe.call_count,
            'persist_calls': persist.call_count,
        }


def probe_anthropic_dispatch():
    """Swap to B, then build the actual request client via the real core path."""
    agent = SimpleNamespace(
        provider='anthropic', model='claude-fixture', api_mode='anthropic_messages',
        api_key='fixture-A', base_url='https://api.anthropic.com',
        _anthropic_api_key='fixture-A', _anthropic_base_url='https://api.anthropic.com',
        _anthropic_client=SimpleNamespace(close=lambda: None),
        _build_direct_anthropic_client=lambda key, base: SimpleNamespace(close=lambda: None),
        _anthropic_oauth_flag=lambda token: False,
        _checkout_request_slot=lambda slot, key: (None, None),
        _store_request_slot=lambda slot, client, key: None,
        _anthropic_log_context=lambda: 'fixture',
    )
    agent._try_refresh_anthropic_client_credentials = lambda: ClientLifecycleMixin._try_refresh_anthropic_client_credentials(agent)
    # Capture which key the actual request-client constructor receives, without HTTP.
    agent._request_anthropic_client_key = lambda: ('anthropic', agent._anthropic_api_key)
    agent._build_anthropic_client_for_key = lambda key: SimpleNamespace(selected_key=key[1])
    entry = SimpleNamespace(id='B', runtime_api_key='fixture-B', access_token='fixture-B',
                            runtime_base_url='https://api.anthropic.com', base_url=None)
    with patch('hermes_cli.anon_auth.route_can_serve_model', return_value=True), \
         patch('agent.anthropic_credentials.resolve_anthropic_token', return_value='fixture-A'), \
         patch('agent.auxiliary_key_rotation.rotate_runtime_main_api_key'):
        swapped = ClientLifecycleMixin._swap_credential(agent, entry)
        after_swap = agent._anthropic_api_key
        client = ClientLifecycleMixin._create_request_anthropic_client(agent, reason='selection-test')
    return {'swapped': swapped, 'after_swap': after_swap,
            'request_key': client.selected_key, 'recorded_entry_id': agent._credential_pool_entry_id}


class Characterization(unittest.TestCase):
    def test_ready_reclaim_can_clear_another_future_cooldown(self):
        self.assertEqual(probe_reclaim(), {'chosen': 'ready', 'cooldown_status': 'ok',
                                         'probe_calls': 1, 'persist_calls': 1})

    def test_cooling_reclaim_can_clear_selected_future_cooldown(self):
        self.assertEqual(probe_reclaim('cooling')['chosen'], 'cooling')

    def test_request_client_reverts_successful_anthropic_swap(self):
        self.assertEqual(probe_anthropic_dispatch(), {
            'swapped': True, 'after_swap': 'fixture-B', 'request_key': 'fixture-A',
            'recorded_entry_id': 'B'})


def _load_selection():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / 'selection.py'
    name = 'account_usage_selection_contract'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses resolve their module by name
        spec.loader.exec_module(module)
    return sys.modules[name]


def probe_plugin_choice(target='ready'):
    """The plugin's own selection path over the SAME fixture rows probe_reclaim uses."""
    from agent.account_usage_cache import _identity_id_for
    selection = _load_selection()
    now = time.time()
    entries = [
        PooledCredential(provider='openai-codex', id='ready', label='duplicate label',
                         auth_type='oauth', priority=0, source='fixture',
                         access_token='fixture-ready-not-a-secret', last_status='ok',
                         base_url='https://chatgpt.com/backend-api/codex'),
        PooledCredential(provider='openai-codex', id='cooling', label='duplicate label',
                         auth_type='oauth', priority=1, source='fixture',
                         access_token='fixture-cooling-not-a-secret', last_status='exhausted',
                         last_status_at=now, last_error_reset_at=now + 86400,
                         base_url='https://chatgpt.com/backend-api/codex'),
    ]
    target_entry = next(e for e in entries if e.id == target)
    with patch.object(CredentialPool, '_persist') as persist, \
         patch.object(CredentialPool, 'reclaim') as reclaim:
        try:
            snapshot = selection.choose_snapshot(
                entries, provider='openai-codex', model='gpt-fixture',
                account_id=_identity_id_for('openai-codex', target_entry),
                base_url='https://chatgpt.com/backend-api/codex')
            chosen = snapshot.entry_id
        except selection.SelectionRejected:
            chosen = None
    return {'chosen': chosen,
            'cooldown_status': next(e.last_status for e in entries if e.id == 'cooling'),
            'persist_calls': persist.call_count, 'reclaim_calls': reclaim.call_count}


def probe_plugin_anthropic_dispatch():
    """Same agent fixture as probe_anthropic_dispatch, through the plugin's factory."""
    selection = _load_selection()
    agent = SimpleNamespace(provider='anthropic', model='claude-fixture',
                            api_key='fixture-A', base_url='https://api.anthropic.com',
                            _anthropic_api_key='fixture-A')
    def build(key):
        return SimpleNamespace(selected_key=key[1], default_headers={'x-api-key': key[1]},
                               close=lambda: None)
    agent._build_anthropic_client_for_key = build
    snapshot = selection.CredentialSnapshot('anthropic', 'claude-fixture', 'acct-B', 'B',
                                            'https://api.anthropic.com', 'fixture-B')
    create = selection.anthropic_factory(ClientLifecycleMixin._create_request_anthropic_client)
    token = selection._dispatch.set((agent, snapshot))
    try:
        client = create(agent, reason='selection-test')
    finally:
        selection._dispatch.reset(token)
    return {'request_key': client.selected_key, 'shared_key_after': agent._anthropic_api_key}


class Contract(unittest.TestCase):
    """The plugin's selection path must hold what the core seams above do not.

    These used to target the core seams directly (RED by design: those seams
    are why the plugin never calls reclaim/_swap_credential). They now pin the
    plugin's own path on the same fixtures.
    """
    def test_click_must_not_clear_other_accounts_cooldowns(self):
        result = probe_plugin_choice()
        self.assertEqual(result['chosen'], 'ready')
        self.assertEqual(result['cooldown_status'], 'exhausted',
                         'Selecting an account clears an unrelated future cooldown')
        self.assertEqual((result['persist_calls'], result['reclaim_calls']), (0, 0))

    def test_cooling_account_must_be_unavailable_not_reset(self):
        result = probe_plugin_choice('cooling')
        self.assertIsNone(result['chosen'], 'A cooling account was selectable')
        self.assertEqual(result['cooldown_status'], 'exhausted')
        self.assertEqual((result['persist_calls'], result['reclaim_calls']), (0, 0))

    def test_selected_anthropic_identity_must_reach_request_client(self):
        result = probe_plugin_anthropic_dispatch()
        self.assertEqual(result['request_key'], 'fixture-B',
                         'Request-client refresh overwrites the selection before dispatch')
        self.assertEqual(result['shared_key_after'], 'fixture-A',
                         'Selection must not swap the shared agent credential')


if __name__ == '__main__':
    unittest.main(verbosity=2)
