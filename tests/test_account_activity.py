"""Local-only activity display and control-state race regressions."""
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
from app.account_activity import activity_view, set_activity, note_pause
from app.manager import BotManager
from app.state import AccountState

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def state(**fields):
    return AccountState({"name": "synthetic", "short": "S", "color": "blue", "urls": [], **fields})


def manager(s):
    bot = BotManager.__new__(BotManager)
    bot.paused = False
    bot._stop_event = threading.Event()
    bot.account_states = [s]
    bot.temp_states = {}
    bot.temp_sessions = []
    bot._add_log = Mock()
    return bot


def test_progress_does_not_restart_stage_clock_and_snapshot_is_pure():
    s = state()
    set_activity(s, "collect", "Загрузка поиска", "Проверка результатов", progress=(0, 5), now=NOW)
    started = s.activity["started_at"]
    set_activity(s, "collect", "Загрузка поиска", "Проверка результатов", progress=(3, 5), now=NOW + timedelta(seconds=90))
    assert s.activity["started_at"] == started
    a = activity_view(s)
    assert a == activity_view(s)
    a["progress"]["done"] = 999
    assert s.activity["progress"]["done"] == 3
    set_activity(s, "filter", "Проверка условий", "Подготовка очереди", now=NOW + timedelta(seconds=100))
    assert s.activity["started_at"] != started


def test_actual_wait_deadline_is_aware_and_unknown_network_duration_has_none():
    s = state()
    deadline = NOW + timedelta(seconds=45)
    set_activity(s, "cycle_wait", "Пауза цикла", "Повторный поиск", wait_until=deadline, now=NOW)
    assert datetime.fromisoformat(activity_view(s)["wait_until"]) == deadline
    set_activity(s, "apply", "Ожидает ответа HH", "Проверит результат", now=NOW)
    assert activity_view(s)["wait_until"] is None


def test_new_operation_resets_clock_without_exporting_vacancy_identity():
    s = state()
    set_activity(s, "apply", "Ожидает HH", "Проверит результат", now=NOW, operation="private-first")
    first = activity_view(s)["started_at"]
    set_activity(s, "apply", "Ожидает HH", "Проверит результат", now=NOW + timedelta(seconds=10), operation="private-second")
    assert activity_view(s)["started_at"] != first
    assert "private-" not in str(activity_view(s))


def test_restart_preserves_known_pending_time_but_does_not_invent_manual_pause_time():
    s = state(paused=True, pending_apply={"vacancy_id": "synthetic", "recorded_at": NOW.isoformat()})
    note_pause(s)
    assert datetime.fromisoformat(activity_view(s)["started_at"]) == NOW
    manual = state(paused=True, paused_reason="manual")
    note_pause(manual)
    assert activity_view(manual)["started_at"] is None


@pytest.mark.parametrize("reason,phase", [("manual", "manual_pause"), ("auth", "auth"), ("auto_errors", "auto_errors"),
    ("outcome_unknown", "outcome_unknown"), ("message_outcome_unknown", "message_outcome_unknown")])
def test_protective_state_overrides_stale_work_and_timer(reason, phase):
    s = state(paused=True, paused_reason=reason)
    note_pause(s)
    set_activity(s, "questionnaire", "Обработка анкеты", "Следующая вакансия", wait_until=NOW)
    result = activity_view(s)
    assert result["phase"] == phase
    assert result["requires_action"] is True
    assert result["wait_until"] is None
    assert result["next"] != "Следующая вакансия"
    assert result["started_at"] == activity_view(s)["started_at"]


def test_late_collect_progress_cannot_hide_new_pause():
    s = state()
    set_activity(s, "collect", "Загрузка", "Проверка", progress=(0, 2), now=NOW)
    s.paused, s.paused_reason = True, "manual"
    note_pause(s)
    set_activity(s, "collect", "Загрузка", "Проверка", progress=(2, 2), now=NOW + timedelta(seconds=20))
    assert activity_view(s)["phase"] == "manual_pause"
    assert activity_view(s, stopped=True)["phase"] == "stopped"


def test_unknown_requires_explicit_check_and_active_receipt_is_distinct():
    s = state(paused=True, pending_apply={"vacancy_id": "test", "resume_id": "r"})
    assert activity_view(s)["requires_action"] is True
    assert "Нажмите" in activity_view(s)["next"]
    s.receipt_check_started_at = NOW.isoformat()
    active = activity_view(s)
    assert active["phase"] == "receipt_check" and not active["requires_action"]
    assert active["wait_until"] is None
    assert "заблокированы" in active["current"]
    assert activity_view(s, stopped=True)["phase"] == "stopped"
    assert activity_view(s, stopped=True)["requires_action"] is True


@pytest.mark.parametrize("protection", ["manual", "global", "hard", "auto_errors"])
def test_limit_timer_is_hidden_when_check_cannot_run(protection):
    s = state(limit_exceeded=True)
    s.limit_reset_time = NOW + timedelta(seconds=60)
    if protection == "hard":
        s.hard_stopped = True
    elif protection != "global":
        s.paused, s.paused_reason = True, protection
    view = activity_view(s, global_paused=protection == "global")
    assert view["phase"] == "limit" and view["wait_until"] is None
    assert view["requires_action"] is (protection != "hard")


