"""Fresh, bounded, GET-only evidence for an ambiguous application submission.

No cached negotiation metadata, login, token refresh, redirects, retries, or
message reads. Missing evidence means unknown, never permission to resubmit.
"""
import json
import re
import threading
import time
from datetime import datetime

import requests

from app import oauth
from app.hh_http import PinnedEgressSession
from app.hh_mobile_transport import MOBILE_BASE, mobile_headers
from app.logging_utils import log_debug

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PER_PAGE = 100
_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_receipt_diagnostics = threading.local()


def receipt_check_failure_reason() -> str:
    """Read after an inconclusive check, in the same worker thread.

    Only fixed public reason codes are retained, never response/exception text.
    A new check clears the previous reason even if it fails before making a GET.
    """
    return getattr(_receipt_diagnostics, "reason", "unconfirmed")


def _diagnostic(phase, started, *, status=None, error=None):
    """Fixed phase/class names only: never log URLs, IDs, bodies, or secrets."""
    fields = ["apply_receipt", "phase=" + phase,
              "elapsed_ms=" + str(max(0, int((time.monotonic() - started) * 1000)))]
    if type(status) is int:
        fields.append("http_status=" + str(status))
        if status in (401, 403):
            _receipt_diagnostics.reason = "auth"
        elif status == 429:
            _receipt_diagnostics.reason = "rate_limit"
    if error is not None:
        kind = "other"
        for cls, label in (
            (requests.exceptions.ProxyError, "proxy_error"),
            (requests.exceptions.SSLError, "tls_error"),
            (requests.exceptions.ConnectTimeout, "connect_timeout"),
            (requests.exceptions.ReadTimeout, "read_timeout"),
            (requests.exceptions.Timeout, "timeout"),
            (requests.exceptions.ConnectionError, "connection_error"),
            (requests.exceptions.RequestException, "request_error"),
            (json.JSONDecodeError, "invalid_json"),
            (TimeoutError, "timeout"),
        ):
            if isinstance(error, cls):
                kind = label
                break
        fields.append("error_class=" + kind)
        if kind in ("connect_timeout", "read_timeout"):
            _receipt_diagnostics.reason = kind
    try:
        log_debug(" ".join(fields))
    except Exception:
        pass  # Diagnostic output must never change receipt classification.


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    return value if _ID.fullmatch(value) else None


def _binding(acc):
    return (oauth._token_key(acc), str(acc.get("user_id") or ""),
            str(acc.get("_pinned_resume_id") or acc.get("resume_hash") or ""))


def _cached_token(acc):
    # Do not call _obtain_oauth_token: it may authorize/refresh using a POST.
    with oauth._oauth_lock:
        record = oauth._oauth_tokens.get(oauth._token_key(acc))
        if not record:
            record = oauth._oauth_tokens.get(acc.get("resume_hash", ""))
            if (not isinstance(record, dict) or record.get("source") != "mobile_otp" or
                    not acc.get("user_id") or
                    str(record.get("mobile_user_id") or "") != str(acc["user_id"])):
                return None
        if not isinstance(record, dict):
            return None
        if (record.get("mobile_user_id") and
                str(record["mobile_user_id"]) != str(acc.get("user_id") or "")):
            return None
        token, expires = record.get("access_token"), record.get("expires_at")
        if (not isinstance(token, str) or not token or
                isinstance(expires, bool) or not isinstance(expires, (int, float)) or
                expires <= time.time()):
            return None
        return token


def _get_json(acc, token, path, params=None, *, session):
    # requests' default adapter has no retries. A new request also cannot
    # inherit cookies from an unrelated browser or follow an arbitrary URI.
    started = time.monotonic()
    phase = "list" if path == "/negotiations" else "detail"
    _diagnostic(phase + "_request", started)
    with session.get(
        MOBILE_BASE + path, params=params, headers=mobile_headers(acc, token),
        timeout=(5, 10), allow_redirects=False,
        stream=True,
    ) as response:
        _diagnostic(phase + "_http", started, status=response.status_code)
        if response.status_code != 200:
            return None
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                _diagnostic(phase + "_response_limit", started)
                return None
            chunks.append(chunk)
        _diagnostic(phase + "_read", started)
        value = json.loads(b"".join(chunks))
        return value if isinstance(value, dict) else None


