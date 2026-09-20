"""No live requests: durable bounded automatic READ-only receipt checks."""
import ast
import copy
import inspect
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app import manager as module
from app.account_activity import activity_view
from app.manager import BotManager
from app.state import AccountState

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def make_state(**extra):
    return AccountState({"name": "synthetic", "short": "S", "color": "blue", "urls": [],
                         "resume_hash": "resume-a", "user_id": "owner-a", "cookies": {}, **extra})


@pytest.fixture
def setup(monkeypatch):
    bot = BotManager.__new__(BotManager)
    state = make_state()
    bot.account_states = [state]
    bot.temp_states = {}
    bot.temp_sessions = []
    bot._retiring_sessions = []
    bot._stop_event = threading.Event()
    bot.paused = False
    bot._add_log = Mock()
    monkeypatch.setattr(module, "save_accounts", Mock())
    monkeypatch.setattr(module, "save_browser_sessions", Mock())
    monkeypatch.setattr(module, "add_applied", Mock())
    monkeypatch.setattr(module, "is_applied", lambda *args: False)
    receipt = Mock(return_value=None)
    monkeypatch.setattr("app.apply_confirmation.confirm_application_receipt", receipt)
    monkeypatch.setattr("app.apply_confirmation.receipt_check_failure_reason", lambda: "unconfirmed", raising=False)
    bot.hold_pending_apply(state, "vacancy-a", "resume-pinned", flow="questionnaire")
    state.pending_apply["recorded_at"] = NOW.isoformat()
    state.pending_apply["reconcile_next_at"] = (NOW + timedelta(seconds=30)).isoformat()
    module.save_accounts.reset_mock()
    module.save_browser_sessions.reset_mock()
    return bot, state, receipt


def test_three_checks_follow_30_90_300_schedule_and_then_require_manual_action(setup):
    bot, state, receipt = setup
    assert not bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=29))
    assert receipt.call_count == 0
    for at, count, next_at in [(30, 1, 120), (120, 2, 420), (420, 3, None)]:
        assert not bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=at))
        assert receipt.call_count == count
        assert state.pending_apply["reconcile_attempts"] == count
        assert state.receipt_check_started_at is None
        if next_at is not None:
            assert datetime.fromisoformat(state.pending_apply["reconcile_next_at"]) == NOW + timedelta(seconds=next_at)
            assert activity_view(state)["requires_action"] is False
            assert not bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=next_at - 1))
            assert receipt.call_count == count
    assert state.pending_apply["reconcile_next_at"] is None
    assert activity_view(state)["requires_action"] is True
    assert activity_view(state)["wait_until"] is None
    assert not bot._reconcile_pending_if_due(state, now=NOW + timedelta(days=30))
    assert receipt.call_count == 3
    assert state.paused and state.paused_reason == "outcome_unknown"
    module.add_applied.assert_not_called()


def test_reservation_is_persisted_before_get_and_uses_exact_pinned_resume(setup):
    bot, state, receipt = setup
    def verify(acc, vid, resume):
        module.save_accounts.assert_called_with(wait=True)
        module.save_browser_sessions.assert_called_with(bot.temp_sessions, wait=True)
        assert state.acc["pending_apply"]["reconcile_attempts"] == 1
        assert state.acc["pending_apply"]["reconcile_next_at"] is not None
        assert state.receipt_check_started_at
        assert (vid, resume, acc["_pinned_resume_id"]) == ("vacancy-a", "resume-pinned", "resume-pinned")
        assert acc["resume_hash"] == "resume-a"
        assert activity_view(state)["phase"] == "receipt_check"
        return None
    receipt.side_effect = verify
    bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))


def test_success_releases_only_exact_pending_without_new_application_call(setup):
    bot, state, receipt = setup
    receipt.return_value = {"receipt_confirmed": True, "negotiation_id": "topic-a"}
    assert bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))
    assert state.pending_apply is None and not state.paused
    assert state.receipt_check_started_at is None
    assert state.sent == state.daily_sent == 0  # Receipt had no creation date.
    module.add_applied.assert_called_once_with(state.name, "vacancy-a", confirmed=False)


def test_duplicate_hold_does_not_reset_attempt_budget_or_schedule(setup):
    bot, state, receipt = setup
    bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))
    before = copy.deepcopy(state.pending_apply)
    bot.hold_pending_apply(state, "vacancy-a", "resume-pinned")
    assert state.pending_apply == before
    assert len(state.pending_applies) == 1


