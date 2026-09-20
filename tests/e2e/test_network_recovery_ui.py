"""Synthetic local websocket/API only: network recovery is never driven by the UI."""
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from tests.e2e.test_auth_recovery_ui import prepare


def network_account(ui):
    acc = prepare(ui, "network_error")
    next_check = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    acc["network_recovery"] = {"reason": "connect_timeout", "attempts": 9,
        "next_check_at": next_check, "last_started_at": None, "last_error": "network"}
    acc["activity"].update(phase="network_recovery", current="Связь с HH недоступна; отклики приостановлены",
        next="Проверить связь без отправки откликов; продолжить только после подтверждения доступа",
        wait_until=next_check, requires_action=False)
    return acc


@pytest.mark.parametrize("width", [390, 1280])
def test_scheduled_network_state_readable_and_never_auto_checks(ui, width):
    network_account(ui)
    ui.open()
    ui.page.set_viewport_size({"width": width, "height": 900})
    activity = ui.page.locator("#acc-activity-0")
    expect(activity.locator('[data-activity="time"]')).to_contain_text("До проверки связи:")
    expect(activity.locator('[data-activity="network"]')).to_contain_text("Попыток проверки связи: 9")
    expect(activity.locator('[data-activity="action"]')).not_to_be_visible()
    button = ui.page.locator("#acc-auth-check-btn-0")
    expect(button).to_have_text("Проверить связь и продолжить")
    expect(button).to_be_enabled()
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()
    expect(ui.page.locator("#acc-badge-0")).to_contain_text("связи")
    assert activity.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
    assert button.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
    ui.page.wait_for_timeout(1100)  # Exercise the local clock tick, not a network poll.
    assert not [c for c in ui.calls if c["path"].endswith("/recheck-auth")]
    assert ui.commands == [] and ui.page_errors == []


def test_explicit_network_check_waits_for_proof_and_snapshot(ui):
    acc = network_account(ui)
    ui.set_response("POST", r"/api/account/0/recheck-auth$",
        {"ok": True, "verified": True, "paused": False})
    ui.open()
    ui.page.click("#acc-auth-check-btn-0")
    expect(ui.page.locator("#acc-auth-check-result-0")).to_contain_text("Связь и доступ к HH подтверждены")
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()
    calls = [c for c in ui.calls if c["path"].endswith("/recheck-auth")]
    assert len(calls) == 1 and calls[0]["method"] == "POST" and calls[0]["json"] is None
    acc.update(paused=False, paused_reason="", network_recovery=None)
    acc["activity"].update(phase="idle", current="Готов к следующему циклу", wait_until=None)
    ui.push_state()
    expect(ui.page.locator("#acc-auth-check-0")).not_to_be_visible()
    expect(ui.page.locator("#acc-activity-0 [data-activity=network]")).not_to_be_visible()
    assert ui.commands == []


def test_terminal_access_result_stops_timer_and_stale_panel_hides_schedule(ui):
    acc = network_account(ui)
    ui.open()
    activity = ui.page.locator("#acc-activity-0")
    acc["network_recovery"].update(last_error="challenge", next_check_at=None)
    acc["activity"].update(phase="network_error", current="HH запрашивает проверку доступа",
                           next="Нужна ручная проверка HH", wait_until=None, requires_action=True)
    ui.push_state()
    expect(activity.locator('[data-activity="network"]')).to_contain_text("Автопроверки остановлены")
    expect(activity.locator('[data-activity="action"]')).to_have_text("Требуется ваше действие")
    expect(activity.locator('[data-activity="time"]')).not_to_be_visible()
    ui.page.evaluate("State.reconnectDelay=10000")
    ui.close_ws()
    expect(ui.page.locator("#acc-auth-check-btn-0")).to_be_disabled()
    expect(activity.locator('[data-activity="freshness"]')).to_contain_text("Нет свежих данных")
    assert not [c for c in ui.calls if c["path"].endswith("/recheck-auth")]


def test_new_auto_attempt_clears_old_manual_feedback(ui):
    acc = network_account(ui)
    ui.set_response("POST", r"/api/account/0/recheck-auth$",
        {"ok": False, "verified": False, "paused": True, "reason": "network",
         "message": "Предыдущая ручная проверка не установила связь"})
    ui.open()
    ui.page.click("#acc-auth-check-btn-0")
    result = ui.page.locator("#acc-auth-check-result-0")
    expect(result).to_contain_text("Предыдущая ручная проверка")
    acc["network_recovery"]["last_started_at"] = datetime.now(timezone.utc).isoformat()
    acc["activity"].update(phase="auth_check", current="Проверяем связь с HH", wait_until=None)
    ui.push_state()
    expect(result).to_have_text("")
    expect(ui.page.locator("#acc-auth-check-btn-0")).to_be_disabled()
    expect(ui.page.locator("#acc-auth-check-btn-0")).to_have_text("Проверяем связь с HH…")
    assert len([c for c in ui.calls if c["path"].endswith("/recheck-auth")]) == 1
