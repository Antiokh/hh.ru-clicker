import asyncio
import inspect
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.hh_client_fallback import FallbackHHClient
from app.hh_client_mobile import MobileHHClient
from app.hh_mobile_transport import MobileAPIError
from app.manager import BotManager
from app.mutation_safety import MutationBlocked
from app.state import AccountState
_spec = importlib.util.spec_from_file_location("robot_safety_fixtures", Path(__file__).with_name("test_robot_draft_safety.py"))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
robot_chat = _fixtures.robot_chat


@pytest.mark.parametrize("change", ["pause", "global_pause", "disable_llm", "disable_global_llm", "delete", "stop"])
def test_late_cancel_prevents_robot_dispatch(robot_chat, monkeypatch, change):
    chat = robot_chat
    monkeypatch.setattr("app.manager.CONFIG.llm_auto_send", True)
    chat.button.update(event="APPLICANT_READY")
    chat.bot._stop_event = threading.Event()

    def picker(*args):
        if change == "pause":
            chat.state.paused = True
        elif change == "global_pause":
            chat.bot.paused = True
        elif change == "disable_llm":
            chat.state.llm_enabled = False
        elif change == "disable_global_llm":
            monkeypatch.setattr("app.manager.CONFIG.llm_enabled", False)
        elif change == "delete":
            chat.state._deleted = True
        else:
            chat.bot._stop_event.set()
        return 0, "Yes", "test"

    chat.picker.side_effect = picker
    chat.bot._process_llm_replies_inner(chat.state)
    chat.client.send_workflow_event.assert_not_called()
    chat.client.send_message.assert_not_called()
    assert not chat.bot._llm_sent_global


@pytest.mark.parametrize("status", [0, 500, 502])
@pytest.mark.parametrize("method", ["submit_response", "fill_questionnaire"])
def test_ambiguous_async_write_never_falls_back(status, method):
    mobile = SimpleNamespace(acc={})
    web = SimpleNamespace()
    setattr(mobile, method, AsyncMock(side_effect=MobileAPIError(status)))
    setattr(web, method, AsyncMock())
    with pytest.raises(MobileAPIError):
        asyncio.run(getattr(FallbackHHClient(mobile, web), method)("synthetic"))
    getattr(web, method).assert_not_called()


def test_cancel_is_checked_when_coroutine_executes():
    allowed = True
    client = MobileHHClient({"_mutation_guard": lambda: allowed})
    task = client.submit_response("synthetic")
    allowed = False
    with pytest.raises(MutationBlocked):
        asyncio.run(task)


def test_mobile_transport_rechecks_after_token_lookup(monkeypatch):
    from app import hh_mobile_transport as transport
    allowed = True
    acc = {"_mutation_guard": lambda: allowed}

    def token(account):
        nonlocal allowed
        allowed = False
        return "synthetic"

    send = Mock()
    monkeypatch.setattr(transport.oauth, "_obtain_oauth_token", token)
    monkeypatch.setattr(transport.requests.Session, "request", send)
    with pytest.raises(MutationBlocked):
        transport.mobile_request(acc, "POST", "/negotiations")
    send.assert_not_called()


@pytest.mark.parametrize("body", [b"<html>error</html>", b""])
def test_http_200_invalid_apply_is_unknown(monkeypatch, body):
    from app import hh_mobile_transport as transport
    response = Mock(status_code=200, content=body)
    response.json.side_effect = ValueError("not JSON")
    monkeypatch.setattr(transport.requests.Session, "request", Mock(return_value=response))
    monkeypatch.setattr(transport.oauth, "_obtain_oauth_token", lambda acc: "synthetic")
    monkeypatch.setattr(transport, "mobile_headers", lambda *args: {})
    monkeypatch.setattr(transport, "egress_proxies", lambda: {})
    with pytest.raises(MobileAPIError) as error:
        transport.mobile_request({}, "POST", "/negotiations")
    assert error.value.outcome_unknown


def _state(**extra):
    return AccountState({"name": "fake", "short": "S", "color": "blue", "urls": [], **extra})


def test_pause_state_restored_and_start_does_not_reset_it():
    state = _state(paused=True, paused_reason="manual", limit_exceeded=True, hard_stopped=True)
    assert state.paused and state.paused_reason == "manual"
    assert state.limit_exceeded and state.hard_stopped
    assert 'ts["paused"] = False' not in inspect.getsource(BotManager.start)


def test_pause_changes_are_persisted_for_regular_and_temp(monkeypatch):
    state = _state()
    temp = _state()
    bot = BotManager.__new__(BotManager)
    bot.account_states = [state]
    bot.temp_states = {0: temp}
    bot.temp_sessions = [{}]
    bot._add_log = Mock()
    save_regular, save_temp = Mock(), Mock()
    monkeypatch.setattr("app.manager.save_accounts", save_regular)
    monkeypatch.setattr("app.manager.save_browser_sessions", save_temp)
    bot.toggle_account_pause(1)
    assert bot.temp_sessions[0]["paused"] is True
    assert bot.temp_sessions[0]["paused_reason"] == "manual"
    save_temp.assert_called_once_with(bot.temp_sessions, wait=True)
    state.paused = True
    state.paused_reason = "manual"
    bot._persist_pauses()
    assert state.acc["paused"] is True
    assert state.acc["paused_reason"] == "manual"
    assert save_regular.call_count == 2


