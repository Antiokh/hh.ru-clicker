from playwright.sync_api import expect


def test_quarantine_list_and_legacy_warning(ui):
    ui.open()
    ui.page.route('**/api/llm/quarantine', lambda route: route.fulfill(json={
        'ok': True, 'items': [{'chat_id': '123', 'account': 'Synthetic',
        'reason': 'Повтор запрещён', 'recorded_at': '2026-09-20', 'can_review': False}]}))
    ui.page.evaluate('llmQuarantineLoad()')
    panel = ui.page.locator('#llm-quarantine')
    expect(panel).to_contain_text('Synthetic')
    expect(panel).to_contain_text('Старая запись')
    expect(panel.locator('a')).to_have_attribute('href', 'https://hh.ru/chat/123')
    expect(panel.locator('button')).to_have_count(0)


def test_quarantine_error_is_not_empty_list(ui):
    ui.open()
    ui.page.route('**/api/llm/quarantine', lambda route: route.fulfill(status=500, body='error'))
    ui.page.evaluate('llmQuarantineLoad()')
    expect(ui.page.locator('#llm-quarantine')).to_contain_text('Не удалось загрузить')