def test_crash_after_reservation_survives_restart_without_immediate_retry(setup):
    bot, state, receipt = setup
    class SimulatedCrash(BaseException):
        pass
    receipt.side_effect = SimulatedCrash()
    with pytest.raises(SimulatedCrash):
        bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))
    persisted = {key: copy.deepcopy(state.acc[key]) for key in ("paused", "paused_reason", "pending_apply", "pending_applies")}
    restored = make_state(**persisted)
    bot.account_states = [restored]
    receipt.side_effect = None
    receipt.reset_mock()
    assert restored.pending_apply["reconcile_attempts"] == 1
    assert not bot._reconcile_pending_if_due(restored, now=NOW + timedelta(seconds=31))
    receipt.assert_not_called()
    bot._reconcile_pending_if_due(restored, now=NOW + timedelta(seconds=120))
    assert restored.pending_apply["reconcile_attempts"] == 2
    assert receipt.call_count == 1


def test_legacy_pending_is_scheduled_once_not_retried_immediately(setup):
    bot, state, receipt = setup
    for key in ("reconcile_attempts", "reconcile_next_at", "reconcile_last_started_at"):
        state.pending_apply.pop(key)
    bot._reconcile_pending_if_due(state, now=NOW)
    receipt.assert_not_called()
    assert state.pending_apply["reconcile_attempts"] == 0
    assert datetime.fromisoformat(state.pending_apply["reconcile_next_at"]) == NOW + timedelta(seconds=30)
    module.save_accounts.assert_called_once_with(wait=True)
    bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=1))
    module.save_accounts.assert_called_once_with(wait=True)


@pytest.mark.parametrize("block", ["global", "manual", "stop", "deleted", "busy"])
def test_controls_and_existing_manual_check_prevent_background_reads(setup, block):
    bot, state, receipt = setup
    if block == "global":
        bot.paused = True
    elif block == "manual":
        state.paused_reason = "manual"
    elif block == "stop":
        bot._stop_event.set()
    elif block == "deleted":
        state._deleted = True
    else:
        state.receipt_check_started_at = "manual-operation"
    before = copy.deepcopy(state.pending_apply)
    assert not bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))
    receipt.assert_not_called()
    assert state.pending_apply == before
    if block == "global":
        assert activity_view(state, global_paused=True)["requires_action"] is True
        assert activity_view(state, global_paused=True)["wait_until"] is None


@pytest.mark.parametrize("change", ["manual", "global", "stop", "deleted", "resume", "owner", "cookies", "replace_state", "global_aba"])
def test_late_receipt_cannot_override_changed_account_or_controls(setup, change):
    bot, state, receipt = setup
    def verify(*args):
        if change == "manual":
            state.paused_reason = "manual"
        elif change == "global":
            bot.paused = True
        elif change == "stop":
            bot._stop_event.set()
        elif change == "deleted":
            state._deleted = True
        elif change == "resume":
            state.acc["resume_hash"] = "different"
        elif change == "owner":
            state.acc["user_id"] = "different"
        elif change == "cookies":
            state.acc["cookies"] = {"synthetic": "different"}
        elif change == "replace_state":
            bot.account_states = [make_state()]
        else:
            bot._activity_global_pause_revision = 2
        return {"receipt_confirmed": True, "negotiation_id": "topic-a"}
    receipt.side_effect = verify
    assert not bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))
    assert state.pending_apply and state.paused
    assert state.receipt_check_started_at is None
    module.add_applied.assert_not_called()


def test_failed_reservation_write_never_starts_get_and_disables_automatic_loop(setup, monkeypatch):
    bot, state, receipt = setup
    monkeypatch.setattr(bot, "_persist_pauses", Mock(side_effect=OSError("synthetic")))
    assert not bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))
    receipt.assert_not_called()
    assert state._reconcile_persistence_failed and state.receipt_check_started_at is None
    assert activity_view(state)["requires_action"]
    assert activity_view(state)["wait_until"] is None
    assert not bot._reconcile_pending_if_due(state, now=NOW + timedelta(days=1))
    receipt.assert_not_called()


def test_temporary_account_uses_public_index_and_preserves_other_limits(setup):
    bot, state, receipt = setup
    bot.account_states = [make_state(name="other")]
    bot.temp_states = {0: state}
    bot.temp_sessions = [{}]
    state.hard_stopped = state.limit_exceeded = True
    receipt.return_value = {"receipt_confirmed": True, "negotiation_id": "topic-a"}
    assert bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))
    assert state.pending_apply is None
    assert state.paused and state.hard_stopped and state.limit_exceeded
    assert bot.temp_sessions[0]["pending_apply"] is None


def test_manual_retry_still_works_after_background_budget_exhausted(setup):
    bot, state, receipt = setup
    state.pending_apply.update(reconcile_attempts=3, reconcile_next_at=None)
    assert bot.confirm_pending_apply(0, "vacancy-a", "resume-pinned", "verified-manual-topic")
    assert not state.paused and state.pending_apply is None


