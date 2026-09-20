"""Synthetic network-only recovery, with no HH traffic or production storage."""
import ast
import copy
import inspect
import textwrap
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app import manager as module
from app.account_activity import activity_view, note_pause
from app.manager import BotManager
from app.state import AccountState
from tests.test_manager_unknown_outcome import manager


META = {"error_type": "oauth_bridge_network", "phase": "oauth_bridge",
        "dispatched": False, "reason": "connect_timeout"}


@pytest.fixture
def network(manager, monkeypatch):
    state = manager.account_states[0]
    state.acc.update(mode="oauth", user_id="synthetic-owner", cookies={})
    monkeypatch.setattr(module.CONFIG, "auto_pause_errors", 2)
    manager._persist_pauses = Mock()
    verifier = Mock(return_value={"verified": False, "reason": "network"})
    monkeypatch.setattr("app.auth_verification.verify_oauth_and_web_access", verifier)
    return manager, state, verifier


def hold(network):
    bot, state, _ = network
    for _ in range(2):
        state.consecutive_errors += 1
        bot._check_auto_pause(state, network_reason=bot._network_reason(META))
    assert state.paused_reason == "network_error"
    return datetime.fromisoformat(state.network_recovery["next_check_at"])


def test_only_fully_proven_network_series_qualifies(network):
    bot, state, _ = network
    next_at = hold(network)
    assert 0 < (next_at - datetime.now(timezone.utc)).total_seconds() <= 30
    assert state.network_recovery["attempts"] == 0
    assert state.pending_apply is None and state.daily_sent == 0
    public = bot.network_recovery_view(state)
    assert "account_key" not in public and "dispatched" not in public
    assert activity_view(state)["phase"] == "network_recovery"
    assert not activity_view(state)["requires_action"]


@pytest.mark.parametrize("metadata", [{}, {**META, "dispatched": True}, {**META, "dispatched": None},
    {**META, "error_type": "other"}, {**META, "reason": "private-secret"},
    {**META, "phase": "submission"}])
def test_unproven_errors_remain_manual(network, metadata):
    bot, state, verifier = network
    for _ in range(2):
        state.consecutive_errors += 1
        bot._check_auto_pause(state, network_reason=bot._network_reason(metadata))
    assert state.paused_reason == "auto_errors" and state.network_recovery is None
    assert not bot._network_probe_if_due(state)
    verifier.assert_not_called()


def test_mixed_error_streak_and_legacy_pause_never_migrate(network):
    bot, state, verifier = network
    state.consecutive_errors = 1
    bot._check_auto_pause(state)
    state.consecutive_errors += 1
    bot._check_auto_pause(state, network_reason="connect_timeout")
    assert state.paused_reason == "auto_errors" and state.network_recovery is None
    assert not bot._network_probe_if_due(state)
    verifier.assert_not_called()


def test_delays_grow_and_cap_without_post_retries(network):
    bot, state, verifier = network
    due = hold(network)
    for attempt, delay in enumerate([90, 300, 900, 900, 900], 1):
        assert not bot._network_probe_if_due(state, now=due - timedelta(seconds=1))
        assert bot._network_probe_if_due(state, now=due)
        assert state.network_recovery["attempts"] == attempt
        following = datetime.fromisoformat(state.network_recovery["next_check_at"])
        assert (following - due).total_seconds() == delay
        assert state.paused and not state.pending_apply
        due = following
    assert verifier.call_count == 5 and state.sent == state.daily_sent == 0


def test_success_requires_durable_reserve_and_commit(network):
    bot, state, verifier = network
    due = hold(network)
    bot._persist_pauses.reset_mock()
    def proof(acc):
        bot._persist_pauses.assert_called_once_with(wait=True)
        assert state.network_recovery["attempts"] == 1 and state.auth_check_started_at
        assert not bot._can_mutate(state)
        return {"verified": True}
    verifier.side_effect = proof
    assert bot._network_probe_if_due(state, now=due)
    assert not state.paused and state.network_recovery is None
    assert state.auth_check_started_at is None
    assert bot._persist_pauses.call_count == 2
    assert state.sent == state.daily_sent == 0


@pytest.mark.parametrize("reason", ["auth", "challenge", "rate_limit", "unavailable", "stale", "private-secret"])
def test_other_probe_failures_stop_schedule(network, reason):
    bot, state, verifier = network
    due = hold(network)
    verifier.return_value = {"verified": False, "reason": reason}
    bot._network_probe_if_due(state, now=due)
    assert state.paused and state.network_recovery["next_check_at"] is None
    assert state.network_recovery["attempts"] == 1
    assert not bot._network_probe_if_due(state, now=due + timedelta(days=1))
    assert verifier.call_count == 1
    assert activity_view(state)["requires_action"] and activity_view(state)["wait_until"] is None
    assert "private-secret" not in str(bot.network_recovery_view(state))


