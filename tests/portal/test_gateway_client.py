"""GatewayClient unit tests: spend-limit body building, token-exp/cookie
budget, and the effective-usage request builder (param names + auth header
forms, binary-verified contract)."""

import json
import time
import urllib.parse

import pytest

from portal.crypto import b64url_encode, verify_cookie
from portal.gateway import (GW_COOKIE_BUDGET, SPEND_PERIODS, GatewayClient,
                            build_gw_cookie, build_spend_limit_body,
                            gateway_token_exp)
from portal.money import AmountError
from portal.selection import SelectionError

from conftest import TEST_ENV
from portal.config import Config


# ------------------------------------------------------------- body builder


def test_spend_body_user():
    body = build_spend_limit_body("user", "00u123", "50.00", "monthly")
    assert body == {"scope": {"type": "user", "user_id": "00u123"},
                    "amount": "5000", "period": "monthly", "currency": "USD"}


def test_spend_body_group_and_org():
    body = build_spend_limit_body("rbac_group", "claude-developers", "2500", "weekly")
    assert body["scope"] == {"type": "rbac_group", "rbac_group_id": "claude-developers"}
    org = build_spend_limit_body("organization", "", "10000", "monthly")
    assert org["scope"] == {"type": "organization"}


def test_spend_body_clear_is_null_amount():
    body = build_spend_limit_body("user", "00u123", None, "monthly")
    assert body["amount"] is None


@pytest.mark.parametrize("scope_type,scope_id,period", [
    ("bogus", "x", "monthly"),          # unknown scope
    ("user", "", "monthly"),            # user cap without an id
    ("organization", "oops", "monthly"),  # org cap must not carry an id
    ("user", "x", "hourly"),            # unknown period
    ("user", "a\x00b", "monthly"),      # control chars in the id
])
def test_spend_body_rejects(scope_type, scope_id, period):
    with pytest.raises(SelectionError):
        build_spend_limit_body(scope_type, scope_id, "5", period)


def test_spend_body_bad_amount_raises_amount_error():
    with pytest.raises(AmountError):
        build_spend_limit_body("user", "00u123", "1.2.3", "monthly")


def test_spend_periods_are_the_gateway_triple():
    assert SPEND_PERIODS == ("daily", "weekly", "monthly")


# ------------------------------------------------------------- token exp


def test_gateway_token_exp_prefers_jwt_claim():
    now = int(time.time())
    payload = b64url_encode(json.dumps({"exp": now + 1234}).encode())
    tok = "h." + payload + ".sig"
    assert gateway_token_exp({"access_token": tok}, now=now) == now + 1234


def test_gateway_token_exp_falls_back_to_expires_in_then_default():
    now = int(time.time())
    assert gateway_token_exp({"access_token": "opaque", "expires_in": 300}, now=now) == now + 300
    assert gateway_token_exp({"access_token": "opaque"}, now=now) == now + 900


# ------------------------------------------------------------- cookie budget


def test_oversized_gateway_token_drops_refresh_then_errors():
    now = int(time.time())
    session = {"email": "a@x", "groups": [], "exp": now + 3600}
    small = {"access_token": "t" * 100, "refresh_token": "r" * 3000, "expires_in": 600}
    cookie, ttl = build_gw_cookie(small, session, "secret", now=now)
    assert cookie is not None and len(cookie) <= GW_COOKIE_BUDGET
    payload = verify_cookie(cookie, "secret", now=now)
    assert payload["rt"] == ""                         # refresh token dropped to fit
    huge = {"access_token": "t" * 5000, "expires_in": 600}
    cookie, ttl = build_gw_cookie(huge, session, "secret", now=now)
    assert cookie is None and ttl == 0                 # explicit failure, not a silent drop


def test_gw_cookie_never_outlives_the_portal_session():
    now = int(time.time())
    session = {"email": "a@x", "groups": [], "exp": now + 60}   # session ends first
    tok = {"access_token": "t" * 100, "expires_in": 3600}
    cookie, ttl = build_gw_cookie(tok, session, "secret", now=now)
    assert verify_cookie(cookie, "secret", now=now)["exp"] == now + 60
    assert ttl == 60


# ------------------------------------------------------------- effective_usage


class RecordingGateway(GatewayClient):
    """Real request-building code; _http captures instead of dialing."""

    def __init__(self, config):
        super().__init__(config)
        self.calls = []

    def _http(self, method, url, headers=None, body=None):
        self.calls.append((method, url, headers, body))
        return 200, {"data": [], "next_page": None}


@pytest.fixture
def gw():
    return RecordingGateway(Config(dict(TEST_ENV)))


def _query(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query,
                                 keep_blank_values=True)


def test_effective_usage_bearer_auth_header(gw):
    status, doc = gw.effective_usage(("bearer", "tok-123"))
    assert status == 200
    method, url, headers, body = gw.calls[0]
    assert method == "GET" and body is None
    assert headers == {"Authorization": "Bearer tok-123"}
    assert url == gw.base + "/v1/organizations/spend_limits/effective"


def test_effective_usage_api_key_auth_header(gw):
    gw.effective_usage(("api_key", "read-key"))
    _, _, headers, _ = gw.calls[0]
    assert headers == {"x-api-key": "read-key"}


def test_effective_usage_rejects_unknown_auth_kind(gw):
    with pytest.raises(ValueError):
        gw.effective_usage(("cookie", "nope"))


def test_effective_usage_builds_literal_bracket_params(gw):
    """The gateway's param names are literally 'period[]' / 'user_ids[]'
    (repeatable), binary-verified."""
    gw.effective_usage(("api_key", "k"), periods=["daily", "monthly"],
                       user_ids=["00u1", "00u2"], q="smith", page="tok==x",
                       limit=50)
    _, url, _, _ = gw.calls[0]
    q = _query(url)
    assert q["period[]"] == ["daily", "monthly"]
    assert q["user_ids[]"] == ["00u1", "00u2"]
    assert q["q"] == ["smith"]
    assert q["page"] == ["tok==x"]      # opaque token survives URL-encoding
    assert q["limit"] == ["50"]
    assert "sort" not in q


def test_effective_usage_sort_param(gw):
    gw.effective_usage(("bearer", "t"), periods=["monthly"], sort="spend_desc")
    _, url, _, _ = gw.calls[0]
    q = _query(url)
    assert q["sort"] == ["spend_desc"]
    assert q["period[]"] == ["monthly"]


def test_effective_usage_no_params_means_bare_url(gw):
    gw.effective_usage(("bearer", "t"))
    _, url, _, _ = gw.calls[0]
    assert "?" not in url
