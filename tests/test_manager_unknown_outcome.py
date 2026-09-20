"""Mock-only pause/reconciliation regressions. Run pytest from a temporary cwd."""
import ast
import copy
import inspect
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import manager as module
from app.manager import BotManager
from app.state import AccountState


def make_state(**extra):
    return AccountState({"name": "synthetic", "short": "S", "color": "blue",
                         "urls": [], "resume_hash": "resume-a", **extra})


@pytest.fixture
def manager(monkeypatch):
    bot = BotManager.__new__(BotManager)
    bot.account_states = [make_state()]
    bot.temp_states = {}
    bot.temp_sessions = []
    bot._retiring_sessions = []
    bot._activate_lock = threading.Lock()
    bot._stop_event = threading.Event()
    bot.paused = False
    bot._add_log = Mock()
    bot._add_response = Mock()
    bot._add_acc_event = Mock()
    bot._push_action = Mock()
    bot._start_ws_push = Mock()
    bot._build_session_urls = Mock(return_value=[])
    monkeypatch.setattr(module, "save_accounts", Mock())
    monkeypatch.setattr(module, "save_browser_sessions", Mock())
    monkeypatch.setattr(module, "add_test_vacancy", Mock())
    monkeypatch.setattr(module, "add_applied", Mock())
    monkeypatch.setattr(module, "is_applied", lambda *args: False)
    monkeypatch.setattr("app.ws_manager.ws_manager.suspend_account", Mock())
    monkeypatch.setattr("app.ws_manager.ws_manager.resume_account", Mock())
    return bot


def hold(bot, vacancy="vacancy-a", resume="resume-a", **kwargs):
    state = bot.account_states[0]
    bot.hold_pending_apply(state, vacancy, resume, **kwargs)
    return state


def test_pending_roundtrip_is_independent_and_blocks_mutations(manager):
    state = hold(manager, flow="questionnaire", reason_code="questionnaire_unconfirmed")
    assert state.paused_reason == "outcome_unknown"
    assert state.pending_apply["flow"] == "questionnaire"
    assert datetime.fromisoformat(state.pending_apply["recorded_at"]).utcoffset() == timedelta(0)
    assert "Пауза пользователем" not in state.status_detail
    assert state.sent == state.daily_sent == 0
    restored = make_state(**{k: copy.deepcopy(state.acc[k]) for k in (
        "paused", "paused_reason", "pending_apply", "pending_applies")})
    assert restored.paused and restored.pending_apply == state.pending_apply
    state.pending_apply["reason_code"] = "changed-in-memory"
    assert restored.pending_apply["reason_code"] == "questionnaire_unconfirmed"
    assert state.acc["pending_apply"]["reason_code"] == "questionnaire_unconfirmed"
    restored.paused = False  # Even a stale UI flag must not bypass the pending guard.
    assert not manager._can_mutate(restored)


def test_pending_forces_pause_even_if_saved_pause_flag_false():
    state = make_state(paused=False, pending_apply={"vacancy_id": "v", "resume_id": "r"})
    assert state.paused and state.paused_reason == "outcome_unknown"


def test_batch_unknowns_deduplicate_and_require_each_receipt(manager):
    state = hold(manager)
    hold(manager)
    hold(manager, "vacancy-b", "resume-b")
    assert len(state.pending_applies) == 2
    assert manager.confirm_pending_apply(0, "vacancy-a", "resume-a", "topic-a")
    assert state.paused and state.pending_apply["vacancy_id"] == "vacancy-b"
    assert not manager.confirm_pending_apply(0, "vacancy-a", "resume-a", "topic-a")
    assert manager.confirm_pending_apply(0, "vacancy-b", "resume-b", "topic-b")
    assert not state.paused and state.pending_apply is None
    assert state.sent == state.daily_sent == 0


@pytest.mark.parametrize("mismatch", ["vacancy", "resume", "topic", "state", "pending", "deleted"])
def test_confirmation_rejects_stale_or_mismatched_receipt(manager, mismatch):
    state = hold(manager)
    kwargs = {"expected_state": state, "expected_pending": copy.deepcopy(state.pending_apply)}
    vacancy, resume, topic = "vacancy-a", "resume-a", "topic-a"
    if mismatch == "vacancy":
        vacancy = "other"
    elif mismatch == "resume":
        resume = "other"
    elif mismatch == "topic":
        topic = ""
    elif mismatch == "state":
        kwargs["expected_state"] = make_state()
    elif mismatch == "pending":
        kwargs["expected_pending"]["recorded_at"] = "changed"
    else:
        state._deleted = True
    assert not manager.confirm_pending_apply(0, vacancy, resume, topic, **kwargs)
    module.add_applied.assert_not_called()
    assert state.paused and state.pending_apply


