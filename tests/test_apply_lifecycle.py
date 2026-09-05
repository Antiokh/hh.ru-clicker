from types import SimpleNamespace

from app.manager import BotManager, _cap_apply_batch, _completed_apply_results
from app.config import CONFIG


def test_batch_cannot_exceed_last_daily_slot_without_fresh_mode(monkeypatch):
    monkeypatch.setattr(CONFIG, 'fresh_vacancies_mode', False)
    monkeypatch.setattr(CONFIG, 'daily_apply_limit', 100)
    monkeypatch.setattr(CONFIG, 'hh_daily_limit', 200)
    assert _cap_apply_batch(['a', 'b', 'c'], 99, 90) == ['a']
    assert _cap_apply_batch(['a'], 90, 100) == []


def test_record_successes_before_errors_that_stop_processing():
    results = [('limit', {}), ('sent', {}), ('already', {})]
    assert [v for v, r in _completed_apply_results(['a', 'b', 'c'], results)] == ['b', 'c', 'a']
    assert list(_completed_apply_results(['a', 'b'], [('sent', {})])) == [('a', ('sent', {}))]


def test_stop_keeps_restart_blocked_while_old_worker_alive(monkeypatch):
    mgr = BotManager()
    session = {'name': 'test', 'resume_hash': 'test-resume'}
    worker = SimpleNamespace(is_alive=lambda: True, join=lambda timeout: None)
    state = SimpleNamespace(_workers=[worker], short='T', _deleted=False, paused=False)
    mgr.temp_sessions = [session]
    mgr.temp_states = {0: state}
    monkeypatch.setattr('app.manager.save_browser_sessions', lambda *a: None)
    monkeypatch.setattr(mgr, '_add_log', lambda *a: None)
    assert mgr.deactivate_session(0)
    assert state._deleted and state.paused
    assert not mgr.activate_session(0)
    assert not mgr.temp_states
