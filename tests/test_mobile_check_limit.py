"""An achievement counter must never be used to infer daily quota."""
from unittest.mock import Mock

from app.hh_client_mobile import MobileHHClient
from app.mobile_check_limit import check_limit


def test_mobile_quota_is_unknown_without_an_authoritative_source(monkeypatch):
    request = Mock(side_effect=AssertionError("No streak request needed"))
    monkeypatch.setattr("app.hh_mobile_transport.mobile_request", request)
    assert check_limit({}) == {"applied_today": None, "limit": None, "can_apply": None}
    assert MobileHHClient({}).check_limit() is None
    request.assert_not_called()