def test_limit_check_is_not_displayed_as_waiting_for_reset():
    s = state(limit_exceeded=True)
    set_activity(s, "limit_check", "Проверяет HH", "Обработает ответ", now=NOW)
    assert activity_view(s)["phase"] == "limit_check"
    set_activity(s, "limit_wait", "Ждёт проверки", "Проверит HH", now=NOW)
    s.limit_reset_time = NOW + timedelta(seconds=60)
    assert activity_view(s)["phase"] == "limit"
    assert activity_view(s)["wait_until"] is not None


def test_no_raw_errors_or_previous_vacancy_in_new_activity():
    s = state()
    s.status_detail = "secret raw exception with cookies"
    s.current_vacancy_id, s.current_vacancy_title, s.current_vacancy_company = "old", "Old success", "Private employer"
    set_activity(s, "collect", "Загружает поиск", "Проверит вакансии", now=NOW)
    assert s.current_vacancy_id == s.current_vacancy_title == s.current_vacancy_company == ""
    result = str(activity_view(s))
    assert "secret" not in result and "Private employer" not in result


def test_single_current_vacancy_is_set_before_call_and_batch_has_none():
    s = state()
    s.vacancy_meta = {"v": {"title": "Synthetic role", "company": "Synthetic employer"}}
    BotManager._activity_vacancy(s, "v")
    assert (s.current_vacancy_id, s.current_vacancy_title) == ("v", "Synthetic role")
    BotManager._activity_vacancy(s)
    assert s.current_vacancy_id == s.current_vacancy_title == ""


@pytest.mark.parametrize("change", ["manual", "unknown", "delete", "stop", "global", "aba"])
def test_completed_limit_read_cannot_override_new_controls(change):
    s = state(limit_exceeded=True)
    bot = manager(s)
    before = bot._limit_check_guard(s)
    if change == "manual":
        s.paused, s.paused_reason = True, "manual"
    elif change == "unknown":
        s.pending_apply = {"vacancy_id": "v"}
    elif change == "delete":
        s._deleted = True
    elif change == "stop":
        bot._stop_event.set()
    elif change == "global":
        bot.paused = True
    else:
        s.paused, s.paused_reason = True, "manual"
        note_pause(s)
        s.paused, s.paused_reason = False, ""
        note_pause(s)
    assert not bot._clear_checked_limit(s, before)
    assert s.limit_exceeded


def test_unchanged_successful_limit_read_keeps_existing_clear_behavior():
    s = state(limit_exceeded=True)
    bot = manager(s)
    assert bot._clear_checked_limit(s, bot._limit_check_guard(s))
    assert not s.paused and not s.limit_exceeded


def test_real_limit_result_branch_preserves_pause_requested_during_http(monkeypatch):
    s = state(limit_exceeded=True)
    bot = manager(s)
    s.limit_reset_time = datetime.now() - timedelta(seconds=1)
    def check():
        s.paused, s.paused_reason = True, "manual"
        return False
    monkeypatch.setattr(module, "get_client", lambda acc: SimpleNamespace(check_limit=check))
    monkeypatch.setattr(module.time, "sleep", Mock())
    tree = ast.parse(textwrap.dedent(inspect.getsource(BotManager._run_account_worker_inner)))
    branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
        and ast.unparse(node.test) == "state.limit_exceeded"
        and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and child.func.attr == "check_limit" for child in ast.walk(node)))
    wrapper = ast.For(target=ast.Name(id="once", ctx=ast.Store()), iter=ast.List(elts=[ast.Constant(0)], ctx=ast.Load()),
                      body=[copy.deepcopy(branch)], orelse=[])
    code = compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "<limit-control-race>", "exec")
    exec(code, dict(vars(module), self=bot, state=s, acc=s.acc, now=datetime.now()))
    assert s.paused and s.paused_reason == "manual" and s.limit_exceeded
    assert activity_view(s)["requires_action"]


def test_api_collection_progress_counts_requests_not_assumed_pages(monkeypatch):
    s = state()
    s.acc["urls"] = ["https://hh.ru/search/vacancy?text=synthetic"]
    bot = manager(s)
    monkeypatch.setattr(module.CONFIG, "pages_per_url", 20)
    monkeypatch.setattr(module, "get_client", lambda acc: SimpleNamespace(search_vacancies=lambda *args, **kwargs: []))
    monkeypatch.setattr("app.vacancy_history.observe", lambda meta: None)
    bot._collect_via_oauth_api(s)
    assert activity_view(s)["progress"] == {"done": 1, "total": 1}
    assert "страниц" not in activity_view(s)["current"]


def test_snapshot_includes_activity_for_regular_active_and_stopped_temp(monkeypatch):
    bot = BotManager()
    bot.account_states = [state()]
    bot.temp_states = {0: state()}
    bot.temp_sessions = [{"name": "active"}, {"name": "stopped", "paused_reason": "outcome_unknown",
                         "pending_apply": {"vacancy_id": "synthetic"}}]
    monkeypatch.setattr(module, "get_oauth_status", lambda *args: {})
    first = bot.get_state_snapshot()
    second = bot.get_state_snapshot()
    assert datetime.fromisoformat(first["snapshot_at"]).tzinfo is not None
    assert len(first["accounts"]) == 3
    assert [row["activity"]["phase"] for row in first["accounts"]] == ["idle", "idle", "stopped"]
    assert first["accounts"][0]["activity"]["started_at"] == second["accounts"][0]["activity"]["started_at"]
    assert first["accounts"][2]["activity"]["requires_action"]
    assert "не подтверждён" in first["accounts"][2]["activity"]["current"]
