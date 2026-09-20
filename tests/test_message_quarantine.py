import importlib
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
