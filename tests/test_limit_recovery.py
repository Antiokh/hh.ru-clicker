from types import SimpleNamespace

import pytest
from app.manager import BotManager
from app.state import AccountState


@pytest.mark.parametrize('local,remote,day,reason,expected', [
    (50, 10, '2026-09-05', 'limit', True),
    (10, 10, '2026-09-05', 'limit', False),
    (10, 10, '2026-09-04', 'limit', True),
    (10, 10, '2026-09-05', 'manual', True),
])
def test_recovery_respects_local_limit_date_and_manual_pause(monkeypatch, local, remote, day, reason, expected):
    monkeypatch.setattr('app.manager._today_msk', lambda: '2026-09-05')
    monkeypatch.setattr('app.manager.CONFIG.daily_apply_limit', 50)
    monkeypatch.setattr('app.manager.CONFIG.hh_daily_limit', 200)
    monkeypatch.setattr('app.manager.fetch_negotiations_today_count',
                        lambda *a, **k: {'today': remote, 'msk_date': day})
    monkeypatch.setattr('app.manager.fetch_negotiations_statistic', lambda *a: {})
    state = AccountState({'name': 'test', 'short': 'T', 'color': 'red', 'urls': [], 'resume_hash': 'test'})
    state.daily_sent = local
    state.paused = True
    state.hard_stopped = True
    state.paused_reason = reason
    mgr = BotManager.__new__(BotManager)
    mgr.account_states = [state]
    mgr.temp_states = {}
    mgr._add_log = lambda *a, **k: None
    mgr._stop_event = SimpleNamespace(wait=lambda seconds: seconds == 300, is_set=lambda: False)
    mgr._hh_limit_tracker_worker()
    assert state.paused is expected
    assert state.daily_sent == local
