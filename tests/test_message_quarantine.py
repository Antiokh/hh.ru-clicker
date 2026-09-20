import pytest
from app import message_quarantine as store
from app.mutation_safety import guarded_method, MutationBlocked


def test_persistent_account_scoped_block(tmp_path, monkeypatch):
    path = tmp_path / 'quarantine.json'
    monkeypatch.setattr(store, 'PATH', path)
    acc = {'user_id': 'owner'}
    store.retain(acc, 'chat')
    assert store.blocked(acc, 'chat')
    assert not store.blocked(acc, 'other')
    assert not store.blocked({'user_id': 'other'}, 'chat')
    assert 'message_outcome_unknown' in path.read_text()


@pytest.mark.parametrize('operation', ['send_message', 'send_workflow_event'])
def test_guard_prevents_repeat_but_allows_other_chats(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(store, 'PATH', tmp_path / 'quarantine.json')
    acc = {'user_id': 'owner'}
    store.retain(acc, 'blocked')
    calls = []
    def method(neg_id, *args):
        calls.append(neg_id)
        return True
    method.__name__ = operation
    guarded = guarded_method(method, acc)
    with pytest.raises(MutationBlocked):
        guarded('blocked', 'text')
    assert not calls
    assert guarded('other', 'text')
    assert calls == ['other']


def test_corrupt_storage_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / 'quarantine.json'
    monkeypatch.setattr(store, 'PATH', path)
    path.write_text('broken')
    assert store.blocked({'user_id': 'owner'}, 'chat')
    with pytest.raises(ValueError):
        store.retain({'user_id': 'owner'}, 'chat')


def test_legacy_resume_key_remains_blocked_after_login():
    store.retain({'resume_hash': 'resume-a'}, 'chat')
    assert store.blocked({'resume_hash': 'resume-a', 'user_id': 'owner'}, 'chat')
    assert store.blocked({'resume_hash': 'resume-b', 'user_id': 'owner'}, 'chat')


def test_malformed_account_bucket_fails_closed():
    store.PATH.write_text('{"owner": []}')
    assert store.blocked({'user_id': 'owner'}, 'chat')


def test_write_ahead_survives_crash_and_blocks_restart():
    acc = {'user_id': 'owner'}
    def send_message(chat_id, text):
        assert store.blocked(acc, chat_id)
        assert text not in store.PATH.read_text()
        raise SystemExit('simulated process termination')
    guarded = guarded_method(send_message, acc)
    with pytest.raises(SystemExit):
        guarded('chat', 'private synthetic message')
    with pytest.raises(MutationBlocked):
        guarded('chat', 'private synthetic message')


def test_success_fingerprint_prevents_repeat_but_allows_new_reply():
    def send_message(chat_id, text):
        return True
    acc = {'user_id': 'owner'}
    guarded = guarded_method(send_message, acc)
    assert guarded('chat', 'first')
    assert not store.blocked(acc, 'chat')
    with pytest.raises(MutationBlocked):
        guarded('chat', 'first')
    assert guarded('chat', 'second')


def test_nested_fallback_uses_single_reservation():
    acc = {'user_id': 'owner'}
    def send_message(chat_id, text):
        return True
    inner = guarded_method(send_message, acc)
    def outer(chat_id, text):
        return inner(chat_id, text)
    outer.__name__ = 'send_message'
    assert guarded_method(outer, acc)('chat', 'text')


@pytest.mark.parametrize('payload', [None, {}, {'unexpected': 'value'}, {'message': {}}, {'message': {'id': True}}])
def test_missing_receipt_is_unknown(payload, monkeypatch):
    from app import mobile_send_message as mobile
    from app.hh_mobile_transport import MobileAPIError
    monkeypatch.setattr(mobile, 'mobile_request', lambda *a, **k: payload)
    with pytest.raises(MobileAPIError) as exc:
        mobile.send_message({'user_id': 'owner'}, 'chat', 'text')
    assert exc.value.outcome_unknown


def test_review_allows_only_new_incoming_message():
    from app.mutation_safety import OutcomeUnknown
    acc = {'user_id': 'owner', '_message_trigger_id': 'hr-message-1'}
    def send_message(chat_id, text):
        raise OutcomeUnknown()
    with pytest.raises(OutcomeUnknown):
        guarded_method(send_message, acc)('chat', 'first draft')
    fingerprint = store.records(acc)['chat']['fingerprint']
    store.acknowledge(acc, 'chat', fingerprint)
    with pytest.raises(MutationBlocked):
        guarded_method(send_message, acc)('chat', 'different generated draft')
    acc['_message_trigger_id'] = 'hr-message-2'
    with pytest.raises(OutcomeUnknown):
        guarded_method(send_message, acc)('chat', 'new reply')


def test_no_review_during_send_or_for_legacy_record():
    acc = {'user_id': 'owner', '_message_trigger_id': 'hr-1'}
    def send_message(chat_id, text):
        fingerprint = store.records(acc)[chat_id]['fingerprint']
        with pytest.raises(ValueError):
            store.acknowledge(acc, chat_id, fingerprint)
        return True
    assert guarded_method(send_message, acc)('chat', 'text')
    store.retain(acc, 'legacy')
    with pytest.raises(ValueError):
        store.acknowledge(acc, 'legacy', None)


def test_storage_failure_prevents_network(monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(store, '_atomic_write_json', Mock(side_effect=OSError))
    calls = []
    def send_message(chat_id, text):
        calls.append(chat_id)
    with pytest.raises(OSError):
        guarded_method(send_message, {'user_id': 'owner'})('chat', 'text')
    assert calls == []


def test_explicit_rejection_does_not_quarantine():
    from app.hh_mobile_transport import MobileAPIError
    acc = {'user_id': 'owner'}
    def send_message(chat_id, text):
        raise MobileAPIError(403)
    with pytest.raises(MobileAPIError):
        guarded_method(send_message, acc)('chat', 'text')
    assert not store.blocked(acc, 'chat')


def test_stop_during_reservation_does_not_leave_ghost_block(monkeypatch):
    allowed = [True]
    real_write = store._atomic_write_json
    def write(path, data):
        real_write(path, data)
        allowed[0] = False
    monkeypatch.setattr(store, '_atomic_write_json', write)
    calls = []
    def send_message(chat_id, text):
        calls.append(chat_id)
        return True
    acc = {'user_id': 'owner', '_mutation_guard': lambda: allowed[0]}
    with pytest.raises(MutationBlocked):
        guarded_method(send_message, acc)('chat', 'text')
    assert calls == []
    assert not store.blocked(acc, 'chat')


@pytest.mark.parametrize('body', [[], None, 'text', 42])
def test_review_rejects_non_object_json(body):
    import asyncio
    from app.routes.llm import api_llm_quarantine_review
    class Request:
        async def json(self):
            return body
    result = asyncio.run(api_llm_quarantine_review(Request()))
    assert result['ok'] is False
