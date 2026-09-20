"""Synthetic GET-only OAuth/WebView verification; never contacts HH."""
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app import auth_verification as verification
from app import oauth, hh_http
from app.mobile_auth import MobileAuthError


PROXIES = {"http": "socks5h://synthetic-proxy.test:1080",
           "https": "socks5h://synthetic-proxy.test:1080"}
SECRET = "synthetic-secret https://user:password@proxy.test/private?token=secret"


@pytest.fixture
def probe(monkeypatch):
    acc = {"name": "synthetic", "short": "S", "user_id": "owner-a",
           "resume_hash": "resume-a", "cookies": {"hhtoken": "old-synthetic-cookie"}}
    record = {"access_token": "synthetic-token", "refresh_token": "synthetic-refresh",
              "expires_at": time.time() + 3600, "source": "mobile_otp", "mobile_user_id": "owner-a"}
    monkeypatch.setattr(oauth, "_oauth_tokens", {oauth._token_key(acc): record})
    monkeypatch.setattr(oauth, "_obtain_oauth_token", Mock(side_effect=AssertionError("No refresh/authorize")))
    monkeypatch.setattr(oauth, "_save_oauth_tokens", Mock(side_effect=AssertionError("No persistence")))
    monkeypatch.setattr(hh_http, "_PROXY", PROXIES["https"])
    client = SimpleNamespace(
        _request=Mock(return_value={"id": "owner-a", "synthetic-private-field": SECRET}),
        create_browser_cookies=Mock(return_value={"hhtoken": "new-synthetic-cookie", "_xsrf": "new-synthetic-csrf"}),
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(verification, "HHMobileClient", factory)
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("Unmocked transport")))
    return acc, record, client, factory


def verify(acc):
    result = verification.verify_oauth_and_web_access(acc)
    assert set(result) <= {"verified", "reason", "message", "retry_after_seconds"}
    serialized = json.dumps(result, ensure_ascii=False)
    for forbidden in ("synthetic-", "https://", "owner-a", "resume-a", "hhtoken", "_xsrf", "password"):
        assert forbidden not in serialized
    return result


def test_success_proves_owner_and_bridge_without_persistence_or_refresh(probe):
    acc, _, client, factory = probe
    original, tokens = copy.deepcopy(acc), copy.deepcopy(oauth._oauth_tokens)
    assert verify(acc) == {"verified": True}
    client._request.assert_called_once_with("GET", "me", token="synthetic-token")
    client.create_browser_cookies.assert_called_once_with("synthetic-token", client._request.return_value)
    assert factory.call_args.kwargs["session"].trust_env is False
    assert acc == original and oauth._oauth_tokens == tokens
    oauth._obtain_oauth_token.assert_not_called()
    oauth._save_oauth_tokens.assert_not_called()
    requests.Session.request.assert_not_called()


def test_explicit_direct_proof_is_supported_without_environment_proxy(probe, monkeypatch):
    acc, _, _, factory = probe
    monkeypatch.setattr(hh_http, "_PROXY", "")
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-environment.test:3128")
    assert verify(acc) == {"verified": True}
    session = factory.call_args.kwargs["session"]
    assert session._egress_snapshot == {} and session.trust_env is False


@pytest.mark.parametrize("kind", ["invalid_account", "missing_owner", "missing_token", "expired_token", "broken_proxy_config"])
def test_preconditions_do_not_attempt_api_or_authentication(probe, monkeypatch, kind):
    acc, record, _, factory = probe
    if kind == "invalid_account":
        acc = None
    elif kind == "missing_owner":
        acc.pop("user_id")
    elif kind == "missing_token":
        oauth._oauth_tokens.clear()
    elif kind == "expired_token":
        record["expires_at"] = time.time() - 1
    else:
        monkeypatch.setattr(hh_http, "egress_proxy", Mock(side_effect=RuntimeError("synthetic config failure")))
    assert verify(acc)["reason"] == "unavailable"
    factory.assert_not_called()
    oauth._obtain_oauth_token.assert_not_called()


@pytest.mark.parametrize("me", [{"id": "different-owner"}, {}, None, []])
def test_missing_or_mismatched_owner_never_opens_web_bridge(probe, me):
    acc, _, client, _ = probe
    client._request.return_value = me
    assert verify(acc)["reason"] == "auth"
    client.create_browser_cookies.assert_not_called()


@pytest.mark.parametrize("stage", ["me", "bridge"])
@pytest.mark.parametrize("change", ["owner", "resume", "cookie", "token"])
def test_identity_or_token_change_rejects_stale_proof(probe, stage, change):
    acc, record, client, _ = probe
    target = client._request if stage == "me" else client.create_browser_cookies
    def changing(*args, **kwargs):
        if change == "owner":
            acc["user_id"] = "other-owner"
        elif change == "resume":
            acc["resume_hash"] = "other-resume"
        elif change == "cookie":
            acc["cookies"]["hhtoken"] = "changed-cookie"
        else:
            record["access_token"] = "changed-token"
        return target.return_value
    target.side_effect = changing
    assert verify(acc)["reason"] == "stale"
    if stage == "me":
        client.create_browser_cookies.assert_not_called()


@pytest.mark.parametrize("cookies", [None, {}, {"hhtoken": "x"}, {"hhtoken": "x", "_xsrf": ""}, {"hhtoken": True, "_xsrf": "x"}])
def test_incomplete_or_malformed_ephemeral_cookies_do_not_confirm(probe, cookies):
    acc, _, client, _ = probe
    client.create_browser_cookies.return_value = cookies
    assert verify(acc)["reason"] == "unavailable"


