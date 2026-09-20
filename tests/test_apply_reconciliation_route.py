"""The recovery action reads HH and never repeats an application POST."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.routes import apply as routes


@pytest.fixture
def held(monkeypatch):
    pending = {"vacancy_id": "123", "resume_id": "resume-a", "flow": "questionnaire",
               "recorded_at": "2026-09-06T19:53:48+00:00", "reason_code": "submission_unconfirmed"}
    state = SimpleNamespace(
        _state_lock=threading.Lock(), pending_apply=pending, paused=True,
        acc={"resume_hash": "resume-a", "cookies": {}, "user_id": "owner-a"},
    )
    calls = []

    def resolve(*args, **kwargs):
        calls.append((args, kwargs))
        state.pending_apply = None
        state.paused = False
        return True

    bot = SimpleNamespace(_get_apply_state=lambda idx: state if idx == 0 else None,
                          confirm_pending_apply=resolve,
                          record_receipt_failure=lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "bot", bot)
    return state, calls, bot


def test_confirmed_receipt_releases_only_matching_pending(monkeypatch, held):
    state, calls, _ = held
    original = dict(state.pending_apply)

    def receipt(acc, vid, resume_id):
        assert acc is not state.acc
        assert (vid, resume_id) == ("123", "resume-a")
        assert state.paused
        return {"receipt_confirmed": True, "negotiation_id": "topic-a",
                "receipt_created_at": "2026-09-06T22:53:48+03:00"}

    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", receipt)
    response = asyncio.run(routes.api_reconcile_application(0))
    assert response["ok"] and response["confirmed"]
    assert response["paused"] is False
    assert calls[0][0] == (0, "123", "resume-a", "topic-a")
    assert calls[0][1]["expected_state"] is state
    assert calls[0][1]["expected_pending"] == original
    assert calls[0][1]["receipt_created_at"] == "2026-09-06T22:53:48+03:00"


@pytest.mark.parametrize("result", [None, {}, {"receipt_confirmed": False}, {"negotiation_id": "x"}])
def test_unconfirmed_does_not_resume_or_count(monkeypatch, held, result):
    state, calls, _ = held
    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", lambda *args: result)
    response = asyncio.run(routes.api_reconcile_application(0))
    assert response["ok"] is False
    assert state.paused and state.pending_apply
    assert calls == []


def test_receipt_error_stays_paused_without_leaking_credentials(monkeypatch, held):
    state, calls, _ = held

    def fail(*args):
        raise RuntimeError("private-proxy-credential")

    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", fail)
    response = asyncio.run(routes.api_reconcile_application(0))
    assert not response["ok"] and state.paused and not calls
    assert "private-proxy-credential" not in str(response)


def test_deleted_or_changed_account_not_reported_as_resumed(monkeypatch, held):
    state, calls, bot = held
    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", lambda *args: {
        "receipt_confirmed": True, "negotiation_id": "topic-a"})
    bot.confirm_pending_apply = lambda *args, **kwargs: False
    response = asyncio.run(routes.api_reconcile_application(0))
    assert not response["ok"] and response["confirmed"]
    assert state.paused


def test_missing_account_or_pending_never_calls_hh(monkeypatch, held):
    state, calls, _ = held
    def forbidden(*args):
        pytest.fail("No receipt request is allowed without a pending application")
    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", forbidden)
    assert not asyncio.run(routes.api_reconcile_application(8))["ok"]
    state.pending_apply = None
    assert not asyncio.run(routes.api_reconcile_application(0))["ok"]
    assert calls == []


def test_reconcile_pins_recorded_resume_without_switching_active_resume(monkeypatch, held):
    state, calls, _ = held
    state.acc["resume_hash"] = "active-search-resume"

    def receipt(acc, vid, resume_id):
        assert resume_id == "resume-a"
        assert acc["_pinned_resume_id"] == "resume-a"
        assert acc["resume_hash"] == "active-search-resume"
        return {"receipt_confirmed": True, "negotiation_id": "topic-a"}

    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", receipt)
    response = asyncio.run(routes.api_reconcile_application(0))
    assert response["ok"]
    assert state.acc["resume_hash"] == "active-search-resume"
    assert "_pinned_resume_id" not in state.acc
    assert calls[0][1]["expected_account"]["resume_hash"] == "active-search-resume"


def test_receipt_activity_visible_and_duplicate_check_not_started(monkeypatch, held):
    from app.account_activity import activity_view
    state, calls, _ = held
    started, release = threading.Event(), threading.Event()
    reads = []

    def blocked_receipt(*args):
        reads.append(True)
        started.set()
        assert release.wait(5)
        return None

    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", blocked_receipt)

    async def scenario():
        task = asyncio.create_task(routes.api_reconcile_application(0))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            activity = activity_view(state)
            assert activity["phase"] == "receipt_check"
            assert activity["started_at"]
            assert activity["requires_action"] is False
            assert activity["wait_until"] is None
            duplicate = await routes.api_reconcile_application(0)
            assert duplicate["busy"] is True
            assert duplicate["ok"] is False
            assert state.paused
        finally:
            release.set()
            await task

    asyncio.run(scenario())
    assert len(reads) == 1 and calls == []
    assert state.receipt_check_started_at is None
    assert activity_view(state)["phase"] == "outcome_unknown"
    assert activity_view(state)["requires_action"] is True


def test_cancelled_caller_keeps_check_visible_until_actual_read_finishes(monkeypatch, held):
    state, calls, _ = held
    started, release = threading.Event(), threading.Event()

    def blocked_receipt(*args):
        started.set()
        assert release.wait(5)
        return {"receipt_confirmed": True, "negotiation_id": "topic-a"}

    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", blocked_receipt)

    async def scenario():
        task = asyncio.create_task(routes.api_reconcile_application(0))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert state.receipt_check_started_at is not None
            assert state.paused and state.pending_apply
            duplicate = await routes.api_reconcile_application(0)
            assert duplicate["busy"] is True
        finally:
            release.set()
            if state._receipt_check_task is not None:
                await asyncio.wait_for(asyncio.shield(state._receipt_check_task), 2)

    asyncio.run(scenario())
    assert state.receipt_check_started_at is None
    assert state.paused and state.pending_apply
    assert calls == []


def test_busy_guard_covers_local_receipt_commit(monkeypatch, held):
    state, calls, bot = held
    original_resolve = bot.confirm_pending_apply

    def resolve(*args, **kwargs):
        assert state.receipt_check_started_at is not None
        assert state._receipt_check_task is not None
        return original_resolve(*args, **kwargs)

    bot.confirm_pending_apply = resolve
    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", lambda *args: {
        "receipt_confirmed": True, "negotiation_id": "topic-a"})
    response = asyncio.run(routes.api_reconcile_application(0))
    assert response["ok"] and len(calls) == 1
    assert state.receipt_check_started_at is None
    assert state._receipt_check_task is None


def test_connection_failure_diagnostic_is_read_in_get_thread_and_recorded(monkeypatch, held):
    state, calls, bot = held
    thread = []
    recorded = []

    def receipt(*args):
        thread.append(threading.get_ident())
        return None

    def reason():
        assert threading.get_ident() == thread[0]
        return "connect_timeout"

    def record(idx, reason, **kwargs):
        assert state.receipt_check_started_at is not None
        assert kwargs["expected_state"] is state
        assert kwargs["expected_pending"] == state.pending_apply
        recorded.append((idx, reason))
        return True

    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", receipt)
    monkeypatch.setattr("app.apply_confirmation.receipt_check_failure_reason", reason)
    bot.record_receipt_failure = record
    result = asyncio.run(routes.api_reconcile_application(0))
    assert not result["ok"] and state.paused and not calls
    assert recorded == [(0, "connect_timeout")]
    assert "прокси" in result["message"]
    assert state.receipt_check_started_at is None
