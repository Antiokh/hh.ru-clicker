"""Real browser actions against synthetic local auth verification responses."""
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


def prepare(ui, reason="auth"):
    ui.page.route("**/*", lambda route: route.continue_()
                  if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost"}
                  else route.abort())
    stamp = datetime.now(timezone.utc).isoformat()
    ui.state["snapshot_at"] = stamp
    acc = ui.state["accounts"][0]
    acc.update(paused=True, paused_reason=reason, mode="oauth", cookies_expired=False, pending_apply=None)
    acc["activity"] = {"phase": reason, "current": "Нужна проверка входа в HH",
                       "next": "Проверьте доступ кнопкой ниже", "started_at": None,
                       "wait_until": None, "requires_action": True, "progress": None}
    return acc


@pytest.mark.parametrize("width", [390, 1280])
def test_auth_card_requires_explicit_proof_and_never_auto_requests(ui, width):
    prepare(ui)
    ui.open()
    ui.page.set_viewport_size({"width": width, "height": 900})
    button = ui.page.locator("#acc-auth-check-btn-0")
    expect(button).to_be_visible()
    expect(button).to_be_enabled()
    expect(button).to_have_text("Проверить вход и продолжить")
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()
    assert button.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
    assert not [c for c in ui.calls if c["path"].endswith("/recheck-auth")]
    assert ui.commands == []
    assert ui.page_errors == []


def test_verified_auth_uses_key_wrapper_and_waits_for_fresh_snapshot(ui):
    acc = prepare(ui)
    ui.page.add_init_script("localStorage.setItem('hh-api-key','synthetic-auth-ui-key')")
    headers = []
    ui.page.on("request", lambda request: headers.append(request.headers)
               if request.url.endswith('/api/account/0/recheck-auth') else None)
    ui.set_response("POST", r"/api/account/0/recheck-auth$",
                    {"ok": True, "verified": True, "paused": False, "message": "Вход подтверждён"})
    ui.open()
    ui.page.click("#acc-auth-check-btn-0")
    expect(ui.page.locator("#acc-auth-check-result-0")).to_contain_text("Вход в HH подтверждён")
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()
    calls = [c for c in ui.calls if c["path"].endswith('/recheck-auth')]
    assert len(calls) == 1 and calls[0]["method"] == "POST" and calls[0]["json"] is None
    assert len(headers) == 1 and headers[0].get("x-api-key") == "synthetic-auth-ui-key"
    acc.update(paused=False, paused_reason="")
    acc["activity"].update(phase="idle", current="Ожидаем следующий цикл", requires_action=False)
    ui.push_state()
    expect(ui.page.locator("#acc-auth-check-0")).not_to_be_visible()
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_enabled()
    assert ui.commands == []


def test_network_failure_is_not_labeled_as_expired_login(ui):
    prepare(ui)
    ui.set_response("POST", r"/api/account/0/recheck-auth$",
                    {"ok": False, "verified": False, "paused": True, "reason": "network",
                     "message": "HH сейчас недоступен по сети. Проверка входа не завершена."})
    ui.open()
    ui.page.click("#acc-auth-check-btn-0")
    result = ui.page.locator("#acc-auth-check-result-0")
    expect(result).to_contain_text("недоступен по сети")
    expect(result).not_to_contain_text("истёк")
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()
    ui.push_state()
    expect(result).to_contain_text("недоступен по сети")
    assert len([c for c in ui.calls if c["path"].endswith('/recheck-auth')]) == 1


def test_runtime_auth_check_is_busy_and_disconnection_disables_check(ui):
    acc = prepare(ui)
    acc["activity"].update(phase="auth_check", current="Проверяем вход в HH",
                           requires_action=False, started_at=datetime.now(timezone.utc).isoformat())
    ui.open()
    button = ui.page.locator("#acc-auth-check-btn-0")
    expect(button).to_be_disabled()
    expect(button).to_have_text("Проверяем вход в HH…")
    assert ui.page.evaluate("recheckAccountAuth(0)") is False
    assert not [c for c in ui.calls if c["path"].endswith('/recheck-auth')]
    acc["activity"].update(phase="auth", requires_action=True)
    ui.push_state()
    expect(button).to_be_enabled()
    ui.page.evaluate("State.reconnectDelay=10000")
    ui.close_ws()
    expect(button).to_be_disabled()


@pytest.mark.parametrize("reason,label", [("hh_rate_limit", "частоту запросов"),
                                           ("challenge", "проверку доступа")])
def test_other_access_failures_are_not_mislabeled_or_given_auth_button(ui, reason, label):
    acc = prepare(ui, reason)
    acc["activity"].update(current="Проверка доступа приостановлена", next="Нужна ручная проверка HH")
    ui.open()
    expect(ui.page.locator("#acc-auth-check-0")).not_to_be_visible()
    expect(ui.page.locator("#acc-badge-0")).to_contain_text(label)
    assert ui.commands == []


def test_non_oauth_auth_pause_requires_login_not_unsupported_verifier(ui):
    acc = prepare(ui)
    acc.update(mode="web", use_oauth=True)
    ui.open()
    expect(ui.page.locator("#acc-auth-check-0")).not_to_be_visible()
    expect(ui.page.locator("#acc-pause-btn-0")).to_be_disabled()
    expect(ui.page.locator("#acc-pause-btn-0")).to_have_attribute("title", "Восстановите авторизацию этого аккаунта в HH. Обычное продолжение не снимает защитную паузу.")
    assert ui.page.evaluate("recheckAccountAuth(0)") is False
    assert not [c for c in ui.calls if c["path"].endswith('/recheck-auth')]


@pytest.mark.parametrize("block", ["global", "hard_stopped", "limit_exceeded"])
def test_auth_check_does_not_override_other_holds(ui, block):
    acc = prepare(ui)
    if block == "global":
        ui.state["paused"] = True
    else:
        acc[block] = True
    ui.open()
    expect(ui.page.locator("#acc-auth-check-btn-0")).to_be_disabled()
    expect(ui.page.locator("#acc-auth-check-result-0")).to_contain_text("сейчас недоступна")
    assert ui.page.evaluate("recheckAccountAuth(0)") is False
    assert not [c for c in ui.calls if c["path"].endswith('/recheck-auth')]
