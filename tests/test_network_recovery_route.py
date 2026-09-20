"""Explicit network verification uses the same strict proof and durable guards."""
import asyncio
import copy
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.routes import apply as routes
from tests.test_auth_verification_route import auth_route
from tests.test_manager_unknown_outcome import manager


@pytest.fixture
def network_route(auth_route):
    bot, state, verifier = auth_route
    state.paused_reason = 'network_error'
    state.cookies_expired = False
    state.network_recovery = {
        'error_type': 'oauth_bridge_network', 'phase': 'oauth_bridge',
        'dispatched': False, 'reason': 'connect_timeout', 'attempts': 2,
        'account_key': bot._network_account_key(state.acc),
        'next_check_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        'last_started_at': None, 'last_error': 'network',
    }
    return bot, state, verifier


def test_explicit_network_proof_resumes_without_resetting_applications(network_route):
    bot, state, verifier = network_route
    state.sent = 7
    state.daily_sent = 141
    response = asyncio.run(routes.api_recheck_auth(0))
    assert response['ok'] and response['verified'] and not response['paused']
    assert not state.paused and state.network_recovery is None
    assert state.sent == 7 and state.daily_sent == 141
    assert state.auth_check_started_at is None
    bot._persist_pauses.assert_called_once_with(wait=True)


@pytest.mark.parametrize('reason', ['auth', 'challenge', 'rate_limit', 'unavailable', 'stale'])
def test_non_network_denial_stops_background_checks(network_route, reason):
    bot, state, verifier = network_route
    verifier.return_value = {'verified': False, 'reason': reason}
    result = asyncio.run(routes.api_recheck_auth(0))
    assert not result['ok'] and state.paused and not bot._can_mutate(state)
    assert state.network_recovery['last_error'] == reason
    assert state.network_recovery['next_check_at'] is None
    assert state.network_recovery['attempts'] == 2
    assert state.auth_check_started_at is None


def test_manual_failed_network_probe_does_not_reset_retry_budget(network_route):
    bot, state, verifier = network_route
    original = copy.deepcopy(state.network_recovery)
    verifier.return_value = {'verified': False, 'reason': 'network'}
    result = asyncio.run(routes.api_recheck_auth(0))
    assert not result['ok'] and state.paused
    assert state.network_recovery['attempts'] == original['attempts']
    assert state.network_recovery['next_check_at'] == original['next_check_at']
    assert state.network_recovery['last_error'] == 'network'


@pytest.mark.parametrize('record', [None, {}, [], 'invalid'])
def test_missing_or_malformed_record_never_starts_verification(network_route, record):
    bot, state, verifier = network_route
    state.network_recovery = record
    result = asyncio.run(routes.api_recheck_auth(0))
    assert not result['ok'] and state.paused
    verifier.assert_not_called()


@pytest.mark.parametrize('change', ['record', 'mode', 'manual', 'global', 'pending'])
def test_late_network_proof_preserves_newer_context(network_route, change):
    bot, state, verifier = network_route
    def verify(acc):
        if change == 'record': state.network_recovery['attempts'] += 1
        elif change == 'mode': state.acc['mode'] = 'web'
        elif change == 'manual': state.paused_reason = 'manual'
        elif change == 'global': bot.paused = True
        elif change == 'pending': state.pending_apply = {'vacancy_id': 'synthetic'}
        return {'verified': True}
    verifier.side_effect = verify
    result = asyncio.run(routes.api_recheck_auth(0))
    assert not result['ok'] and state.paused
    bot._persist_pauses.assert_not_called()


def test_network_verifier_exception_is_safe_and_stops_automatic_checks(network_route):
    bot, state, verifier = network_route
    verifier.side_effect = RuntimeError('synthetic-private-value')
    result = asyncio.run(routes.api_recheck_auth(0))
    assert not result['ok'] and state.paused
    assert 'synthetic-private-value' not in str(result)
    assert state.network_recovery['last_error'] == 'unavailable'
    assert state.network_recovery['next_check_at'] is None


def test_compose_default_is_explicit_direct_not_proxy_dependent():
    compose = (Path(__file__).resolve().parents[1] / 'docker-compose.yml').read_text()
    bot_service = compose.split('  hh-bot:', 1)[1]
    assert re.search(r'^\s+HH_PROXY:\s*""\s*$', bot_service, re.MULTILINE)
    assert 'depends_on:' not in bot_service
