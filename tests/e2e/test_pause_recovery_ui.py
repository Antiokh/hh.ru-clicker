"""Synthetic-only pause, connection and read-only reconciliation browser checks."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


def prepare(ui, reason="outcome_unknown"):
    # No external assets/endpoints; UIController separately mocks every /api/*.
    ui.page.route("**/*", lambda route: route.continue_()
                  if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost"}
                  else route.abort())
    acc = ui.state["accounts"][0]
    acc.update(paused=True, paused_reason=reason, current_vacancy_title="Старая вакансия",
               status_detail="Пауза пользователем", bot_active=True)
    if reason == "outcome_unknown":
        acc["pending_apply"] = {"vacancy_id": "12345", "resume_id": "PRIVATE-RESUME",
                                "flow": "apply", "recorded_at": "2026-09-07T00:00:00Z",
                                "reason_code": "transport_unknown"}
    return acc


@pytest.mark.parametrize("width", [390, 1280])
def test_unknown_visible_above_old_vacancy_and_resume_blocked(ui, width):
    prepare(ui)
    ui.open()
    # The existing shared fixture waits for conn-dot visibility; mobile hides it.
    ui.page.set_viewport_size({"width": width, "height": 900})
    vacancy = ui.page.locator("#acc-vacancy-0")
    expect(vacancy).to_contain_text("Исход отклика неизвестен")
    expect(vacancy).to_contain_text("Нужна сверка результата в HH")
    text = vacancy.text_content()
    assert text.index("Нужна сверка") < text.index("Старая вакансия")
    assert "PRIVATE-RESUME" not in text
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()
    expect(ui.page.locator("#acc-application-check-btn-0")).to_be_enabled()
    assert ui.page.evaluate("""() => {
      const region=document.getElementById('acc-vacancy-0');
      return region.scrollWidth <= region.clientWidth+1;
    }""")
    assert ui.page_errors == []
    assert not [c for c in ui.calls if "reconcile-application" in c["path"]]


@pytest.mark.parametrize("reason,label", [("auto_errors", "ошибки подряд"),
                                           ("auth", "нужна авторизация"),
                                           ("limit", "лимит")])
def test_reason_visible_even_with_global_pause(ui, reason, label):
    prepare(ui, reason)
    ui.state["paused"] = True
    ui.open()
    vacancy = ui.page.locator("#acc-vacancy-0")
    expect(vacancy).to_contain_text(re.compile(label, re.I))
    expect(vacancy).to_contain_text("Также включена общая пауза")
    expect(vacancy).not_to_contain_text("Пауза пользователем")
    expect(ui.page.locator("#acc-application-check-btn-0")).not_to_be_visible()
    assert ui.page_errors == []


def test_reconciliation_business_failure_stays_visible_without_ws_command(ui):
    prepare(ui)
    ui.page.add_init_script("localStorage.setItem('hh-api-key', 'synthetic-ui-key')")
    captured_headers = []
    ui.page.on("request", lambda request: captured_headers.append(request.headers)
               if request.url.endswith('/api/account/0/reconcile-application') else None)
    ui.set_response("POST", r"/api/account/0/reconcile-application$",
                    {"ok": False, "message": "Отклик пока не подтверждён. Сверьте HH."})
    ui.open()
    ui.page.click("#acc-application-check-btn-0")
    expect(ui.page.locator("#acc-application-check-result-0")).to_contain_text("пока не подтверждён")
    ui.push_state()
    expect(ui.page.locator("#acc-application-check-result-0")).to_contain_text("пока не подтверждён")
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()
    calls = [c for c in ui.calls if "reconcile-application" in c["path"]]
    assert len(calls) == 1
    assert calls[0]["method"] == "POST" and calls[0]["json"] is None
    assert len(captured_headers) == 1
    assert captured_headers[0].get("x-api-key") == "synthetic-ui-key"
    assert ui.commands == []


def test_reconciliation_confirmation_waits_for_snapshot(ui):
    acc = prepare(ui)
    ui.set_response("POST", r"/api/account/0/reconcile-application$",
                    {"ok": True, "confirmed": True, "pending": False, "paused": False})
    ui.open()
    ui.page.click("#acc-application-check-btn-0")
    expect(ui.page.locator("#acc-application-check-result-0")).to_contain_text("Отклик подтверждён")
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()
    acc.update(paused=False, paused_reason="", pending_apply=None, status_detail="Готов к работе")
    ui.push_state()
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_enabled()
    expect(ui.page.locator("#acc-application-check-btn-0")).not_to_be_visible()
    assert ui.commands == []


def test_offline_keyboard_pause_shows_error_and_is_not_queued(ui):
    prepare(ui, "manual")
    ui.open()
    ui.page.evaluate("State.reconnectDelay = 10000")
    ui.close_ws()
    expect(ui.page.locator("#pause-btn")).to_be_disabled()
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()
    assert ui.page.evaluate("sendCmd({type:'pause_toggle'})") is False
    expect(ui.page.locator("#dbg-err")).to_contain_text("не отправлена и не поставлена в очередь")
    assert ui.commands == []


def scheduled(ui):
    acc = prepare(ui)
    stamp = datetime.now(timezone.utc)
    due = (stamp + timedelta(seconds=90)).isoformat()
    ui.state["snapshot_at"] = stamp.isoformat()
    acc["pending_apply"].update(reconcile_attempts=1, reconcile_next_at=due,
                               reconcile_last_started_at=stamp.isoformat())
    acc["activity"] = {"phase": "outcome_unknown", "current": "Исход отклика пока не подтверждён",
                       "next": "Автоматически проверим результат в HH", "requires_action": False,
                       "started_at": None, "wait_until": due, "progress": None}
    return acc


def test_scheduled_unknown_has_timer_optional_manual_and_one_protective_note(ui):
    acc = scheduled(ui)
    ui.open()
    ui.page.set_viewport_size({"width": 390, "height": 900})
    activity = ui.page.locator("#acc-activity-0")
    expect(activity).to_contain_text("Автоматически проверим результат в HH")
    expect(activity).to_contain_text("Ожидание: ещё")
    expect(activity.locator('[data-activity="action"]')).not_to_be_visible()
    expect(ui.page.locator("#acc-application-check-btn-0")).to_have_text("Проверить сейчас")
    expect(ui.page.locator("#acc-vacancy-0")).to_contain_text("Не отправляйте повторно: HH мог принять отклик")
    expect(ui.page.locator("#acc-vacancy-0")).not_to_contain_text("Исход отклика неизвестен")
    assert activity.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
    assert ui.commands == []
    acc["pending_apply"].update(reconcile_attempts=3, reconcile_next_at=None)
    acc["activity"].update(requires_action=True, wait_until=None,
                           next="Автоматические проверки завершены. Сверьте результат вручную")
    ui.push_state()
    expect(activity).to_contain_text("Требуется ваше действие")
    expect(activity.locator('[data-activity="time"]')).not_to_be_visible()
    expect(ui.page.locator("#acc-application-check-btn-0")).to_have_text("Проверить в HH и продолжить")


def test_new_auto_attempt_clears_previous_manual_failure(ui):
    acc = scheduled(ui)
    ui.set_response("POST", r"/api/account/0/reconcile-application$",
                    {"ok": False, "message": "Предыдущая ручная сверка не подтвердила результат"})
    ui.open()
    ui.page.click("#acc-application-check-btn-0")
    result = ui.page.locator("#acc-application-check-result-0")
    expect(result).to_contain_text("Предыдущая ручная сверка")
    acc["pending_apply"]["reconcile_last_started_at"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    acc["activity"].update(phase="receipt_check", current="Проверяем уже отправленный отклик", wait_until=None)
    ui.push_state()
    expect(result).to_have_text("")
    expect(ui.page.locator("#acc-application-check-btn-0")).to_be_disabled()
    assert len([c for c in ui.calls if "reconcile-application" in c["path"]]) == 1


@pytest.mark.parametrize("width", [390, 1280])
def test_daily_count_is_distinct_from_zero_after_restart(ui, width):
    acc = prepare(ui)
    acc.update(sent=0, daily_sent=84, total_applied=120, hh_today_applies=85,
               hh_today_applies_updated="2026-09-07T00:00:00Z")
    ui.open()
    ui.page.set_viewport_size({"width": width, "height": 900})
    summary = ui.page.locator("#card-0 .acc-apply-summary")
    expect(summary).to_contain_text("Сегодня · бот")
    expect(summary).to_contain_text("За этот запуск")
    expect(ui.page.locator("#acc-daily-0")).to_have_text("84")
    expect(ui.page.locator("#acc-sent-0")).to_have_text("0")
    expect(ui.page.locator("#acc-hh-today-0")).to_contain_text("HH сегодня 85/200")
    assert summary.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
    assert ui.page_errors == []
