"""Page-level layout regression, using synthetic local account snapshots only."""
import pytest
from playwright.sync_api import expect

from tests.e2e.test_network_recovery_ui import network_account


@pytest.mark.parametrize("width", [390, 1280])
def test_main_page_fits_viewport_and_desktop_keeps_sidebar(ui, width):
    acc = network_account(ui)
    acc.update(name="Синтетический аккаунт для проверки длинного имени", daily_sent=145,
               total_applied=123456, hh_today_applies=147, hh_daily_limit=200)
    ui.state["global_stats"].update(total_found=123456, total_sent=12345, storage_total=54321)
    ui.state["uptime_seconds"] = 987654
    ui.open()
    ui.page.set_viewport_size({"width": width, "height": 1000})
    expect(ui.page.locator("#acc-auth-check-btn-0")).to_have_text("Проверить связь и продолжить")
    geometry = ui.page.evaluate("""() => {
      const box = selector => {
        const el = document.querySelector(selector), r = el.getBoundingClientRect();
        return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,
          clientWidth:el.clientWidth,scrollWidth:el.scrollWidth};
      };
      return {viewport:document.documentElement.clientWidth,
        pageWidth:document.documentElement.scrollWidth,header:box('#header'),
        layout:box('#main-layout'),accounts:box('#accounts-grid'),sidebar:box('#sidebar'),
        pause:box('#pause-btn'),check:box('#acc-auth-check-btn-0'),
        columns:getComputedStyle(document.querySelector('#main-layout')).gridTemplateColumns};
    }""")
    assert geometry["pageWidth"] <= width + 1, geometry
    for name in ("header", "layout", "accounts", "sidebar", "pause", "check"):
        assert geometry[name]["left"] >= -1, (name, geometry)
        assert geometry[name]["right"] <= width + 1, (name, geometry)
    if width == 390:
        assert geometry["sidebar"]["top"] >= geometry["accounts"]["bottom"], geometry
        assert len(geometry["columns"].split()) == 1, geometry
    else:
        assert geometry["sidebar"]["left"] > geometry["accounts"]["right"], geometry
        assert geometry["columns"].split()[-1] == "280px", geometry
    assert ui.commands == [] and ui.page_errors == []
    assert not [call for call in ui.calls if call["method"] != "GET"]
