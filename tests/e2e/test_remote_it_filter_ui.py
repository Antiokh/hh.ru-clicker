from playwright.sync_api import expect


def test_remote_it_filter_badge_and_control_are_global(ui):
    ui.state['config']['remote_it_only'] = True
    ui.open()
    checkbox = ui.page.locator('.smart-filter-cb[data-key="remote_it_only"]').first
    expect(checkbox).to_be_checked()
    expect(checkbox).to_be_enabled()
    expect(ui.page.locator('#hdr-filters')).to_contain_text('ИТ · только удалёнка')
    checkbox.evaluate('el => { el.checked = false; el.dispatchEvent(new Event("change")); }')
    ui.page.wait_for_timeout(100)
    assert any(cmd.get('key') == 'remote_it_only' and cmd.get('value') is False for cmd in ui.commands)
    assert ui.page_errors == []


def test_old_backend_cannot_toggle_unknown_scope(ui):
    ui.state['config'].pop('remote_it_only', None)
    ui.open()
    expect(ui.page.locator('.smart-filter-cb[data-key="remote_it_only"]').first).to_be_disabled()
    assert ui.commands == []
