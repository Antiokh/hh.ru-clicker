"""Mobile-версия заполнения анкеты при отклике (Phase 3).

Решение Phase 3 (факт из разбора APK ru.hh.android v26.28.1, декомпилят
/tmp/hh-apk/src2/sources): нативного Retrofit-endpoint'а для анкет/опросов
в mobile-приложении НЕТ (grep по @u9-аннотациям с
test/questionnaire/survey/answer нашёл только /survey_user_targeting/
banner_info и /contests/* — не то). При error_type=test_required приложение
показывает alert и открывает WEB-страницу анкеты (applicant/vacancy_response)
в webview. То есть официальное mobile-приложение заполняет анкеты через
web-flow — повторяем эту семантику: делегируем в
hh_apply.fill_and_submit_questionnaire. Cookies hh.ru в acc те же, что
использует web-flow; FallbackHHClient для web-аккаунтов и так ходит в эту
же функцию, так что поведение mobile и web совпадает.
"""

import asyncio

import requests

from app import hh_apply
from app.mobile_auth import MobileAuthError


def classify_oauth_bridge_error(exc: Exception) -> tuple:
    """Classify a failed bridge before any form POST, without exposing its data.

    A local token lookup can fail due to networking; missing tokens and generic
    exceptions do not prove expired credentials. HTTP 403/429 are protective
    access/rate stops, never the daily application quota.
    """
    chain, seen = [], set()
    current = exc
    for _ in range(8):
        if not isinstance(current, BaseException) or id(current) in seen:
            break
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    typed = next((error for error in chain if isinstance(error, MobileAuthError)), None)
    status = typed.status_code if typed is not None else None
    kind = typed.error_kind if typed is not None else "other"
    category, reason, result = "unavailable", "bridge_unavailable", "error"
    message = "Не удалось подготовить веб-анкету HH. Анкета не отправлена."
    if status == 429 or kind == "rate_limit":
        category = reason = "rate_limit"
        result = "rate_limit"
        message = "HH временно ограничил частоту запросов. Анкета не отправлена."
    elif status == 401 or kind == "auth":
        category = reason = "auth"
        result = "auth_error"
        message = "HH не подтвердил авторизацию для веб-анкеты. Анкета не отправлена."
    elif status == 403 or kind == "challenge":
        category, result = "challenge", "challenge"
        reason = "challenge" if kind == "challenge" else "access_denied"
        message = "HH ограничил доступ к веб-анкете. Требуется проверка доступа; анкета не отправлена."
    else:
        for error in chain:
            for cls, label in (
                (requests.exceptions.ProxyError, "proxy_error"),
                (requests.exceptions.SSLError, "tls_error"),
                (requests.exceptions.ConnectTimeout, "connect_timeout"),
                (requests.exceptions.ReadTimeout, "read_timeout"),
                (requests.exceptions.Timeout, "timeout"),
                (requests.exceptions.ConnectionError, "connection_error"),
                (requests.exceptions.RequestException, "network_error"),
                (TimeoutError, "timeout"),
            ):
                if isinstance(error, cls):
                    category, reason = "network", label
                    break
            if category == "network":
                break
        if category == "network" or kind == "network":
            category = "network"
            if reason == "bridge_unavailable":
                reason = "network_error"
            message = "Не удалось соединиться с HH для подготовки веб-анкеты. Анкета не отправлена."
        elif kind in {"token_unavailable", "invalid_response"}:
            reason = kind
    info = {"error_type": "oauth_bridge_" + category, "reason": reason,
            "phase": "oauth_bridge", "dispatched": False, "message": message}
    if (type(status) is int and 100 <= status <= 599
            and (kind in {"auth", "challenge", "rate_limit", "denied", "http_error"}
                 or status in (401, 403, 429))):
        info["status_code"] = status
    if result == "rate_limit" and typed is not None:
        retry = typed.retry_after
        if type(retry) is int and retry > 0:
            info["retry_after_seconds"] = min(retry, 86400)
    return result, info


def oauth_web_account_sync(acc: dict) -> dict:
    """Build an ephemeral HH web session from the account's OAuth token.

    The Android app uses the same bridge for WebView-only screens.  Cookies are
    kept only in the returned copy and are never persisted in the OAuth account.
    """
    from app.mobile_auth import HHMobileClient
    from app.oauth import _obtain_oauth_token

    token = _obtain_oauth_token(acc)
    if not token:
        raise MobileAuthError("Не удалось получить OAuth-токен для веб-анкеты",
                              status_code=0, error_kind="token_unavailable")
    user_id = str(acc.get("user_id") or "").strip()
    if not user_id:
        counters = HHMobileClient()._request("GET", "me", token=token)
        user_id = str(counters.get("id") or "") if isinstance(counters, dict) else ""
    cookies = HHMobileClient().create_browser_cookies(token, {"id": user_id})
    return {**acc, "cookies": cookies}


async def oauth_web_account(acc: dict) -> dict:
    return await asyncio.to_thread(oauth_web_account_sync, acc)


async def fill_questionnaire(acc: dict, vid: str,
                             vacancy_title: str = "", company: str = "") -> tuple:
    """Заполнить анкету при отклике: делегирование в web-flow.

    Нативного mobile-endpoint'а для анкет нет (см. docstring модуля:
    официальное приложение открывает web-анкету в webview), поэтому
    вызываем hh_apply.fill_and_submit_questionnaire как есть —
    аргументы пробрасываются позиционно, результат возвращается
    без преобразования.

    Возвращает (result, info) web-функции:
    result = sent | limit | test | error | auth_error | challenge | rate_limit.
    """
    web_acc = acc
    if str(acc.get("mode") or "").strip().lower() == "oauth":
        try:
            web_acc = await oauth_web_account(acc)
        except Exception as exc:
            return classify_oauth_bridge_error(exc)
    # The bridge replaces hhtoken, which participates in the OAuth cache key.
    # Keep the original identity explicitly, without storing a nested account
    # reference in a dict that could later be serialized.
    receipt_kw = {"receipt_account": acc} if web_acc is not acc else {}
    return await hh_apply.fill_and_submit_questionnaire(
        web_acc, vid, vacancy_title, company, **receipt_kw)
