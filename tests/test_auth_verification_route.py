"""Mock-only route tests: exact owner/control guards, cancellation, no secrets."""
import asyncio
import threading
from unittest.mock import Mock

import pytest

from app.routes import apply as routes
from app.account_activity import note_pause
from tests.test_manager_unknown_outcome import manager


@pytest.fixture
def auth_route(manager, monkeypatch):
    state = manager.account_states[0]
    state.acc.update(mode="oauth", user_id="synthetic-owner", cookies={"synthetic": "cookie"})
    state.paused = True
    state.paused_reason = "auth"
    state.cookies_expired = True
    note_pause(state)
    manager._persist_pauses = Mock()
    monkeypatch.setattr(routes, "bot", manager)
    verifier = Mock(return_value={"verified": True})
    monkeypatch.setattr("app.auth_verification.verify_oauth_and_web_access", verifier)
    return manager, state, verifier


def run():
    return asyncio.run(routes.api_recheck_auth(0))


def test_success_is_exact_local_recovery_after_safe_proof(auth_route):
    bot, state, verifier = auth_route
    old_cookies = state.acc["cookies"].copy()
    response = run()
    assert response["ok"] and response["verified"] and not response["paused"]
    assert not state.paused and not state.cookies_expired
    assert state.sent == state.daily_sent == 0 and state.pending_apply is None
    acc = verifier.call_args.args[0]
    assert acc is not state.acc and acc["cookies"] is not state.acc["cookies"]
    assert acc["user_id"] == "synthetic-owner" and acc["cookies"] == old_cookies
    assert state.auth_check_started_at is None and state._auth_check_task is None
    bot._persist_pauses.assert_called_once_with(wait=True)
    assert "synthetic-owner" not in str(response) and "cookie" not in str(response)


@pytest.mark.parametrize("change", ["missing", "manual", "global", "pending", "queue", "limit",
    "hard_stop", "stopped", "deleted", "web_mode", "unpaused", "rate_limit", "challenge"])
def test_ineligible_requests_do_not_call_hh_or_resume(auth_route, change):
    bot, state, verifier = auth_route
    if change == "missing": bot.account_states = []
    elif change == "manual": state.paused_reason = "manual"
    elif change == "global": bot.paused = True
    elif change == "pending": state.pending_apply = {"vacancy_id": "synthetic"}
    elif change == "queue": state.pending_applies = [{"vacancy_id": "synthetic"}]
    elif change == "limit": state.limit_exceeded = True
    elif change == "hard_stop": state.hard_stopped = True
    elif change == "stopped": bot._stop_event.set()
    elif change == "deleted": state._deleted = True
    elif change == "web_mode": state.acc["mode"] = "web"
    elif change == "unpaused": state.paused = False
    else: state.paused_reason = "hh_rate_limit" if change == "rate_limit" else "challenge"
    response = run()
    assert not response["ok"]
    verifier.assert_not_called()
    bot._persist_pauses.assert_not_called()


@pytest.mark.parametrize("busy_field", ["auth_check_started_at", "receipt_check_started_at", "_auth_recovery_pending"])
def test_shared_busy_guard_skips_verification(auth_route, busy_field):
    bot, state, verifier = auth_route
    setattr(state, busy_field, True)
    response = run()
    assert response["busy"] and not response["ok"] and state.paused
    verifier.assert_not_called()


@pytest.mark.parametrize("proof", [None, [], "private-test-key", {}, {"verified": 1},
    {"verified": "true"}, {"verified": False, "reason": []},
    {"verified": False, "reason": {"secret": "private-test-key"}},
    {"verified": False, "reason": "private-test-key", "message": "private-test-key"}])
def test_malformed_proof_never_resumes_or_echoes_private_details(auth_route, proof):
    bot, state, verifier = auth_route
    verifier.return_value = proof
    response = run()
    assert not response["ok"] and state.paused
    assert "private-test-key" not in str(response)
    bot._persist_pauses.assert_not_called()
    assert state.auth_check_started_at is None


def test_verifier_exception_has_safe_failure_and_clears_marker(auth_route):
    bot, state, verifier = auth_route
    verifier.side_effect = RuntimeError("private-test-key")
    response = run()
    assert not response["ok"] and state.paused
    assert "private-test-key" not in str(response)
    assert state.auth_check_started_at is None


def test_unexpected_local_commit_error_is_sanitized(auth_route):
    bot, state, verifier = auth_route
    bot.recover_verified_auth = Mock(side_effect=RuntimeError("private-test-key"))
    response = run()
    assert not response["ok"] and state.paused
    assert "private-test-key" not in str(response)
    assert state.auth_check_started_at is None


@pytest.mark.parametrize("change", ["manual", "global", "pending", "account", "cookies", "state", "revision"])
def test_stale_successful_proof_cannot_clear_new_controls(auth_route, change):
    bot, state, verifier = auth_route
    def verify(acc):
        with state._state_lock:
            if change == "manual": state.paused_reason = "manual"
            elif change == "global": bot.paused = True
            elif change == "pending": state.pending_apply = {"vacancy_id": "synthetic"}
            elif change == "account": state.acc["user_id"] = "different-owner"
            elif change == "cookies": state.acc["cookies"] = {"changed": "synthetic"}
            elif change == "state": bot.account_states = []
            elif change == "revision": state._activity_control_revision += 1
        return {"verified": True}
    verifier.side_effect = verify
    response = run()
    assert not response["ok"] and state.paused
    bot._persist_pauses.assert_not_called()
    assert state.auth_check_started_at is None


def test_cancellation_keeps_busy_until_real_read_finishes_and_discards_proof(auth_route):
    bot, state, verifier = auth_route
    entered, release = threading.Event(), threading.Event()
    def verify(acc):
        entered.set()
        assert release.wait(5)
        return {"verified": True}
    verifier.side_effect = verify
    async def scenario():
        task = asyncio.create_task(routes.api_recheck_auth(0))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            duplicate = await routes.api_recheck_auth(0)
            assert duplicate["busy"] and verifier.call_count == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert state.auth_check_started_at and state.paused
            assert (await routes.api_recheck_auth(0))["busy"]
        finally:
            release.set()
            actual_task = state._auth_check_task
            if actual_task is not None:
                await asyncio.wait_for(asyncio.shield(actual_task), 2)
    asyncio.run(scenario())
    assert state.paused and state.auth_check_started_at is None
    assert state._auth_check_task is None and verifier.call_count == 1
    bot._persist_pauses.assert_not_called()


def test_busy_marker_survives_through_durable_commit(auth_route):
    bot, state, verifier = auth_route
    responses = []
    def save(**kwargs):
        assert kwargs == {"wait": True}
        assert state.auth_check_started_at and state._auth_check_task is not None
        assert state._auth_recovery_pending and not bot._can_mutate(state)
        def second_caller():
            responses.append(asyncio.run(routes.api_recheck_auth(0)))
        thread = threading.Thread(target=second_caller)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
    bot._persist_pauses.side_effect = save
    assert run()["ok"]
    assert len(responses) == 1 and responses[0]["busy"]
    assert verifier.call_count == 1 and state.auth_check_started_at is None


def test_persistence_failure_reports_failure_and_remains_paused(auth_route):
    bot, state, verifier = auth_route
    bot._persist_pauses.side_effect = OSError("private-test-key")
    response = run()
    assert not response["ok"] and state.paused and state.paused_reason == "auth"
    assert "private-test-key" not in str(response)
    assert state.auth_check_started_at is None and not bot._can_mutate(state)