@pytest.mark.parametrize("block", ["global", "manual", "pending", "limit", "auth_busy", "receipt_busy", "stop", "owner"])
def test_blocked_probe_does_not_request_or_resume(network, block):
    bot, state, verifier = network
    due = hold(network)
    if block == "global": bot.paused = True
    elif block == "manual": state.paused_reason = "manual"
    elif block == "pending": state.pending_apply = {"vacancy_id": "synthetic"}
    elif block == "limit": state.limit_exceeded = True
    elif block == "auth_busy": state.auth_check_started_at = due.isoformat()
    elif block == "receipt_busy": state.receipt_check_started_at = due.isoformat()
    elif block == "stop": bot._stop_event.set()
    elif block == "owner": state.acc["user_id"] = "other-owner"
    assert not bot._network_probe_if_due(state, now=due)
    assert state.paused
    verifier.assert_not_called()


@pytest.mark.parametrize("change", ["manual", "global", "owner", "pending", "record"])
def test_late_successful_probe_cannot_clear_new_state(network, change):
    bot, state, verifier = network
    due = hold(network)
    def proof(acc):
        if change == "manual": state.paused_reason = "manual"
        elif change == "global": bot.paused = True
        elif change == "owner": state.acc["user_id"] = "new-owner"
        elif change == "pending": state.pending_apply = {"vacancy_id": "synthetic"}
        elif change == "record": state.network_recovery["reason"] = "read_timeout"
        return {"verified": True}
    verifier.side_effect = proof
    assert not bot._network_probe_if_due(state, now=due)
    assert state.paused and state.auth_check_started_at is None


def test_reservation_failure_never_calls_hh(network):
    bot, state, verifier = network
    due = hold(network)
    bot._persist_pauses.side_effect = OSError("synthetic-disk-failure")
    assert not bot._network_probe_if_due(state, now=due)
    assert state.paused and state._network_recovery_persistence_failed
    assert activity_view(state)["requires_action"]
    verifier.assert_not_called()


def test_restart_preserves_deadline_budget_and_binding(network):
    bot, state, verifier = network
    due = hold(network)
    bot._network_probe_if_due(state, now=due)
    account = {**state.acc, "paused": True, "paused_reason": "network_error",
               "network_recovery": copy.deepcopy(state.network_recovery)}
    restored = AccountState(account)
    bot.account_states[0] = restored
    assert restored.network_recovery == state.network_recovery
    assert not bot._network_probe_if_due(restored, now=due + timedelta(seconds=1))
    assert verifier.call_count == 1
    next_at = datetime.fromisoformat(restored.network_recovery["next_check_at"])
    bot._network_probe_if_due(restored, now=next_at)
    assert restored.network_recovery["attempts"] == 2 and verifier.call_count == 2


def test_worker_really_calls_network_recovery_inside_pause_loop():
    tree = ast.parse(textwrap.dedent(inspect.getsource(BotManager._run_account_worker_inner)))
    pauses = [node for node in ast.walk(tree) if isinstance(node, ast.While)
              and "state.paused" in ast.unparse(node.test)]
    assert any(any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                   and child.func.attr == "_network_probe_if_due"
                   for child in ast.walk(loop)) for loop in pauses)


@pytest.mark.parametrize("corrupt", ["provenance", "owner", "mode", "attempts"])
def test_corrupt_record_cannot_be_manually_recovered_or_scheduled(network, corrupt):
    bot, state, verifier = network
    due = hold(network)
    if corrupt == "provenance": state.network_recovery["dispatched"] = True
    elif corrupt == "owner": state.network_recovery["account_key"] = "other-owner"
    elif corrupt == "mode": state.acc["mode"] = "web"
    else: state.network_recovery["attempts"] = -1
    expected = copy.deepcopy(state.network_recovery)
    assert not bot.recover_verified_auth(0, expected_state=state,
        expected_account={k: copy.deepcopy(state.acc.get(k)) for k in ("resume_hash", "user_id", "cookies")},
        expected_control=bot._limit_check_guard(state), recovery_reason="network_error",
        expected_network_recovery=expected)
    assert not bot._network_probe_if_due(state, now=due)
    assert state.paused and state.network_recovery["next_check_at"] is None
    assert activity_view(state)["requires_action"]
    verifier.assert_not_called()


def test_network_commit_failure_restores_record_and_remains_paused(network):
    bot, state, verifier = network
    due = hold(network)
    verifier.return_value = {"verified": True}
    count = 0
    def save(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            assert state._auth_recovery_pending and not bot._can_mutate(state)
            raise OSError("synthetic commit failure")
    bot._persist_pauses.side_effect = save
    assert not bot._network_probe_if_due(state, now=due)
    assert state.paused and state.paused_reason == "network_error"
    assert state.network_recovery["attempts"] == 1
    assert not state._auth_recovery_pending
    assert state._network_recovery_persistence_failed
    assert activity_view(state)["requires_action"]


def test_mode_change_during_durable_recovery_is_stale(network):
    bot, state, verifier = network
    due = hold(network)
    verifier.return_value = {"verified": True}
    def save(**kwargs):
        if state._auth_recovery_pending:
            state.acc["mode"] = "web"
    bot._persist_pauses.side_effect = save
    assert not bot._network_probe_if_due(state, now=due)
    assert state.paused and state.network_recovery is not None
