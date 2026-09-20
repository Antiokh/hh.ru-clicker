"""Bounded verification of cached OAuth and its WebView bridge, never a send.

The temporary browser cookies are discarded. No token refresh, account edits,
application POSTs, external redirects, or proxy fallback are allowed here.
"""
import time
from urllib.parse import urlsplit

import requests

from app.apply_confirmation import _binding, _cached_token
from app.hh_http import PinnedEgressSession
from app.mobile_auth import HHMobileClient
from app.mobile_questionnaire import classify_oauth_bridge_error


_MESSAGES = {
    "network": "Не удалось соединиться с HH. Это не подтверждает истечение авторизации; пауза сохранена.",
    "auth": "HH не подтвердил текущий вход. Требуется восстановить авторизацию.",
    "challenge": "HH ограничил доступ к веб-анкете. Проверьте вход и проверки безопасности на сайте HH.",
    "rate_limit": "HH временно ограничил запросы. Дождитесь снятия ограничения, не повторяйте проверку сейчас.",
    "unavailable": "Не удалось проверить вход и доступ к веб-анкете. Пауза сохранена.",
    "stale": "Данные входа изменились во время проверки. Обновите карточку и проверьте снова.",
}


def _failure(reason):
    reason = reason if isinstance(reason, str) and reason in _MESSAGES else "unavailable"
    return {"verified": False, "reason": reason, "message": _MESSAGES[reason]}


class _VerificationSession(PinnedEgressSession):
    """One fixed HH-only GET budget shared by /me and all bridge redirects."""
    def __init__(self):
        super().__init__()
        self._deadline = time.monotonic() + 35
        self._reads = 0

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (method.upper() != "GET" or parsed.scheme != "https"
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)
                or not (host == "hh.ru" or host.endswith(".hh.ru")
                        or host == "hh.kz" or host.endswith(".hh.kz"))):
            raise ValueError("Verification only permits HH GET requests")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0 or self._reads >= 12:
            raise requests.ReadTimeout("Verification read budget exhausted")
        self._reads += 1
        kwargs.update(allow_redirects=False, stream=True,
                      timeout=(min(5, remaining), min(8, remaining)))
        response = super().request(method, url, **kwargs)
        try:
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                if time.monotonic() >= self._deadline:
                    raise requests.ReadTimeout("Verification read budget exhausted")
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise ValueError("Verification response exceeds size limit")
                chunks.append(chunk)
            response._content = b"".join(chunks)
            response._content_consumed = True
            return response
        finally:
            response.close()


def verify_oauth_and_web_access(acc):
    """Return safe proof only after exact owner and fresh web cookies verified."""
    try:
        if not isinstance(acc, dict) or not str(acc.get("user_id") or ""):
            return _failure("unavailable")
        identity = _binding(acc)
        token = _cached_token(acc)
        # Absence of usable cached credentials is not proof of server rejection.
        if not token:
            return _failure("unavailable")

        def current():
            return _binding(acc) == identity and _cached_token(acc) == token

        with _VerificationSession() as session:
            client = HHMobileClient(session=session)
            me = client._request("GET", "me", token=token)
            if not current():
                return _failure("stale")
            if not isinstance(me, dict) or str(me.get("id") or "") != str(acc["user_id"]):
                return _failure("auth")
            cookies = client.create_browser_cookies(token, me)
            if not current():
                return _failure("stale")
            if not isinstance(cookies, dict) or not all(
                    isinstance(cookies.get(key), str) and cookies[key]
                    for key in ("hhtoken", "_xsrf")):
                return _failure("unavailable")
        return {"verified": True}
    except Exception as exc:
        result, info = classify_oauth_bridge_error(exc)
        reason = {"auth_error": "auth", "challenge": "challenge",
                  "rate_limit": "rate_limit"}.get(result)
        if reason is None:
            reason = "network" if info.get("error_type") == "oauth_bridge_network" else "unavailable"
        failure = _failure(reason)
        retry_after = info.get("retry_after_seconds")
        if type(retry_after) is int and 1 <= retry_after <= 86400:
            failure["retry_after_seconds"] = retry_after
        return failure