def test_stopped_worker_does_not_change_profile(monkeypatch):
    setter = Mock()
    monkeypatch.setattr("app.manager.get_client", lambda acc: SimpleNamespace(set_job_search_status=setter))
    bot = BotManager.__new__(BotManager)
    bot._stop_event = threading.Event()
    bot._stop_event.set()
    state = _state(paused=True)
    state._deleted = True
    bot._run_account_worker_inner(0, state)
    setter.assert_not_called()


def test_checked_resume_is_used_without_second_selection(monkeypatch):
    from app import mobile_apply, mobile_precheck
    acc = {"resume_hash": "default", "all_resumes": [{"id": "A"}, {"id": "B"}]}
    monkeypatch.setattr(mobile_precheck, "pick_suitable_resume", lambda *args: "A")
    monkeypatch.setattr(mobile_precheck, "mobile_request", lambda *args, **kwargs: {"data_inconsistency": []})
    checked = mobile_precheck.check_vacancy_before_apply(acc, "v")
    attempt = {**acc, "resume_hash": checked["resume_id"], "_pinned_resume_id": checked["resume_id"]}
    select = Mock(side_effect=AssertionError("Must not reselect"))
    monkeypatch.setattr(mobile_apply, "get_suitable_resumes", select)
    submit = Mock(return_value={"id": "synthetic"})
    monkeypatch.setattr(mobile_apply, "mobile_request", submit)
    assert mobile_apply.submit_response(attempt, "v", "default")["ok"]
    assert submit.call_args.kwargs["form"]["resume_id"] == "A"
    select.assert_not_called()
    assert acc["resume_hash"] == "default"


@pytest.mark.parametrize("payload", [{}, [], False, "bad", {"error": "unavailable"}])
def test_invalid_precheck_never_grants_permission(monkeypatch, payload):
    from app import mobile_precheck
    monkeypatch.setattr(mobile_precheck, "pick_suitable_resume", lambda *args: "A")
    monkeypatch.setattr(mobile_precheck, "mobile_request", lambda *args, **kwargs: payload)
    assert mobile_precheck.check_vacancy_before_apply({}, "v")["ok"] is False


@pytest.mark.parametrize("module,fn,args", [
    ("mobile_send_message", "send_message", ("chat", "text")),
    ("mobile_touch_resume", "touch_resume", ("resume",)),
    ("mobile_chat_actions", "send_event", ("chat", "READY")),
])
def test_helpers_propagate_unknown_without_fallback(monkeypatch, module, fn, args):
    from importlib import import_module
    mod = import_module("app." + module)
    request = Mock(side_effect=MobileAPIError(200, outcome_unknown=True))
    monkeypatch.setattr(mod, "mobile_request", request)
    with pytest.raises(MobileAPIError) as error:
        getattr(mod, fn)({}, *args)
    assert error.value.outcome_unknown
    assert request.call_count == 1


def test_manual_unknown_pauses_without_counting_success(monkeypatch):
    from app.routes import apply as route
    state = AccountState({"name": "synthetic", "short": "s", "color": "green",
                          "urls": [], "resume_hash": "resume-a"})
    saved = Mock()
    applied = Mock()
    monkeypatch.setattr(route.bot, "_get_apply_state", lambda idx: state)
    monkeypatch.setattr(route.bot, "_persist_pauses", saved)
    monkeypatch.setattr(route, "add_applied", applied)
    client = SimpleNamespace(submit_response=AsyncMock(return_value=("unknown", {})))
    result = asyncio.run(route._mobile_submit_response(0, {"letter": ""}, "v", client))
    assert result["status"] == "unknown"
    assert state.paused and state.sent == 0
    assert state.paused_reason == "outcome_unknown"
    assert state.pending_apply["vacancy_id"] == "v"
    assert state.pending_apply["resume_id"] == "resume-a"
    saved.assert_called_once()
    applied.assert_not_called()


def test_temp_manual_apply_uses_live_pause_guard():
    bot = object.__new__(BotManager)
    bot.account_states = []
    state = AccountState({"name": "synthetic", "short": "s", "color": "green", "resume_hash": "r", "urls": []})
    bot.temp_states = {0: state}
    bot.temp_sessions = [{"name": "synthetic"}]
    bot.paused = False
    bot._stop_event = threading.Event()
    account = bot._get_apply_acc(0)
    assert account["_mutation_guard"]()
    state.paused = True
    assert not account["_mutation_guard"]()
    assert bot._get_apply_state(0) is state