def test_real_paused_worker_loop_calls_recovery_and_leaves_pause_after_proof(setup, monkeypatch):
    bot, state, receipt = setup
    tree = ast.parse(textwrap.dedent(inspect.getsource(BotManager._run_account_worker_inner)))
    loop = next(node for node in ast.walk(tree) if isinstance(node, ast.While)
        and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and child.func.attr == "_reconcile_pending_if_due" for child in ast.walk(node))
        and "self.paused or state.paused" in ast.unparse(node.test))
    def recover(s):
        s.paused = False
        return True
    recovery = Mock(side_effect=recover)
    monkeypatch.setattr(bot, "_reconcile_pending_if_due", recovery)
    sleep = Mock(side_effect=AssertionError("must leave recovered pause before sleeping"))
    monkeypatch.setattr(module.time, "sleep", sleep)
    code = compile(ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(loop)], type_ignores=[])), "<actual-paused-worker-loop>", "exec")
    exec(code, dict(vars(module), self=bot, state=state))
    recovery.assert_called_once_with(state)
    sleep.assert_not_called()


def test_persist_snapshot_and_account_update_are_atomic_against_new_reservation(setup, monkeypatch):
    bot, state, _receipt = setup
    captured, release, reserving = threading.Event(), threading.Event(), threading.Event()
    writes, errors = [], []
    class DelayedDict(dict):
        def update(self, *args, **kwargs):
            if threading.current_thread().name == "old-snapshot":
                captured.set()
                if not release.wait(3):
                    raise AssertionError("test snapshot was not released")
            return super().update(*args, **kwargs)
    state.acc = DelayedDict(state.acc)
    monkeypatch.setattr(module, "save_accounts", lambda **kwargs: writes.append(
        copy.deepcopy(state.acc["pending_apply"]["reconcile_attempts"])))
    def old_save():
        try:
            bot._persist_pauses()
        except BaseException as exc:
            errors.append(exc)
    def reserve():
        try:
            reserving.set()
            with state._state_lock:
                state.pending_apply["reconcile_attempts"] = 1
            bot._persist_pauses(wait=True)
        except BaseException as exc:
            errors.append(exc)
    older = threading.Thread(target=old_save, name="old-snapshot", daemon=True)
    newer = threading.Thread(target=reserve, name="new-reservation", daemon=True)
    older.start()
    try:
        assert captured.wait(2)
        # Before the fix snapshot had already captured zero, but did not hold
        # the state lock: a later reservation could be overwritten by update.
        assert state._state_lock.locked()
        newer.start()
        assert reserving.wait(2)
    finally:
        release.set()
        older.join(3)
        if newer.ident is not None:
            newer.join(3)
    assert not older.is_alive() and not newer.is_alive()
    assert not errors
    assert writes == [0, 1]
    assert state.pending_apply["reconcile_attempts"] == state.acc["pending_apply"]["reconcile_attempts"] == 1


def test_persist_serializes_whole_saves_but_releases_state_lock_before_io(setup, monkeypatch):
    bot, state, _receipt = setup
    io_entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    writes, errors = [], []
    def save_accounts(**kwargs):
        name = threading.current_thread().name
        writes.append((name, "accounts"))
        if name == "first-persist":
            io_entered.set()
            if not release.wait(3):
                raise AssertionError("test I/O was not released")
    monkeypatch.setattr(module, "save_accounts", save_accounts)
    monkeypatch.setattr(module, "save_browser_sessions", lambda *args, **kwargs:
        writes.append((threading.current_thread().name, "sessions")))
    def persist(second=False):
        try:
            if second:
                second_entered.set()
            bot._persist_pauses(wait=True)
        except BaseException as exc:
            errors.append(exc)
    first = threading.Thread(target=persist, name="first-persist", daemon=True)
    second = threading.Thread(target=lambda: persist(True), name="second-persist", daemon=True)
    first.start()
    try:
        assert io_entered.wait(2)
        # Stop/UI controls remain available during the disk barrier.
        assert state._state_lock.acquire(blocking=False)
        state._state_lock.release()
        assert bot._pause_persist_lock.locked()
        second.start()
        assert second_entered.wait(2)
    finally:
        release.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not errors
    assert not first.is_alive() and not second.is_alive()
    assert writes == [("first-persist", "accounts"), ("first-persist", "sessions"),
                      ("second-persist", "accounts"), ("second-persist", "sessions")]


@pytest.mark.parametrize("code,fragment", [("connect_timeout", "Нет соединения с HH через настроенный прокси"),
                                          ("read_timeout", "HH не ответил вовремя")])
