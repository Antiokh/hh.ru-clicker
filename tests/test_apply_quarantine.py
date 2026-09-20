import json
from unittest.mock import Mock

import pytest

from app import apply_quarantine as quarantine
from tests.test_manager_auto_reconcile import setup


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(quarantine, 'PATH', tmp_path / 'quarantine.json')


def exhausted(state):
    state.pending_apply.update(reconcile_attempts=3, reconcile_last_error='unconfirmed')


def test_release_preserves_exclusion_and_counts(setup):
    bot, state, receipt = setup
    exhausted(state)
    count = state.daily_sent
    assert bot._quarantine_exhausted_apply(state)
    assert not state.paused and not state.pending_apply
    assert state.daily_sent == count
    assert quarantine.blocked(state.acc, 'vacancy-a')
    assert not quarantine.blocked(state.acc, 'another')
    assert not quarantine.blocked({'user_id': 'another-account'}, 'vacancy-a')
    assert json.loads(quarantine.PATH.read_text())['owner-a']['vacancy-a']['resume_id'] == 'resume-pinned'
    receipt.assert_not_called()


@pytest.mark.parametrize('reason', ['auth', 'rate_limit', 'read_timeout'])
def test_access_and_network_failures_do_not_resume(setup, reason):
    bot, state, _ = setup
    exhausted(state)
    state.pending_apply['reconcile_last_error'] = reason
    assert not bot._quarantine_exhausted_apply(state)
    assert state.paused and state.pending_apply


def test_disk_failure_does_not_resume(setup, monkeypatch):
    bot, state, _ = setup
    exhausted(state)
    monkeypatch.setattr(quarantine, '_atomic_write_json', Mock(side_effect=OSError))
    assert not bot._quarantine_exhausted_apply(state)
    assert state.paused and state.pending_apply


def test_pause_save_failure_keeps_exclusion(setup, monkeypatch):
    bot, state, _ = setup
    exhausted(state)
    monkeypatch.setattr(bot, '_persist_pauses', Mock(side_effect=OSError))
    assert not bot._quarantine_exhausted_apply(state)
    assert state.paused and state.pending_apply
    assert quarantine.blocked(state.acc, 'vacancy-a')


def test_corruption_blocks_dispatch():
    quarantine.PATH.write_text('broken')
    assert quarantine.blocked({'user_id': 'owner-a'}, 'vacancy-a')


def test_mobile_dispatch_does_not_contact_hh(setup, monkeypatch):
    from app import mobile_apply
    bot, state, _ = setup
    exhausted(state)
    assert bot._quarantine_exhausted_apply(state)
    request = Mock(side_effect=AssertionError('Must not contact HH'))
    monkeypatch.setattr(mobile_apply, 'mobile_request', request)
    result = mobile_apply.submit_response(state.acc, 'vacancy-a', 'resume-pinned')
    assert result['error_type'] == 'outcome_unknown'
    request.assert_not_called()


@pytest.mark.parametrize('flag', ['hard_stopped', 'limit_exceeded', 'cookies_expired'])
def test_other_protections_are_preserved(setup, flag):
    bot, state, _ = setup
    exhausted(state)
    setattr(state, flag, True)
    assert not bot._quarantine_exhausted_apply(state)
    assert state.paused and state.pending_apply
