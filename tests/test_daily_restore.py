from app import storage
from app.state import AccountState


def test_counter_uses_moscow_day_and_current_cache(monkeypatch):
    monkeypatch.setattr(storage, '_load_cache', lambda: None)
    monkeypatch.setattr(storage, '_cache_applied', {'test': {
        'one': {'at': '2026-09-04T21:01:00+00:00'},
        'two': {'at': '2026-09-05T23:59:00+03:00'},
        'yesterday': {'at': '2026-09-04T20:59:00+00:00'},
        'bad': {'at': 'invalid'},
    }})
    assert storage.count_applied_on_day('test', '2026-09-05') == 2
    assert storage.count_applied_on_day('other', '2026-09-05') == 0


def test_account_restart_restores_counter_after_cache_replacement(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(storage, '_load_cache', lambda: None)
    monkeypatch.setattr(storage, '_cache_applied', None)
    acc = {'name': 'test', 'short': 'T', 'color': 'red', 'urls': []}
    assert AccountState(acc).daily_sent == 0
    monkeypatch.setattr(storage, '_cache_applied', {'test': {
        'one': {'at': datetime.now(ZoneInfo('Europe/Moscow')).isoformat()}
    }})
    assert AccountState(acc).daily_sent == 1


def test_already_response_does_not_invent_or_refresh_application_date(monkeypatch):
    monkeypatch.setattr(storage, '_load_cache', lambda: None)
    monkeypatch.setattr(storage, '_schedule_save', lambda fn: None)
    monkeypatch.setattr(storage, '_cache_applied', {'test': {
        'old': {'at': '2020-01-01T12:00:00+03:00'}
    }})
    storage.add_applied('test', 'old', confirmed=False)
    storage.add_applied('test', 'unknown', confirmed=False)
    assert storage._cache_applied['test']['old']['at'] == '2020-01-01T12:00:00+03:00'
    assert storage._cache_applied['test']['unknown']['at'] == ''