@pytest.mark.parametrize("previous_attempts", [0, 2])
def test_network_failure_is_persisted_and_visible_without_budget_changes(setup, monkeypatch, code, fragment, previous_attempts):
    bot, state, receipt = setup
    state.pending_apply["reconcile_attempts"] = previous_attempts
    caller = threading.get_ident()
    def reason():
        assert threading.get_ident() == caller
        assert receipt.call_count == 1
        return code
    monkeypatch.setattr("app.apply_confirmation.receipt_check_failure_reason", reason)
    bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))
    assert state.pending_apply["reconcile_last_error"] == state.acc["pending_apply"]["reconcile_last_error"] == code
    assert state.pending_apply["reconcile_attempts"] == previous_attempts + 1
    assert fragment in activity_view(state)["current"]
    assert activity_view(state)["requires_action"] is (previous_attempts == 2)
    assert bool(state.pending_apply["reconcile_next_at"]) is (previous_attempts == 0)
    assert state.paused_reason == "outcome_unknown"


@pytest.mark.parametrize("code", ["auth", "rate_limit"])
def test_access_restriction_stops_further_auto_reads_without_faking_exhaustion(setup, monkeypatch, code):
    bot, state, receipt = setup
    monkeypatch.setattr("app.apply_confirmation.receipt_check_failure_reason", lambda: code)
    bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))
    assert state.pending_apply["reconcile_attempts"] == 1
    assert state.pending_apply["reconcile_next_at"] is None
    assert state.pending_apply["reconcile_last_error"] == code
    assert activity_view(state)["requires_action"] and activity_view(state)["wait_until"] is None
    assert state.paused and state.paused_reason == "outcome_unknown"
    bot._reconcile_pending_if_due(state, now=NOW + timedelta(days=1))
    assert receipt.call_count == 1


@pytest.mark.parametrize("code", [None, {"secret": "opaque"}, "https://secret.example/token", 123])
def test_unexpected_diagnostic_is_reduced_to_unconfirmed(setup, monkeypatch, code):
    bot, state, _receipt = setup
    monkeypatch.setattr("app.apply_confirmation.receipt_check_failure_reason", lambda: code)
    bot._reconcile_pending_if_due(state, now=NOW + timedelta(seconds=30))
    assert state.pending_apply["reconcile_last_error"] == "unconfirmed"
    assert "secret" not in str(state.pending_apply)
    assert state.pending_apply["reconcile_next_at"] is not None


def failure_expectations(state):
    return {"expected_state": state, "expected_pending": copy.deepcopy(state.pending_apply),
            "expected_account": {key: copy.deepcopy(state.acc.get(key))
                                 for key in ("resume_hash", "user_id", "cookies")}}


@pytest.mark.parametrize("code", ["connect_timeout", "auth", "rate_limit", "untrusted-secret"])
def test_manual_failure_metadata_preserves_attempt_budget(setup, code):
    bot, state, _receipt = setup
    state.pending_apply.update(reconcile_attempts=3, reconcile_next_at=None)
    assert bot.record_receipt_failure(0, code, **failure_expectations(state))
    assert state.pending_apply["reconcile_attempts"] == 3
    assert state.pending_apply["reconcile_next_at"] is None
    assert state.pending_apply["reconcile_last_error"] == ("unconfirmed" if code == "untrusted-secret" else code)
    module.save_accounts.assert_called_with(wait=True)


@pytest.mark.parametrize("change", ["pending", "owner", "state", "deleted"])
def test_manual_failure_record_rejects_stale_targets(setup, change):
    bot, state, _receipt = setup
    expected = failure_expectations(state)
    if change == "pending":
        state.pending_apply["recorded_at"] = "changed"
    elif change == "owner":
        state.acc["user_id"] = "different"
    elif change == "deleted":
        state._deleted = True
    else:
        bot.account_states = [make_state()]
    assert not bot.record_receipt_failure(0, "connect_timeout", **expected)
    assert "reconcile_last_error" not in state.pending_apply
    module.save_accounts.assert_not_called()


def test_manual_failure_persistence_failure_is_fail_closed(setup, monkeypatch):
    bot, state, _receipt = setup
    monkeypatch.setattr(bot, "_persist_pauses", Mock(side_effect=OSError("synthetic")))
    assert not bot.record_receipt_failure(0, "connect_timeout", **failure_expectations(state))
    assert state._reconcile_persistence_failed and state.paused
    assert activity_view(state)["requires_action"]


@pytest.mark.parametrize("code", ["auth", "rate_limit"])
def test_legacy_restriction_is_not_given_a_new_auto_schedule(setup, code):
    bot, state, receipt = setup
    for key in ("reconcile_attempts", "reconcile_next_at", "reconcile_last_started_at"):
        state.pending_apply.pop(key)
    state.pending_apply["reconcile_last_error"] = code
    assert not bot._reconcile_pending_if_due(state, now=NOW)
    assert "reconcile_attempts" not in state.pending_apply
    receipt.assert_not_called()
