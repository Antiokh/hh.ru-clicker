"""Offline regression for explicit proxy/direct routing and pinned checks."""
import json
import time
from unittest.mock import Mock

import pytest
import requests

from app import hh_http, apply_confirmation as receipt, oauth
from app.hh_mobile_transport import mobile_request


def response(payload=None):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps(payload or {}).encode()
    result._content_consumed = True
    return result


@pytest.fixture
def sent(monkeypatch):
    calls = []
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://synthetic-environment.test:3128")
    def send(session, request, **kwargs):
        calls.append((session, request, kwargs))
        return response()
    monkeypatch.setattr(requests.Session, "send", send)
    return calls


@pytest.mark.parametrize("initial", ["", "http://synthetic-proxy.test:3128"])
@pytest.mark.parametrize("changed", ["", "http://different-proxy.test:3128"])
def test_pinned_session_ignores_env_runtime_and_stale_session_proxies(sent, monkeypatch, initial, changed):
    monkeypatch.setattr(hh_http, "_PROXY", initial)
    with hh_http.PinnedEgressSession() as session:
        monkeypatch.setattr(hh_http, "_PROXY", changed)
        session.proxies = {"all": "http://stale.test:3128", "https://api.hh.ru": "http://stale.test:3128"}
        session.get("https://api.hh.ru/me", proxies={"https": "http://override.test:3128"})
    assert len(sent) == 1 and sent[0][0].trust_env is False
    assert sent[0][2]["proxies"] == ({"http": initial, "https": initial} if initial else {})


def test_pinned_config_read_failure_does_not_make_direct_request(sent, monkeypatch):
    monkeypatch.setattr(hh_http, "egress_proxy", Mock(side_effect=RuntimeError("synthetic config failure")))
    with pytest.raises(RuntimeError):
        hh_http.PinnedEgressSession()
    assert sent == []


def test_shared_hh_direct_session_ignores_ambient_proxies(sent, monkeypatch):
    monkeypatch.setattr(hh_http, "_PROXY", "")
    monkeypatch.setattr(hh_http, "_HAS_CFFI", False)
    client = hh_http.HHClient()
    client.get("https://api.hh.ru/me", cookie_jar_key="synthetic", _force_requests=True)
    assert len(sent) == 1 and sent[0][2]["proxies"] == {}
    assert sent[0][0].trust_env is False


def test_mobile_transport_direct_is_explicit_and_ignores_env(sent, monkeypatch):
    monkeypatch.setattr(hh_http, "_PROXY", "")
    monkeypatch.setattr(oauth, "_obtain_oauth_token", lambda acc: "synthetic-token")
    mobile_request({}, "GET", "/me")
    assert len(sent) == 1 and sent[0][2]["proxies"] == {}
    assert sent[0][0].trust_env is False


@pytest.mark.parametrize("initial", ["", "http://synthetic-proxy.test:3128"])
def test_receipt_list_and_detail_keep_the_same_egress_choice(monkeypatch, initial):
    acc = {"resume_hash": "synthetic-resume", "user_id": "synthetic-owner", "cookies": {}}
    record = {"access_token": "synthetic-token", "expires_at": time.time() + 3600,
              "source": "mobile_otp", "mobile_user_id": "synthetic-owner"}
    monkeypatch.setattr(oauth, "_oauth_tokens", {oauth._token_key(acc): record})
    monkeypatch.setattr(oauth, "_obtain_oauth_token", Mock(side_effect=AssertionError("No auth POST")))
    monkeypatch.setattr(receipt, "mobile_headers", lambda *args: {})
    monkeypatch.setattr(hh_http, "_PROXY", initial)
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.test:3128")
    calls = []
    topic = {"id": "synthetic-topic", "vacancy": {"id": "synthetic-vacancy"},
             "resume": {"id": "synthetic-resume"}, "state": {"id": "response"}}
    def send(session, request, **kwargs):
        calls.append((session, request, kwargs))
        monkeypatch.setattr(hh_http, "_PROXY", "http://changed.test:3128" if not initial else "")
        return response({"items": [{**topic, "resume": None}]} if len(calls) == 1 else topic)
    monkeypatch.setattr(requests.Session, "send", send)
    assert receipt.confirm_application_receipt(acc, "synthetic-vacancy", "synthetic-resume")["receipt_confirmed"] is True
    expected = {"http": initial, "https": initial} if initial else {}
    assert len(calls) == 2 and calls[0][0] is calls[1][0]
    assert all(call[1].method == "GET" and call[2]["proxies"] == expected for call in calls)
    assert calls[0][0].trust_env is False
    oauth._obtain_oauth_token.assert_not_called()


def test_receipt_config_read_failure_has_no_network_fallback(monkeypatch):
    monkeypatch.setattr(receipt, "_cached_token", lambda acc: "synthetic")
    monkeypatch.setattr(receipt, "_binding", lambda acc: ("synthetic", "synthetic", "synthetic-resume"))
    monkeypatch.setattr(hh_http, "egress_proxy", Mock(side_effect=RuntimeError("synthetic config failure")))
    send = Mock(side_effect=AssertionError("No direct fallback"))
    monkeypatch.setattr(requests.Session, "send", send)
    assert receipt.confirm_application_receipt({}, "synthetic-vacancy", "synthetic-resume") is None
    send.assert_not_called()