def test_current_receipt_counted_once_and_not_a_new_dispatch(manager):
    state = hold(manager, flow="questionnaire")
    receipt_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert manager.confirm_pending_apply(0, "vacancy-a", "resume-a", "topic-a", receipt_created_at=receipt_at)
    assert (state.sent, state.daily_sent, state.questionnaire_sent) == (1, 1, 1)
    module.add_applied.assert_called_once_with(state.name, "vacancy-a", confirmed=True)
    assert not manager.confirm_pending_apply(0, "vacancy-a", "resume-a", "topic-a", receipt_created_at=receipt_at)
    assert state.sent == 1


@pytest.mark.parametrize("field,value", [("resume_hash", "changed"), ("user_id", "changed"), ("cookies", {"session": "changed"})])
def test_confirmation_rejects_account_change_in_same_state(manager, field, value):
    state = hold(manager)
    before = copy.deepcopy({key: state.acc.get(key) for key in ("resume_hash", "user_id", "cookies")})
    state.acc[field] = value
    assert not manager.confirm_pending_apply(0, "vacancy-a", "resume-a", "topic-a",
        expected_state=state, expected_account=before)
    assert state.pending_apply and state.paused


@pytest.mark.parametrize("receipt", ["", "invalid", "2020-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00", "2026-01-01T00:00:00"])
def test_old_missing_or_unusable_receipt_dates_do_not_inflate_counts(manager, receipt):
    state = hold(manager)
    assert manager.confirm_pending_apply(0, "vacancy-a", "resume-a", "topic-a", receipt_created_at=receipt)
    module.add_applied.assert_called_once_with(state.name, "vacancy-a", confirmed=False)
    assert state.sent == state.daily_sent == 0


