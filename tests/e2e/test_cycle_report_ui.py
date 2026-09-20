"""Cycle accounting shown using synthetic snapshots and mocked local APIs only."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
import re
from playwright.sync_api import expect


def prepare(ui):
    ui.page.route("**/*", lambda route: route.continue_()
                  if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost"}
                  else route.abort())
    stamp = datetime.now(timezone.utc)
    ui.state["snapshot_at"] = stamp.isoformat()
    acc = ui.state["accounts"][0]
    acc.update(daily_sent=84, sent=0)
    acc["activity"] = {"phase": "preflight", "current": "Проверяем следующую вакансию",
                       "next": "Учесть результат проверки", "started_at": stamp.isoformat(),
                       "wait_until": None, "progress": None, "requires_action": False}
    acc["cycle_report"] = {
        "cycle_id": "synthetic-cycle", "started_at": stamp.isoformat(), "finished_at": None,
        "status": "running", "found_raw": 10, "found_unique": 8, "considered": 8,
        "processed": 7, "sent": 2, "already": 2, "skipped": 3,
        "skip_reasons": [{"key": "already", "label": "Уже откликались", "count": 2},
                         {"key": "blacklist", "label": "Чёрный список", "count": 1}],
        "errors": 1, "unknown": 1, "questionnaires_pending": 1, "remaining": 1,
        "partial": False, "operation_errors": 0,
    }
    return acc


@pytest.mark.parametrize('guard', ['none', 'paused', 'partial', 'errors', 'reserve', 'stale'])
def test_exhausted_search_explanation_does_not_mask_other_states(ui, guard):
    acc = prepare(ui)
    acc['activity'].update(phase='cycle_wait', current='Цикл завершён', next='Повторит поиск')
    report = acc['cycle_report']
    report.update(status='waiting', finished_at=report['started_at'], found_unique=548,
                  processed=548, skipped=548, already=542, sent=0, remaining=0,
                  errors=0, unknown=0, questionnaires_pending=0,
                  skip_reasons=[{'key':'already','label':'Уже откликались','count':542},
                                {'key':'questionnaire_incomplete','label':'Анкета не завершена','count':5},
                                {'key':'missing_title','label':'Нет названия','count':1}])
    if guard == 'paused': acc['paused'] = True
    if guard == 'partial': report['partial'] = True
    if guard == 'errors': report['errors'] = 1
    if guard == 'reserve': report['skip_reasons'].append({'key':'fresh_reserve','label':'Резерв','count':1})
    ui.open()
    block = ui.page.locator('#acc-activity-0')
    if guard == 'stale':
        ui.page.evaluate('State.snapshotReceivedAt=performance.now()-16000')
        expect(block).to_have_class(re.compile('.*is-stale.*'))
    if guard == 'none':
        expect(block.locator('[data-activity="current"]')).to_have_text('В текущем поиске почти всё уже обработано')
        expect(block.locator('[data-activity="situation"]')).to_contain_text('Уже откликались: 542 (за разные дни)')
        expect(block.locator('[data-activity="situation"]')).to_contain_text('Анкета не завершена: 5')
        summary = block.locator('[data-activity="situation"]')
        assert summary.inner_text().splitlines() == [
            'Найдено: 548', 'Уже откликались: 542 (за разные дни)',
            '• Анкета не завершена: 5', '• Нет названия: 1']
        assert summary.evaluate('el => getComputedStyle(el).whiteSpace') == 'pre-line'
    else:
        expect(block.locator('[data-activity="situation"]')).not_to_be_visible()
        expect(block.locator('[data-activity="current"]')).to_have_text('Цикл завершён')
    assert ui.commands == []
    assert ui.page_errors == []


@pytest.mark.parametrize("width", [390, 1280])
def test_cycle_counts_are_separate_from_today_and_session_and_fit(ui, width):
    prepare(ui)
    ui.open()
    ui.page.set_viewport_size({"width": width, "height": 900})
    cycle = ui.page.locator("#acc-cycle-0")
    expect(cycle).to_contain_text("За текущий цикл")
    for key, value in {"found": "8", "sent": "2", "skipped": "3", "remaining": "1"}.items():
        expect(cycle.locator(f'[data-cycle="{key}"]')).to_have_text(value)
    expect(cycle).to_contain_text("Разобрано: 7")
    expect(ui.page.locator("#acc-daily-0")).to_have_text("84")
    expect(ui.page.locator("#acc-sent-0")).to_have_text("0")
    assert cycle.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
    columns = cycle.locator('.acc-cycle-grid').evaluate("el=>getComputedStyle(el).gridTemplateColumns.split(' ').length")
    assert columns == 4
    expect(cycle.locator('[data-cycle="processed"]')).not_to_be_visible()
    expect(cycle.locator('[data-cycle="preview"]')).not_to_be_visible()
    cycle.locator('summary').click()
    expect(cycle.locator('[data-cycle="processed"]')).to_be_visible()
    ui.page.evaluate("toggleCompact(0)")
    expect(cycle).to_be_visible()
    assert ui.commands == []
    assert ui.page_errors == []


def test_no_cycle_yet_is_unavailable_not_zero(ui):
    acc = prepare(ui)
    acc["cycle_report"] = None
    ui.open()
    cycle = ui.page.locator("#acc-cycle-0")
    expect(cycle).to_contain_text("Отчёт появится с новым циклом поиска. Дневной итог сохранён ниже.")
    expect(cycle.locator('[data-cycle="body"]')).not_to_be_visible()
    expect(cycle.locator('[data-cycle="found"]')).to_have_text("")
    del acc["cycle_report"]
    ui.push_state()
    expect(cycle.locator('[data-cycle="body"]')).not_to_be_visible()


def test_partial_unknown_and_reasons_are_visible_without_invented_remaining(ui):
    acc = prepare(ui)
    acc["cycle_report"].update(partial=True, remaining=None, found_raw=None, operation_errors=1)
    ui.open()
    cycle = ui.page.locator("#acc-cycle-0")
    expect(cycle).to_contain_text("Промежуточный или неполный итог")
    expect(cycle).to_contain_text("Не подтверждено: 1")
    expect(cycle.locator('[data-cycle="remaining"]')).to_have_text("—")
    expect(cycle).to_contain_text("Уже откликались — 2")
    cycle.locator("summary").click()
    expect(cycle).to_contain_text("Уже откликались: 2 (входит в «Пропущено»)")
    expect(cycle).to_contain_text("Найдено до общего удаления дублей: —")


def test_details_survive_fresh_snapshots_but_new_cycle_resets_view(ui):
    acc = prepare(ui)
    acc["cycle_report"].update(status="blocked", finished_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
    ui.open()
    cycle = ui.page.locator("#acc-cycle-0")
    expect(cycle).to_contain_text("За последний цикл")
    cycle.locator("summary").click()
    ui.push_state()
    expect(cycle.locator("details")).to_have_attribute("open", "")
    acc["cycle_report"].update(cycle_id="next-cycle", finished_at=None, status="running")
    ui.push_state()
    expect(cycle).to_contain_text("За текущий цикл")
    assert cycle.locator("details").get_attribute("open") is None


def test_stale_snapshot_warning_covers_cycle_and_untrusted_reasons_are_text(ui):
    acc = prepare(ui)
    acc["cycle_report"]["skip_reasons"] = [{"key": "unknown", "label": '<img src=x onerror="window.injected=true">', "count": 3}]
    ui.open()
    cycle = ui.page.locator("#acc-cycle-0")
    cycle.locator("summary").click()
    expect(cycle.locator('[data-cycle="reasons"]')).to_have_text('<img src=x onerror="window.injected=true">: 3')
    assert cycle.locator("img").count() == 0
    assert ui.page.evaluate("window.injected===true") is False
    ui.page.evaluate("State.snapshotReceivedAt=performance.now()-16000")
    expect(ui.page.locator("#acc-activity-0")).to_contain_text("Нет свежих данных: последнее известное состояние")
    expect(cycle.locator('[data-cycle="sent"]')).to_have_text("2")
