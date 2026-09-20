"""Тесты mobile-анкеты (Phase 3): app/mobile_questionnaire.py и
MobileHHClient.fill_questionnaire.

Факт из разбора APK (ru.hh.android v26.28.1): нативного Retrofit-endpoint'а
для анкет/опросов НЕТ — официальное приложение при error_type=test_required
открывает WEB-страницу анкеты (applicant/vacancy_response) в webview.
Решение Phase 3 — делегирование в web-flow
hh_apply.fill_and_submit_questionnaire (семантика официального приложения).

Проверяется:
- модуль mobile_questionnaire.fill_questionnaire делегирует в
  hh_apply.fill_and_submit_questionnaire: аргументы (acc, vid,
  vacancy_title, company) пробрасываются позиционно, результат
  возвращается как есть;
- клиентский метод MobileHHClient.fill_questionnaire делегирует в модуль
  mobile_questionnaire (подставляя self.acc первым аргументом);
- NotImplementedError больше НЕ кидается — метод реально отрабатывает
  (smoke), дефолтные vacancy_title/company пробрасываются как "".

Стиль monkeypatch'а — как в test_hh_client_delegates.py: патчим атрибуты
МОДУЛЕЙ (делегаты вызывают функции через атрибут модуля). Async-вызовы —
asyncio.run в отдельном потоке (_run_coro): pytest-playwright (tests/e2e/)
может держать «running» loop в главном потоке до конца сессии.
"""
import asyncio
import concurrent.futures
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from app import hh_apply, mobile_auth, mobile_questionnaire, oauth
from app.hh_client_mobile import MobileHHClient

ACC = {"name": "a1", "cookies": {}, "resume_hash": "rh1"}


def _run_coro(coro):
    # pytest-playwright (tests/e2e/) держит asyncio-loop «running» в главном
    # потоке до конца сессии, поэтому прямой asyncio.run() падает с
    # RuntimeError. Запускаем корутину в отдельном потоке — там лупа нет.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def test_module_delegates_to_hh_apply(monkeypatch):
    """mobile_questionnaire.fill_questionnaire →
    hh_apply.fill_and_submit_questionnaire: аргументы позиционно,
    результат без преобразования."""
    sentinel = ("sent", {"ok": True})
    calls = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(hh_apply, "fill_and_submit_questionnaire", fake)

    result = _run_coro(
        mobile_questionnaire.fill_questionnaire(ACC, "v1", "Заголовок", "Ромашка"))

    assert result is sentinel  # возвращено как есть
    assert len(calls) == 1
    fwd_args, fwd_kwargs = calls[0]
    assert fwd_args == (ACC, "v1", "Заголовок", "Ромашка")
    assert fwd_args[0] is ACC  # тот же объект, не копия
    assert fwd_kwargs == {}  # делегирование только позиционное


def test_client_method_delegates_to_module(monkeypatch):
    """MobileHHClient.fill_questionnaire →
    mobile_questionnaire.fill_questionnaire (self.acc первым аргументом)."""
    sentinel = ("test", {})
    calls = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(mobile_questionnaire, "fill_questionnaire", fake)

    result = _run_coro(MobileHHClient(ACC).fill_questionnaire("v2", "T", "C"))

    assert result is sentinel
    assert len(calls) == 1
    fwd_args, fwd_kwargs = calls[0]
    assert fwd_args == (ACC, "v2", "T", "C")
    assert fwd_args[0] is ACC
    assert fwd_kwargs == {}


def test_fill_questionnaire_no_longer_raises_not_implemented(monkeypatch):
    """Smoke: NotImplementedError больше не кидается — полный путь
    клиент → модуль → hh_apply отрабатывает; дефолтные vacancy_title и
    company пробрасываются как пустые строки."""
    calls = []

    async def fake(*args, **kwargs):
        calls.append(args)
        return ("sent", {})

    monkeypatch.setattr(hh_apply, "fill_and_submit_questionnaire", fake)

    result = _run_coro(MobileHHClient(ACC).fill_questionnaire("v1"))

    assert result == ("sent", {})
    assert calls == [(ACC, "v1", "", "")]


