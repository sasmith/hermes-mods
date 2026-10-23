from types import SimpleNamespace
import time
from unittest.mock import MagicMock, patch
import logging

import pytest


def core_rotates_past_live_cap():
    """True on core lineages whose own _recover_rate_limit rotates on the first 429 once the
    window exceeds LIVE_RETRY_WAIT_CAP_S (60s; fork f8ff20e57b). Unpatched upstream retries
    once instead, so only the plugin's >600s cutoff rotates early. Probes the unwrapped core."""
    import inspect
    import agent.agent_runtime_helpers as helpers
    core = inspect.unwrap(helpers._recover_rate_limit)
    pool = MagicMock()
    pool.entries.return_value = []
    pool.current.return_value = None
    rotate = MagicMock(return_value=True)
    with patch('agent.fallback_cooldown.time.time', return_value=2_000_000_000):
        core(pool, has_retried_429=False, error_context={'reset_at': 2_000_000_061},
             api_key_hint=None, credential_id=None, rotate_and_swap=rotate)
    return rotate.called


def seams():
    import agent.agent_runtime_helpers as helpers
    import agent.turn_recovery as recovery
    import agent.turn_api_error as api_error
    import run_agent
    return [(helpers, '_recover_rate_limit'), (recovery, 'route_classified_error'),
            (api_error, 'route_classified_error'), (run_agent, '_pool_may_recover_from_rate_limit')]


def test_discovery_reload_and_unload_are_idempotent(loaded_local_plugin):
    manager = loaded_local_plugin
    first = [getattr(m, n) for m, n in seams()]
    manager.discover_and_load()
    assert [getattr(m, n) for m, n in seams()] == first
    manager.discover_and_load(force=True)
    for m, n in seams():
        function = getattr(m, n)
        assert function._fast_rate_limit_fallback
        assert not getattr(function.__wrapped__, '_fast_rate_limit_fallback', False)
    manager.unload()
    assert all(not getattr(getattr(m, n), '_fast_rate_limit_fallback', False) for m, n in seams())


@pytest.mark.parametrize('drift', ['missing', 'signature', 'body', 'alias', 'cutoff'])
def test_incompatible_core_warns_and_leaves_all_seams_alone(loaded_local_plugin, monkeypatch, caplog, drift):
    module = loaded_local_plugin._plugins['fast-rate-limit-fallback'].module
    loaded_local_plugin.unload()
    import agent.agent_runtime_helpers as helpers
    import agent.turn_api_error as api_error
    import agent.retry_utils as retry_utils
    if drift == 'missing':
        monkeypatch.delattr(helpers, '_recover_rate_limit')
    elif drift == 'signature':
        monkeypatch.setattr(helpers, '_recover_rate_limit', lambda new_required: None)
    elif drift == 'body':
        def changed(pool, *, has_retried_429, error_context, api_key_hint, credential_id, rotate_and_swap):
            return False, False
        monkeypatch.setattr(helpers, '_recover_rate_limit', changed)
    elif drift == 'alias':
        monkeypatch.setattr(api_error, 'route_classified_error', lambda *a, **k: None)
    else:
        monkeypatch.setattr(retry_utils, 'RETRY_AFTER_CAP_S', 601)
    before = [getattr(m, n, None) for m, n in seams()]
    monkeypatch.setattr(module.logger, '_compatibility_warning_emitted', False, raising=False)
    ctx = SimpleNamespace(on_unload=MagicMock())
    with caplog.at_level(logging.WARNING):
        module.register(ctx)
        module.register(ctx)
    assert [getattr(m, n, None) for m, n in seams()] == before
    assert sum('fast-rate-limit-fallback DISABLED' in r.message for r in caplog.records) == 1
    ctx.on_unload.assert_not_called()


# Fork core rotates past LIVE_RETRY_WAIT_CAP_S (60s) on its own since f8ff20e57b; the
# plugin's 600s cutoff only re-enters that same rotate branch. Upstream core retries once,
# so there only a reset strictly past 600s rotates on the first 429.
@pytest.mark.parametrize('delay, fork_rotated', [(30, False), (60, False), (61, True),
                                                 (599, True), (600, True), (601, True)])
def test_exact_cutoff(delay, fork_rotated):
    rotated = fork_rotated if core_rotates_past_live_cap() else delay > 600
    import agent.agent_runtime_helpers as helpers
    pool = MagicMock()
    pool.entries.return_value = []
    pool.current.return_value = None
    rotate = MagicMock(return_value=True)
    with patch('agent.fallback_cooldown.time.time', return_value=2_000_000_000):
        result = helpers._recover_rate_limit(pool, has_retried_429=False,
            error_context={'reset_at': 2_000_000_000 + delay}, api_key_hint=None,
            credential_id=None, rotate_and_swap=rotate)
    assert result == ((True, False) if rotated else (False, True))
    assert rotate.call_count == int(rotated)


def test_other_profile_keeps_original_transient_policy(monkeypatch, tmp_path):
    import agent.agent_runtime_helpers as helpers
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    original = getattr(helpers._recover_rate_limit, '__wrapped__', helpers._recover_rate_limit)
    results = []
    for function in (helpers._recover_rate_limit, original):
        rotate = MagicMock(return_value=True)
        pool = MagicMock()
        pool.current.return_value = None
        # Short window: the only case where the plugin's has_retried_429 bump
        # could differ from core (core itself rotates past 60s since f8ff20e57b).
        context = {'reset_at': time.time() + 30}
        token = set_hermes_home_override(tmp_path / 'another-profile')
        try:
            results.append((function(pool, has_retried_429=False, error_context=context,
                                     api_key_hint=None, credential_id=None,
                                     rotate_and_swap=rotate), rotate.call_count))
        finally:
            reset_hermes_home_override(token)
    assert results[0] == results[1] == ((False, True), 0)
