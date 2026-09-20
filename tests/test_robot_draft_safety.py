"""Draft-only mode must cover robot workflow actions as well as plain replies.

Exercise the complete reply processor with synthetic chats. Every network,
LLM, and persistence boundary is replaced before execution; no bot is started.
"""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.manager as manager_mod
from app.state import AccountState


@pytest.fixture
def robot_chat(monkeypatch):
    for key, value in {
        "llm_enabled": True,
        "llm_auto_send": False,
        "llm_use_resume": False,
        "llm_use_cover_letter": False,
        "chat_use_oauth": False,
        "llm_check_interval": 5,
    }.items():
        monkeypatch.setattr(manager_mod.CONFIG, key, value)

    monkeypatch.setattr("app.state.count_applied_on_day", lambda *args: 0)
    state = AccountState({
        "name": "synthetic", "short": "S", "color": "blue", "urls": [],
        "cookies": {}, "llm_enabled": True,
    })
    state.llm_enabled = True
    state._replied_seeded = True

    button = {"text": "Yes"}
    history = [{
        "sender": "employer", "text": "Ready?", "is_bot": True,
        "actions": {"text_buttons": [button]},
    }]
    item = {
        "type": "NEGOTIATION", "unreadCount": 1,
        "lastMessage": {"id": "m1", "participantId": "employer", "text": "Ready?"},
        "resources": {"VACANCY": ["v1"]},
    }
    client = SimpleNamespace(
        fetch_chat_list=Mock(return_value=({"n1": item}, {}, "applicant")),
        fetch_chat_history=Mock(return_value=history),
        send_workflow_event=Mock(return_value=True),
        send_message=Mock(return_value=True),
    )
    monkeypatch.setattr(manager_mod, "get_client", lambda acc: client)
    monkeypatch.setattr(manager_mod, "get_no_chat_neg_ids", lambda: set())
    monkeypatch.setattr(manager_mod, "get_replied_keys", lambda: set())
    monkeypatch.setattr(manager_mod, "_check_chat_locked", lambda item: False)
    monkeypatch.setattr(manager_mod, "_build_thread_from_chat_item", lambda *args: {
        "employer_name": "Synthetic employer", "last_employer_msg": "Ready?",
        "vacancy_title": "Synthetic role", "last_msg_id": "m1",
        "needs_reply": True, "messages": history,
    })
    persistence = Mock()
    monkeypatch.setattr(manager_mod, "upsert_interview", persistence)
    picker = Mock(return_value=(0, "Yes", "test"))
    monkeypatch.setattr("app.llm.pick_robot_button", picker)
    monkeypatch.setattr(manager_mod.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(manager_mod, "log_debug", lambda *args: None)

    def fail_on_swallowed_exception(context, error):
        raise AssertionError(f"Reply processor swallowed an exception: {context}") from error

    monkeypatch.setattr(manager_mod, "log_exception", fail_on_swallowed_exception)
    bot = manager_mod.BotManager.__new__(manager_mod.BotManager)
    bot._llm_sent_lock = threading.Lock()
    bot._llm_sent_global = set()
    bot._llm_sent_by_neg_id = {}
    bot._add_log = Mock()
    bot._push_llm_log = Mock()
    bot._persist_llm_log = Mock()
    return SimpleNamespace(
        bot=bot, state=state, client=client, button=button,
        persistence=persistence, picker=picker,
    )


@pytest.mark.parametrize("workflow", [False, True], ids=["text-button", "workflow-button"])
def test_robot_draft_only_never_dispatches_or_marks_sent(robot_chat, workflow):
    chat = robot_chat
    if workflow:
        chat.button.update(event="APPLICANT_READY", event_params={"answer_id": "yes"})

    chat.bot._process_llm_replies_inner(chat.state)

    chat.client.fetch_chat_history.assert_called_once()
    chat.client.send_workflow_event.assert_not_called()
    chat.client.send_message.assert_not_called()
    assert ("n1", "m1") not in chat.state.llm_replied_msgs
    assert chat.state.llm_replied_count == 0
    assert ("n1", "exception") not in chat.state._llm_temp_skip
    assert chat.state._llm_neg_failures.get("n1", 0) == 0
    assert not any(call.kwargs.get("llm_sent") for call in chat.persistence.call_args_list)
    assert not any(call.args[0].get("sent") for call in chat.bot._push_llm_log.call_args_list)
    # A workflow caption must never become a manually sendable plain-text draft.
    assert ("n1", "m1") not in chat.state._llm_drafts
    assert not any(call.kwargs.get("llm_reply") for call in chat.persistence.call_args_list)


@pytest.mark.parametrize("workflow", [False, True], ids=["text-button", "workflow-button"])
def test_robot_auto_send_still_dispatches_selected_action(robot_chat, monkeypatch, workflow):
    chat = robot_chat
    monkeypatch.setattr(manager_mod.CONFIG, "llm_auto_send", True)
    if workflow:
        chat.button.update(event="APPLICANT_READY", event_params={"answer_id": "yes"})

    chat.bot._process_llm_replies_inner(chat.state)

    if workflow:
        chat.client.send_workflow_event.assert_called_once_with(
            "n1", "APPLICANT_READY", {"answer_id": "yes"},
        )
        chat.client.send_message.assert_not_called()
    else:
        chat.client.send_message.assert_called_once_with("n1", "Yes")
        chat.client.send_workflow_event.assert_not_called()
    assert ("n1", "m1") in chat.state.llm_replied_msgs
    assert chat.state.llm_replied_count == 1
    assert ("n1", "exception") not in chat.state._llm_temp_skip
    assert chat.state._llm_neg_failures.get("n1", 0) == 0


@pytest.mark.parametrize("workflow", [False, True], ids=["text-button", "workflow-button"])
def test_robot_draft_remains_eligible_after_enabling_auto_send(robot_chat, monkeypatch, workflow):
    chat = robot_chat
    if workflow:
        chat.button.update(event="APPLICANT_READY", event_params={"answer_id": "yes"})
    chat.bot._process_llm_replies_inner(chat.state)
    chat.client.send_message.assert_not_called()
    chat.client.send_workflow_event.assert_not_called()

    # A normal draft is not a failure: the next poll must remain eligible.
    monkeypatch.setattr(manager_mod.CONFIG, "llm_auto_send", True)
    chat.bot._process_llm_replies_inner(chat.state)

    dispatcher = chat.client.send_workflow_event if workflow else chat.client.send_message
    assert dispatcher.call_count == 1
    assert chat.state.llm_replied_count == 1
    assert ("n1", "m1") not in chat.state._llm_robot_drafts


def test_robot_repeated_draft_poll_does_not_repeat_picker(robot_chat):
    chat = robot_chat
    chat.button.update(event="APPLICANT_READY")

    chat.bot._process_llm_replies_inner(chat.state)
    chat.bot._process_llm_replies_inner(chat.state)

    chat.picker.assert_called_once()
    chat.client.send_workflow_event.assert_not_called()
    chat.client.send_message.assert_not_called()
    assert ("n1", "exception") not in chat.state._llm_temp_skip


@pytest.mark.parametrize("workflow", [False, True], ids=["text-button", "workflow-button"])
def test_robot_rechecks_auto_send_immediately_before_dispatch(robot_chat, monkeypatch, workflow):
    chat = robot_chat
    monkeypatch.setattr(manager_mod.CONFIG, "llm_auto_send", True)
    if workflow:
        chat.button.update(event="APPLICANT_READY")
    client_lookups = []

    def client_with_late_toggle(acc):
        client_lookups.append(acc)
        # First two clients load the list/history; third is the sender.
        if len(client_lookups) == 3:
            monkeypatch.setattr(manager_mod.CONFIG, "llm_auto_send", False)
        return chat.client

    monkeypatch.setattr(manager_mod, "get_client", client_with_late_toggle)
    chat.bot._process_llm_replies_inner(chat.state)

    assert len(client_lookups) == 3
    chat.client.send_workflow_event.assert_not_called()
    chat.client.send_message.assert_not_called()
    assert ("n1", "m1") not in chat.state.llm_replied_msgs
    assert not chat.bot._llm_sent_global


def test_robot_real_send_exception_retains_retry_backoff(robot_chat, monkeypatch):
    chat = robot_chat
    monkeypatch.setattr(manager_mod.CONFIG, "llm_auto_send", True)
    chat.button.update(event="APPLICANT_READY")
    chat.client.send_workflow_event.side_effect = RuntimeError("synthetic transport failure")
    logged_exception = Mock()
    monkeypatch.setattr(manager_mod, "log_exception", logged_exception)

    chat.bot._process_llm_replies_inner(chat.state)

    logged_exception.assert_called_once()
    assert chat.state._llm_neg_failures["n1"] == 1
    assert ("n1", "exception") in chat.state._llm_temp_skip
    assert ("n1", "m1") not in chat.state.llm_replied_msgs
    assert not chat.bot._llm_sent_global