def test_cached_application_is_not_counted_again(manager, monkeypatch):
    state = hold(manager)
    monkeypatch.setattr(module, "is_applied", lambda *args: True)
    assert manager.confirm_pending_apply(0, "vacancy-a", "resume-a", "topic-a",
        receipt_created_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    assert state.sent == state.daily_sent == 0


@pytest.mark.parametrize("protection", ["hard_stopped", "limit_exceeded", "cookies_expired", "global"])
def test_reconciliation_does_not_override_other_protection(manager, protection):
    state = hold(manager)
    if protection == "global":
        manager.paused = True
    else:
        setattr(state, protection, True)
    assert manager.confirm_pending_apply(0, "vacancy-a", "resume-a", "topic-a")
    assert not manager._can_mutate(state)
    if protection != "global":
        assert state.paused


def test_unknown_and_auto_errors_survive_midnight(manager):
    state = hold(manager)
    state.daily_date = "2020-01-01"
    state.hard_stopped = state.limit_exceeded = True
    assert manager._maybe_roll_daily_counter(state)
    assert state.paused and state.paused_reason == "outcome_unknown"
    assert state.pending_apply
    other = make_state(paused=True, paused_reason="auto_errors")
    other.daily_date = "2020-01-01"
    manager._maybe_roll_daily_counter(other)
    assert other.paused and other.paused_reason == "auto_errors"


@pytest.mark.parametrize("reason", ["outcome_unknown", "message_outcome_unknown", "limit"])
def test_explicit_toggle_cannot_override_protective_reason(manager, reason):
    state = manager.account_states[0]
    state.paused, state.paused_reason = True, reason
    manager.toggle_account_pause(0)
    assert state.paused and state.paused_reason == reason


@pytest.mark.parametrize("reason,expired,expected", [("manual", False, False), ("auto_errors", False, False), ("auth", False, True), ("auth", True, True)])
def test_explicit_toggle_retries_recoverable_pauses(manager, reason, expired, expected):
    state = manager.account_states[0]
    state.paused, state.paused_reason = True, reason
    state.cookies_expired = expired
    state.consecutive_errors = 9
    manager.toggle_account_pause(0)
    assert state.paused is expected
    if not expected:
        assert state.consecutive_errors == 0


@pytest.mark.parametrize("reason,resume_manual,paused", [("manual", True, False), ("manual", False, True), ("outcome_unknown", True, True), ("auto_errors", True, True), ("auth", True, True)])
def test_activation_only_explicitly_resumes_manual_pause(manager, monkeypatch, reason, resume_manual, paused):
    manager.temp_sessions = [{"name": "temp", "resume_hash": "resume-a", "user_id": "owner-a",
                              "paused": True, "paused_reason": reason}]
    monkeypatch.setattr(module.threading, "Thread", lambda **kwargs: SimpleNamespace(
        start=Mock(), is_alive=lambda: False))
    assert manager.activate_session(0, resume_manual=resume_manual)
    state = manager.temp_states[0]
    assert state.paused is paused
    assert state.acc["user_id"] == "owner-a"


def test_stop_and_late_unknown_preserve_retiring_session_protection(manager):
    state = make_state()
    session = {"name": "temp", "resume_hash": "resume-a"}
    manager.temp_states = {0: state}
    manager.temp_sessions = [session]
    assert manager.deactivate_session(0)
    manager.hold_pending_apply(state, "late-vacancy", "resume-a")
    assert session["paused_reason"] == "outcome_unknown"
    assert session["pending_apply"]["vacancy_id"] == "late-vacancy"
    assert session["bot_active"] is False


def run_questionnaire_result(bot, result):
    """Execute the real result-handling branch with all transport removed.

    A one-iteration synthetic loop retains the original break/continue semantics.
    The full worker has unrelated scheduler/profile/search side effects.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(BotManager._run_account_worker_inner)))
    branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
        and ast.unparse(n.test) == "q_result == 'sent'")
    loop = ast.For(target=ast.Name(id="_once", ctx=ast.Store()),
                   iter=ast.List(elts=[ast.Constant(0)], ctx=ast.Load()),
                   body=[copy.deepcopy(branch)], orelse=[])
    code = compile(ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])), "<isolated-questionnaire-handler>", "exec")
    state = bot.account_states[0]
    env = dict(vars(module), self=bot, state=state, acc=state.acc,
               vid="vacancy-a", title="Synthetic role", company="Synthetic company",
               display_title="Synthetic role", info={}, q_info={}, q_result=result,
               attempt_accounts={"vacancy-a": {"resume_hash": "resume-a", "_pinned_resume_id": "resume-pinned"}})
    exec(code, env)
    return state


def test_questionnaire_unknown_uses_pinned_resume_and_separate_reason(manager):
    state = run_questionnaire_result(manager, "unknown")
    assert state.pending_apply["resume_id"] == "resume-pinned"
    assert state.pending_apply["flow"] == "questionnaire"
    assert state.paused_reason == "outcome_unknown"
    assert state.sent == state.daily_sent == 0


def test_questionnaire_pre_submit_error_is_not_unknown(manager, monkeypatch):
    monkeypatch.setattr(module.CONFIG, "auto_pause_errors", 3)
    state = run_questionnaire_result(manager, "error")
    assert not state.paused and state.pending_apply is None
    assert state.consecutive_errors == state.errors == 1
    run_questionnaire_result(manager, "error")
    run_questionnaire_result(manager, "error")
    assert state.paused_reason == "auto_errors" and state.pending_apply is None
    assert state.acc["paused_reason"] == "auto_errors"


def test_questionnaire_old_receipt_is_already_not_sent(manager):
    state = run_questionnaire_result(manager, "already")
    assert state.already_applied == 1
    assert state.sent == state.daily_sent == 0
    assert not state.paused and state.pending_apply is None
    assert module.add_applied.call_args.kwargs == {"confirmed": False}


def test_completed_batch_limit_cannot_hide_later_unknown_results(manager, monkeypatch):
    monkeypatch.setattr(module.CONFIG, "stop_on_hh_limit", True)
    tree = ast.parse(textwrap.dedent(inspect.getsource(BotManager._run_account_worker_inner)))
    nodes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "completed" for t in node.targets):
            nodes.append(node)
        elif isinstance(node, ast.For) and ast.unparse(node.target) in ("(pending_vid, pending_result)", "(j, (vid, result_data))"):
            nodes.append(node)
    assert len(nodes) == 3
    nodes.sort(key=lambda n: n.lineno)
    code = compile(ast.fix_missing_locations(ast.Module(body=copy.deepcopy(nodes), type_ignores=[])), "<isolated-completed-batch>", "exec")
    state = manager.account_states[0]
    batch = ["limit-vacancy", "unknown-a", "unknown-b"]
    env = dict(vars(module), self=manager, state=state, acc=state.acc,
               batch=batch, results=[("limit", {}), ("unknown", {}), ("unknown", {})],
               attempt_accounts={vid: {"resume_hash": "resume-a"} for vid in batch})
    exec(code, env)
    assert state.paused_reason == "outcome_unknown" and state.hard_stopped
    assert [p["vacancy_id"] for p in state.pending_applies] == ["unknown-a", "unknown-b"]
    assert state.sent == state.daily_sent == 0
