"""Verified-auth recovery is local, CAS guarded, and fails closed."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import manager as module
from app.account_activity import activity_view, note_pause
from app.cycle_report import cycle_snapshot
from tests.test_manager_unknown_outcome import manager
from tests.test_cycle_report_pipeline import pipeline, completed_results


def recovery_context(bot):
    state = bot.account_states[0]
    state.paused = True
    state.paused_reason = "auth"
    state.cookies_expired = True
    state.consecutive_errors = 4
    note_pause(state)
    return dict(expected_state=state,
        expected_account={key: copy.deepcopy(state.acc.get(key))
                          for key in ("resume_hash", "user_id", "cookies")},
        expected_control=bot._limit_check_guard(state))


def test_auth_recovery_requires_persistence_before_mutations(manager):
    context = recovery_context(manager)
    state = context["expected_state"]
    def durable_save(**kwargs):
        assert kwargs == {"wait": True}
        assert state._auth_recovery_pending
        assert not manager._can_mutate(state)
    manager._persist_pauses = Mock(side_effect=durable_save)
    assert manager.recover_verified_auth(0, **context)
    assert not state.paused and not state.cookies_expired
    assert not state._auth_recovery_pending and manager._can_mutate(state)
    assert state.consecutive_errors == 0 and state.sent == state.daily_sent == 0


@pytest.mark.parametrize("change", ["manual", "pending", "hard_stop", "limit", "global",
    "deleted", "stop", "local_revision", "global_revision", "account", "cookies", "state"])
def test_auth_recovery_rejects_newer_controls_or_owner(manager, change):
    context = recovery_context(manager)
    state = context["expected_state"]
    if change == "manual": state.paused_reason = "manual"
    elif change == "pending": state.pending_apply = {"vacancy_id": "synthetic"}
    elif change == "hard_stop": state.hard_stopped = True
    elif change == "limit": state.limit_exceeded = True
    elif change == "global": manager.paused = True
    elif change == "deleted": state._deleted = True
    elif change == "stop": manager._stop_event.set()
    elif change == "local_revision": state._activity_control_revision += 1
    elif change == "global_revision": manager._activity_global_pause_revision = 1
    elif change == "account": state.acc["user_id"] = "another-synthetic-owner"
    elif change == "cookies": state.acc["cookies"] = {"synthetic": "changed"}
    elif change == "state": context["expected_state"] = object()
    manager._persist_pauses = Mock()
    assert not manager.recover_verified_auth(0, **context)
    assert state.paused and not state._auth_recovery_pending
    manager._persist_pauses.assert_not_called()


def test_auth_recovery_storage_failure_never_enables_dispatch(manager):
    context = recovery_context(manager)
    state = context["expected_state"]
    def failed_save(**kwargs):
        assert not manager._can_mutate(state)
        raise OSError("synthetic storage failure")
    manager._persist_pauses = Mock(side_effect=failed_save)
    assert not manager.recover_verified_auth(0, **context)
    assert state.paused and state.paused_reason == "auth" and state.cookies_expired
    assert not state._auth_recovery_pending and not manager._can_mutate(state)
    assert state.consecutive_errors == 4


@pytest.mark.parametrize("change", ["manual", "global", "cookies"])
def test_auth_recovery_rechecks_races_during_commit(manager, change):
    context = recovery_context(manager)
    state = context["expected_state"]
    def save(**kwargs):
        if change == "manual":
            state.paused = True
            state.paused_reason = "manual"
            note_pause(state)
        elif change == "global": manager.paused = True
        elif change == "cookies": state.acc["cookies"] = {"changed": "synthetic"}
    manager._persist_pauses = Mock(side_effect=save)
    assert not manager.recover_verified_auth(0, **context)
    assert state.paused and not manager._can_mutate(state)
    assert state.paused_reason == ("manual" if change == "manual" else "auth")


@pytest.mark.parametrize("result,reason", [("rate_limit", "hh_rate_limit"), ("challenge", "challenge")])
def test_access_protection_is_not_auth_or_daily_quota(pipeline, monkeypatch, result, reason):
    bot, state = pipeline
    fill = AsyncMock(return_value=(result, {"dispatched": False}))
    monkeypatch.setattr(module, "get_client", Mock(return_value=SimpleNamespace(fill_questionnaire=fill)))
    report = completed_results(pipeline, [("test", {})])
    assert state.paused and state.paused_reason == reason
    assert not state.cookies_expired and not state.limit_exceeded and not state.hard_stopped
    assert state.pending_apply is None and state.sent == state.daily_sent == 0
    assert report["errors"] == 1 and report["questionnaires_pending"] == 0
    view = activity_view(state)
    assert view["phase"] == reason and view["requires_action"]
    assert view["wait_until"] is None


def test_bridge_network_error_not_auth_or_unknown(pipeline, monkeypatch):
    bot, state = pipeline
    monkeypatch.setattr(module, "get_client", Mock(return_value=SimpleNamespace(
        fill_questionnaire=AsyncMock(return_value=("error", {"phase": "oauth_bridge", "dispatched": False})))))
    report = completed_results(pipeline, [("test", {})])
    assert report["errors"] == 1 and report["unknown"] == 0
    assert not state.cookies_expired and state.pending_apply is None
    assert state.paused_reason not in ("auth", "outcome_unknown")


def test_late_auth_error_does_not_replace_manual_pause(pipeline, monkeypatch):
    bot, state = pipeline
    async def fill(*args, **kwargs):
        state.paused = True
        state.paused_reason = "manual"
        return "auth_error", {}
    monkeypatch.setattr(module, "get_client", Mock(return_value=SimpleNamespace(fill_questionnaire=fill)))
    report = completed_results(pipeline, [("test", {})])
    assert state.paused and state.paused_reason == "manual"
    assert state.cookies_expired and report["errors"] == 1


@pytest.mark.parametrize("reason", ["auth", "hh_rate_limit", "challenge"])
def test_plain_toggle_cannot_clear_access_protection(manager, reason):
    state = manager.account_states[0]
    state.paused = True
    state.paused_reason = reason
    state.cookies_expired = False
    manager.toggle_account_pause(0)
    assert state.paused and state.paused_reason == reason


def test_access_protection_preserves_manual_pause_during_request(manager):
    state = manager.account_states[0]
    state.paused = True
    state.paused_reason = "manual"
    manager._hold_questionnaire_access(state, "rate_limit")
    assert state.paused_reason == "manual"
    assert state.errors == 1 and not state.limit_exceeded


def test_auth_activity_check_does_not_override_stop_or_unknown(manager):
    state = recovery_context(manager)["expected_state"]
    state.auth_check_started_at = datetime.now(timezone.utc).isoformat()
    view = activity_view(state)
    assert view["phase"] == "auth_check" and not view["requires_action"]
    assert view["wait_until"] is None
    assert activity_view(state, stopped=True)["phase"] == "stopped"
    state.pending_apply = {"vacancy_id": "synthetic"}
    assert activity_view(state)["phase"] == "outcome_unknown"