@pytest.mark.parametrize("error,reason", [
    (requests.ConnectTimeout(SECRET), "network"),
    (requests.ReadTimeout(SECRET), "network"),
    (requests.exceptions.ProxyError(SECRET), "network"),
    (MobileAuthError(SECRET, status_code=401), "auth"),
    (MobileAuthError(SECRET, status_code=403), "challenge"),
    (MobileAuthError(SECRET, status_code=429, retry_after=120), "rate_limit"),
    (RuntimeError(SECRET), "unavailable"),
])
@pytest.mark.parametrize("stage", ["me", "bridge"])
def test_verification_errors_are_safe_and_never_retried(probe, error, reason, stage):
    acc, _, client, _ = probe
    target = client._request if stage == "me" else client.create_browser_cookies
    target.side_effect = error
    result = verify(acc)
    assert result["verified"] is False and result["reason"] == reason
    assert target.call_count == 1
    if reason == "rate_limit":
        assert result["retry_after_seconds"] == 120
    if stage == "me":
        client.create_browser_cookies.assert_not_called()
    oauth._obtain_oauth_token.assert_not_called()


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(hh_http, "_PROXY", PROXIES["https"])
    response = Mock()
    response.iter_content = Mock(side_effect=lambda _: iter([b"synthetic body"]))
    request = Mock(return_value=response)
    monkeypatch.setattr(requests.Session, "request", request)
    return request, response


@pytest.mark.parametrize("method,url", [
    ("POST", "https://api.hh.ru/me"), ("PUT", "https://hh.ru/"),
    ("GET", "http://api.hh.ru/me"), ("GET", "https://evil.test/"),
    ("GET", "https://hh.ru.evil.test/"), ("GET", "https://user:password@hh.ru/"),
    ("GET", "https://hh.ru:444/"),
])
def test_transport_rejects_mutations_credentials_and_non_hh_targets(transport, method, url):
    request, _ = transport
    with verification._VerificationSession() as session:
        with pytest.raises(ValueError):
            session.request(method, url)
    request.assert_not_called()


def test_transport_forces_nonredirect_stream_and_finite_read_budget(transport):
    request, response = transport
    with verification._VerificationSession() as session:
        result = session.get("https://api.hh.ru/me", allow_redirects=True, timeout=None, stream=False)
        assert result._content == b"synthetic body" and result._content_consumed is True
        assert session.trust_env is False
        kwargs = request.call_args.kwargs
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert 0 < kwargs["timeout"][0] <= 5 and 0 < kwargs["timeout"][1] <= 8
    response.close.assert_called_once()


@pytest.mark.parametrize("proxies", [None, {}, False])
def test_transport_invalid_proxy_config_fails_before_any_get(transport, monkeypatch, proxies):
    request, _ = transport
    monkeypatch.setattr(hh_http, "_PROXY", proxies)
    with pytest.raises(ValueError):
        verification._VerificationSession()
    request.assert_not_called()


@pytest.mark.parametrize("changed", ["", "http://changed.test:9"])
def test_transport_pins_proxy_despite_runtime_changes_and_per_request_override(transport, monkeypatch, changed):
    request, _ = transport
    runtime = {"value": PROXIES["https"]}
    monkeypatch.setattr(hh_http, "egress_proxy", lambda: runtime["value"])
    with verification._VerificationSession() as session:
        runtime["value"] = changed
        session.proxies.clear()
        session.get("https://api.hh.ru/me", proxies={"http": None, "https": None})
        first = request.call_args.kwargs["proxies"]
        assert first == PROXIES
        first.clear()  # Neither caller nor transport may mutate the snapshot.
        session.get("https://hh.ru/", proxies={"https": changed})
        assert request.call_args.kwargs["proxies"] == PROXIES
    assert request.call_count == 2


def test_transport_never_exceeds_twelve_gets(transport):
    request, _ = transport
    with verification._VerificationSession() as session:
        for _ in range(12):
            session.get("https://hh.ru/")
        with pytest.raises(requests.ReadTimeout):
            session.get("https://hh.ru/")
    assert request.call_count == 12


def test_transport_deadline_stops_before_another_get(transport, monkeypatch):
    request, _ = transport
    clock = SimpleNamespace(monotonic=lambda: 100)
    monkeypatch.setattr(verification, "time", clock)
    with verification._VerificationSession() as session:
        clock.monotonic = lambda: 136
        with pytest.raises(requests.ReadTimeout):
            session.get("https://hh.ru/")
    request.assert_not_called()


def test_transport_oversize_response_closed_and_not_published(transport):
    _, response = transport
    response.iter_content.side_effect = lambda _: iter([b"x" * (2 * 1024 * 1024 + 1)])
    with verification._VerificationSession() as session:
        with pytest.raises(ValueError):
            session.get("https://hh.ru/")
    response.close.assert_called_once()
    assert "_content" not in vars(response)


def test_transport_partial_read_timeout_closed_and_not_published(transport):
    _, response = transport
    def chunks(_):
        yield b"synthetic partial"
        raise requests.ReadTimeout(SECRET)
    response.iter_content.side_effect = chunks
    with verification._VerificationSession() as session:
        with pytest.raises(requests.ReadTimeout):
            session.get("https://hh.ru/")
    response.close.assert_called_once()
    assert "_content" not in vars(response)