def test_oauth_questionnaire_uses_ephemeral_web_account(monkeypatch):
    oauth_acc = {**ACC, "mode": "oauth"}
    ephemeral = {**oauth_acc, "cookies": {"hhtoken": "temp", "_xsrf": "csrf"}}
    calls = []

    async def fake_autologin(acc):
        assert acc is oauth_acc
        return ephemeral

    async def fake_submit(*args, **kwargs):
        calls.append((args, kwargs))
        return "sent", {}

    monkeypatch.setattr(mobile_questionnaire, "oauth_web_account", fake_autologin)
    monkeypatch.setattr(hh_apply, "fill_and_submit_questionnaire", fake_submit)
    assert _run_coro(mobile_questionnaire.fill_questionnaire(oauth_acc, "v9")) == ("sent", {})
    assert calls == [((ephemeral, "v9", "", ""), {"receipt_account": oauth_acc})]
    assert "receipt_account" not in ephemeral


SENSITIVE = "synthetic-secret https://synthetic-user:synthetic-password@proxy.test/private?token=synthetic-token"


def bridge_failure(monkeypatch, error):
    bridge = AsyncMock(side_effect=error)
    submit = AsyncMock(side_effect=AssertionError("A failed bridge must not submit"))
    monkeypatch.setattr(mobile_questionnaire, "oauth_web_account", bridge)
    monkeypatch.setattr(hh_apply, "fill_and_submit_questionnaire", submit)
    acc = {**ACC, "mode": "oauth"}
    result, info = _run_coro(mobile_questionnaire.fill_questionnaire(acc, "synthetic-vacancy"))
    bridge.assert_awaited_once_with(acc)
    submit.assert_not_called()
    assert info["phase"] == "oauth_bridge" and info["dispatched"] is False
    encoded = json.dumps(info, ensure_ascii=False)
    for forbidden in ("synthetic-secret", "https://", "synthetic-password", "proxy.test",
                      "synthetic-token", "captcha_url", "hhtoken", "_xsrf"):
        assert forbidden not in encoded
    return result, info


