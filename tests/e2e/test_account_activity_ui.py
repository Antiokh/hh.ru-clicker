"""Account activity layout and freshness: synthetic state, no external requests."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


def prepare(ui):
    ui.page.route("**/*", lambda route: route.continue_()
                  if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost"}
                  else route.abort())
    stamp = datetime.now(timezone.utc)
    ui.state["snapshot_at"] = stamp.isoformat()
    ui.state["accounts"][0]["activity"] = {
        "phase": "wait_between_batches", "current": "Ждём между проверками вакансий",
        "next": "Проверить следующую вакансию", "started_at": (stamp - timedelta(seconds=95)).isoformat(),
        "wait_until": (stamp + timedelta(seconds=120)).isoformat(),
        "progress": {"done": 2, "total": 5}, "requires_action": False,
    }


@pytest.mark.parametrize("width", [390, 1280])
def test_activity_current_next_progress_visible_and_contained(ui, width):
    prepare(ui)
    ui.open()
    ui.page.set_viewport_size({"width": width, "height": 900})
    block = ui.page.locator("#acc-activity-0")
    expect(block).to_be_visible()
    expect(block.locator('[data-activity="current"]')).to_have_text("Ждём между проверками вакансий")
    expect(block.locator('[data-activity="next"]')).to_have_text("Проверить следующую вакансию")
    expect(block).to_contain_text("Прогресс этапа: 2 / 5")
    expect(block).to_contain_text("На этом этапе: 1 мин")
    expect(block).to_contain_text("Ожидание: ещё")
    assert block.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
    ui.page.evaluate("toggleCompact(0)")
    expect(block).to_be_visible()
    assert ui.page_errors == []
    assert ui.commands == []


@pytest.mark.parametrize('width', [390, 1280])
def test_activity_redesign_status_and_honest_meter(ui, width):
    prepare(ui)
    ui.page.set_viewport_size({'width': width, 'height': 1100})
    ui.open()
    block = ui.page.locator('#acc-activity-0')
    expect(block.locator('[data-activity="phase"]')).to_have_text('Плановая пауза')
    expect(block).to_have_attribute('data-tone', 'waiting')
    meter = block.locator('[data-activity-meter]')
    expect(meter).to_be_visible()
    assert meter.evaluate('el => [el.value, el.max]') == [2, 5]
    assert ui.page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    ui.page.evaluate('State.snapshotReceivedAt=performance.now()-16000')
    expect(block.locator('[data-activity="phase"]')).to_have_text('Нет свежих данных')
    expect(meter).not_to_be_visible()
    expect(block).to_have_attribute('data-tone', 'stale')
    assert ui.page_errors == []
    assert ui.commands == []


def test_stale_snapshot_marks_old_state_and_hides_countdown(ui):
    prepare(ui)
    ui.open()
    block = ui.page.locator("#acc-activity-0")
    ui.page.evaluate("State.snapshotReceivedAt=performance.now()-16000")
    expect(block).to_contain_text("Нет свежих данных: последнее известное состояние")
    expect(block.locator('[data-activity="time"]')).not_to_be_visible()
    expect(block).to_contain_text("Ждём между проверками вакансий")
    ui.push_state()
    expect(block.locator('[data-activity="time"]')).to_be_visible()
    expect(block).not_to_contain_text("Нет свежих данных")
    assert ui.commands == []


def test_disconnect_is_stale_immediately_and_preserves_pause_controls(ui):
    prepare(ui)
    ui.open()
    ui.page.evaluate("State.reconnectDelay=10000")
    ui.close_ws()
    block = ui.page.locator("#acc-activity-0")
    expect(block).to_contain_text("Нет свежих данных: последнее известное состояние")
    expect(block.locator('[data-activity="time"]')).not_to_be_visible()
    expect(ui.page.locator("#pause-btn")).to_be_disabled()
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()


def test_activity_strings_never_create_markup(ui):
    prepare(ui)
    activity = ui.state["accounts"][0]["activity"]
    activity.update(current='<img src=x onerror="window.injected=true">',
                    next='<script>window.injected=true</script>', requires_action=True)
    ui.open()
    block = ui.page.locator("#acc-activity-0")
    expect(block.locator('[data-activity="current"]')).to_have_text(activity["current"])
    expect(block.locator('[data-activity="next"]')).to_have_text(activity["next"])
    expect(block).to_contain_text("Требуется ваше действие")
    assert block.locator("img,script").count() == 0
    assert ui.page.evaluate("window.injected === true") is False
    assert ui.page_errors == []


def test_old_snapshot_fallback_keeps_current_but_does_not_invent_timers(ui):
    prepare(ui)
    acc = ui.state["accounts"][0]
    del acc["activity"]
    del ui.state["snapshot_at"]
    acc.update(status="checking", status_detail="Проверяю лимит HH")
    ui.open()
    block = ui.page.locator("#acc-activity-0")
    expect(block).to_contain_text("Проверяю лимит HH")
    expect(block).to_contain_text("Пока не указано сервером")
    expect(block.locator('[data-activity="time"]')).not_to_be_visible()
    expect(block.locator('[data-activity="progress"]')).not_to_be_visible()