def _receipt(item, vacancy_id, resume_id):
    if not isinstance(item, dict):
        return None
    # This native flag means "a question BEFORE applying", not an unfinished
    # questionnaire. Do not count a pre-application question as an application,
    # or accept strings, null, zero, or unfamiliar representations as false.
    if "applicant_question_state" in item and item["applicant_question_state"] is not False:
        return None
    vacancy, resume, state = item.get("vacancy"), item.get("resume"), item.get("state")
    if not all(isinstance(value, dict) for value in (vacancy, resume, state)):
        return None
    topic_id = _identifier(item.get("id"))
    if (not topic_id or _identifier(vacancy.get("id")) != vacancy_id or
            _identifier(resume.get("id")) != resume_id or state.get("id") != "response"):
        return None
    receipt = {"negotiation_id": topic_id, "receipt_confirmed": True}
    # Accounting can distinguish a fresh receipt from an older application.
    # Never infer today's date when HH omitted or malformed the timestamp.
    created = item.get("created_at")
    if isinstance(created, str) and len(created) <= 64:
        try:
            normalized = re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", created.replace("Z", "+00:00"))
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is not None:
                receipt["receipt_created_at"] = parsed.isoformat()
        except ValueError:
            pass
    return receipt


def confirm_application_receipt(acc: dict, vacancy_id: str, resume_id: str) -> dict | None:
    """One vacancy-filtered list GET and at most one exact-topic GET.

    Receipt requires the authenticated account, exact selected resume/vacancy,
    a topic ID, and an application state (not an unsolicited invitation).
    Reads remain allowed while paused so an unknown result can be reconciled.
    """
    _receipt_diagnostics.reason = "unconfirmed"
    session = None
    started = time.monotonic()
    phase = "validate"
    _diagnostic("start", started)
    try:
        vacancy_id, resume_id = _identifier(vacancy_id), _identifier(resume_id)
        if not isinstance(acc, dict) or not vacancy_id or not resume_id:
            return None
        identity = _binding(acc)
        if identity[2] != resume_id:
            return None
        phase = "cached_token"
        _diagnostic(phase, started)
        token = _cached_token(acc)
        if token is None:
            _diagnostic("no_cached_token", started)
            return None
        _diagnostic("token_ready", started)
        session = PinnedEgressSession()

        def current():
            return _binding(acc) == identity and _cached_token(acc) == token

        phase = "list_get"
        # Native NegotiationApi has an explicit GET /negotiations?vacancy_id
        # lookup. A global page 0 can lose older receipts as other chats update.
        # Do not filter by status: validate the returned topic locally below.
        data = _get_json(acc, token, "/negotiations",
                         {"vacancy_id": vacancy_id, "page": 0, "per_page": PER_PAGE}, session=session)
        phase = "list_identity_check"
        if not current() or not isinstance(data, dict):
            return None
        items = data.get("items")
        if not isinstance(items, list) or len(items) > PER_PAGE:
            return None
        candidates = [item for item in items if isinstance(item, dict)
                      and isinstance(item.get("vacancy"), dict)
                      and _identifier(item["vacancy"].get("id")) == vacancy_id]
        if not candidates:
            _diagnostic("no_matching_vacancy", started)
            return None
        for item in candidates:
            receipt = _receipt(item, vacancy_id, resume_id)
            if receipt is not None:
                if current():
                    _diagnostic("confirmed_list", started)
                    return receipt
                return None
        # A missing resume in a list projection is not a wildcard. Resolve at
        # most one exact candidate; do not walk history or try every topic.
        unresolved = [item for item in candidates if item.get("resume") is None
                      and ("applicant_question_state" not in item or
                           item["applicant_question_state"] is False)]
        if len(unresolved) != 1:
            _diagnostic("matching_topic_unconfirmed", started)
            return None
        topic_id = _identifier(unresolved[0].get("id"))
        if not topic_id or not current():
            return None
        phase = "detail_get"
        detail = _get_json(acc, token, "/negotiations/" + topic_id, session=session)
        phase = "detail_identity_check"
        if not current() or not isinstance(detail, dict) or _identifier(detail.get("id")) != topic_id:
            return None
        receipt = _receipt(detail, vacancy_id, resume_id)
        if receipt is not None:
            _diagnostic("confirmed_detail", started)
        return receipt
    except Exception as exc:
        # Transport/parser exceptions may contain credentials or response text.
        # Expose neither, and never convert a failed read into a resend.
        _diagnostic(phase + "_failed", started, error=exc)
        return None
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                _diagnostic("close_failed", started)
        _diagnostic("end", started)