@pytest.mark.parametrize("error_type,reason", [
    (requests.exceptions.ConnectTimeout, "connect_timeout"),
    (requests.exceptions.ReadTimeout, "read_timeout"),
    (requests.exceptions.ProxyError, "proxy_error"),
    (requests.exceptions.SSLError, "tls_error"),
    (requests.exceptions.ConnectionError, "connection_error"),
    (requests.exceptions.Timeout, "timeout"),
    (TimeoutError, "timeout"),
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_bridge_transport_failure_is_not_expired_auth_or_test(monkeypatch, error_type, reason, wrapped):
    error = error_type(SENSITIVE)
    if wrapped:
        wrapper = mobile_auth.MobileAuthError(SENSITIVE)
        wrapper.__cause__ = error
        error = wrapper
    result, info = bridge_failure(monkeypatch, error)
    assert result == "error"
    assert info["error_type"] == "oauth_bridge_network" and info["reason"] == reason
    assert "status_code" not in info


@pytest.mark.parametrize("status,payload,expected,reason", [
    (401, {}, "auth_error", "auth"),
    (403, {}, "challenge", "access_denied"),
    (403, {"errors": [{"value": "invalid_token"}]}, "auth_error", "auth"),
    (403, {"errors": [{"value": "insufficient_scope"}]}, "challenge", "access_denied"),
    (403, {"errors": [{"type": "captcha_required", "captcha_url": "https://hh.ru/captcha?secret=synthetic-token"}]}, "challenge", "challenge"),
    (403, {"errors": [{"type": "challenge_required"}]}, "challenge", "challenge"),
    (429, {"errors": [{"value": "daily_limit"}]}, "rate_limit", "rate_limit"),
    (429, {"errors": [{"type": "captcha_required"}]}, "rate_limit", "rate_limit"),
    (500, {}, "error", "bridge_unavailable"),
    (400, {"errors": [{"value": "invalid_grant"}]}, "auth_error", "auth"),
    (400, {"errors": [{"value": "confirmation_code_expired"}]}, "error", "bridge_unavailable"),
])
def test_explicit_http_bridge_classification_does_not_confuse_limits_or_permissions(monkeypatch, status, payload, expected, reason):
    response = SimpleNamespace(status_code=status, headers={}, json=lambda: payload)
    error = mobile_auth._safe_error(response)
    result, info = bridge_failure(monkeypatch, error)
    assert result == expected and info["reason"] == reason
    assert info["status_code"] == status
    assert result != "limit"  # Request throttling is not the application quota.


@pytest.mark.parametrize("retry,expected", [
    ("120", 120), ("9999999999", 86400), ("0", None), ("-1", None),
    ("invalid", None), ("1" * 40, None), (None, None),
])
def test_rate_retry_after_is_bounded_and_optional(monkeypatch, retry, expected):
    response = SimpleNamespace(status_code=429, headers={"Retry-After": retry}, json=lambda: {})
    result, info = bridge_failure(monkeypatch, mobile_auth._safe_error(response))
    assert result == "rate_limit"
    assert info.get("retry_after_seconds") == expected


@pytest.mark.parametrize("payload", [
    None, [], {"errors": "invalid_token"}, {"errors": {"type": "invalid_token"}},
    {"errors": [{"type": {"nested": "captcha"}, "value": ["invalid_token"]}]},
    {"description": "invalid_token"},
])
def test_arbitrary_body_text_and_malformed_errors_do_not_prove_expiry(monkeypatch, payload):
    response = SimpleNamespace(status_code=403, headers={}, json=lambda: payload)
    result, info = bridge_failure(monkeypatch, mobile_auth._safe_error(response))
    assert result == "challenge" and info["reason"] == "access_denied"


def test_unknown_exception_does_not_infer_auth_from_its_text(monkeypatch):
    result, info = bridge_failure(monkeypatch, RuntimeError("invalid_token expired " + SENSITIVE))
    assert result == "error" and info["reason"] == "bridge_unavailable"


def test_absent_token_is_unavailable_not_proven_expired(monkeypatch):
    obtain = Mock(return_value="")
    client = Mock(side_effect=AssertionError("No bridge request without a token"))
    submit = AsyncMock(side_effect=AssertionError("No form without a bridge"))
    monkeypatch.setattr(oauth, "_obtain_oauth_token", obtain)
    monkeypatch.setattr(mobile_auth, "HHMobileClient", client)
    monkeypatch.setattr(hh_apply, "fill_and_submit_questionnaire", submit)
    result, info = _run_coro(mobile_questionnaire.fill_questionnaire({**ACC, "mode": "oauth"}, "v"))
    assert result == "error" and info["reason"] == "token_unavailable"
    assert info["dispatched"] is False and "status_code" not in info
    obtain.assert_called_once()
    client.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize("phase", ["api", "web"])
def test_native_bridge_preserves_typed_network_cause_without_retry(monkeypatch, phase):
    monkeypatch.setattr("app.hh_http.egress_proxies", lambda: {})
    session = Mock()
    client = mobile_auth.HHMobileClient.__new__(mobile_auth.HHMobileClient)
    client.config = SimpleNamespace(user_agent="synthetic", base_url="https://api.hh.ru")
    client.session = session
    if phase == "api":
        session.request.side_effect = requests.exceptions.ConnectTimeout(SENSITIVE)
    else:
        session.request.return_value = SimpleNamespace(
            status_code=200, content=b"{}", json=lambda: {"key": "synthetic-key"})
        session.get.side_effect = requests.exceptions.ReadTimeout(SENSITIVE)
    with pytest.raises(mobile_auth.MobileAuthError) as caught:
        client.create_browser_cookies("synthetic-token", {"id": "synthetic-owner"})
    assert caught.value.error_kind == "network" and caught.value.status_code == 0
    result, info = bridge_failure(monkeypatch, caught.value)
    assert result == "error"
    assert info["reason"] == ("connect_timeout" if phase == "api" else "read_timeout")
    assert session.request.call_count == 1
    assert session.get.call_count == int(phase == "web")


def test_bridge_task_cancellation_does_not_turn_into_auth_error(monkeypatch):
    monkeypatch.setattr(mobile_questionnaire, "oauth_web_account", AsyncMock(side_effect=asyncio.CancelledError()))
    submit = AsyncMock()
    monkeypatch.setattr(hh_apply, "fill_and_submit_questionnaire", submit)
    with pytest.raises(asyncio.CancelledError):
        _run_coro(mobile_questionnaire.fill_questionnaire({**ACC, "mode": "oauth"}, "v"))
    submit.assert_not_called()
