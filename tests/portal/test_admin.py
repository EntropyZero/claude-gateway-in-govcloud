"""The spend-cap admin section: money conversion, spend-limit body building,
device-flow session handling, group gating (portal AND gateway layers), and
the proxied list/set/clear/audit calls - all against a stub gateway, no
network. The design under test: the portal holds NO gateway admin key; every
call rides the signed-in admin's own gateway token."""

import io
import json
import time

import pytest

from conftest import StubOidc, make_handler, parse_response, cookie_value

GROUP = "claude-gateway-users"
ADMIN_GROUP = "claude-spend-admins"


# ------------------------------------------------------------- fixtures


@pytest.fixture
def admin_config(app, env):
    env["PORTAL_ADMIN_GROUP"] = ADMIN_GROUP
    return app.Config(env)


class StubGateway:
    """Canned gateway. Records every spend_api call; device flow and refresh
    are scripted per test."""

    def __init__(self, device=None, poll_results=None, api=None, refresh_resp=None):
        self.device = device or {
            "device_code": "dc-123", "user_code": "WDJB-MJHT",
            "verification_uri": "https://claude-gateway.example.com/oauth/device",
            "verification_uri_complete": "https://claude-gateway.example.com/oauth/device?code=WDJB-MJHT",
            "expires_in": 600, "interval": 5,
        }
        self.poll_results = list(poll_results or [])
        self.api = dict(api or {})          # (method, path) -> (status, doc)
        self.refresh_resp = refresh_resp
        self.api_calls = []                 # (method, token, path, body)
        self.polled = []
        self.refreshed = []

    def device_authorize(self):
        if isinstance(self.device, Exception):
            raise self.device
        return self.device

    def poll_token(self, device_code):
        self.polled.append(device_code)
        result = self.poll_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def refresh(self, refresh_token):
        self.refreshed.append(refresh_token)
        return self.refresh_resp

    def spend_api(self, method, token, path="", body=None):
        self.api_calls.append((method, token, path, body))
        return self.api.get((method, path.partition("?")[0]), (200, {"data": []}))


def _session_payload(email="admin@example.com", groups=None, ttl=3600):
    return {"email": email,
            "groups": groups if groups is not None else [GROUP, ADMIN_GROUP],
            "exp": int(time.time()) + ttl}


def _session_cookie(app, config, email="admin@example.com", groups=None, ttl=3600):
    return app.sign_cookie(_session_payload(email, groups, ttl), config.session_secret)


def _admin_session(app, config):
    """(session cookie, csrf token) for one admin session - POSTs need both."""
    payload = _session_payload()
    return (app.sign_cookie(payload, config.session_secret),
            app.csrf_token(payload, config.session_secret))


def _gw_cookie(app, config, tok="gw-token-abc", rt="", ttl=900):
    return app.sign_cookie(
        {"tok": tok, "rt": rt, "exp": int(time.time()) + ttl}, config.session_secret)


def _gwdev_cookie(app, config, dc="dc-123", ttl=600):
    return app.sign_cookie(
        {"dc": dc, "uc": "WDJB-MJHT", "vu": "https://gw/verify", "int": 5,
         "exp": int(time.time()) + ttl}, config.session_secret)


def run_get(app, config, audit, path, *, gateway=None, cookies=None):
    h = make_handler(app, config, StubOidc(config, {"keys": []}), audit,
                     cookies=cookies, gateway=gateway)
    h.path = path
    h.do_GET()
    return parse_response(h)


def run_post(app, config, audit, path, form=None, *, gateway=None, cookies=None):
    body = "&".join("%s=%s" % (k, v) for k, v in (form or {}).items()).encode()
    h = make_handler(app, config, StubOidc(config, {"keys": []}), audit,
                     cookies=cookies, gateway=gateway,
                     headers={"Content-Length": str(len(body))})
    h.rfile = io.BytesIO(body)
    h.path = path
    h.do_POST()
    return parse_response(h)


# ------------------------------------------------------------- money


def test_dollars_to_cents_exact(app):
    assert app.dollars_to_cents("50") == "5000"
    assert app.dollars_to_cents("50.5") == "5050"
    assert app.dollars_to_cents("0.05") == "5"     # the historical float bug
    assert app.dollars_to_cents("1234.56") == "123456"
    assert app.dollars_to_cents("99999999.99") == "9999999999"


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3", "0.001", "-5", "0", "0.00", "."])
def test_dollars_to_cents_rejects(app, bad):
    with pytest.raises(app.AmountError):
        app.dollars_to_cents(bad)


def test_cents_to_dollars(app):
    assert app.cents_to_dollars("5005") == "$50.05"
    assert app.cents_to_dollars("5") == "$0.05"
    assert app.cents_to_dollars("not-a-number") == "not-a-number"  # defensive


# ------------------------------------------------------------- body builder


def test_spend_body_user(app):
    body = app.build_spend_limit_body("user", "00u123", "50.00", "monthly")
    assert body == {"scope": {"type": "user", "user_id": "00u123"},
                    "amount": "5000", "period": "monthly", "currency": "USD"}


def test_spend_body_group_and_org(app):
    body = app.build_spend_limit_body("rbac_group", "claude-developers", "2500", "weekly")
    assert body["scope"] == {"type": "rbac_group", "rbac_group_id": "claude-developers"}
    org = app.build_spend_limit_body("organization", "", "10000", "monthly")
    assert org["scope"] == {"type": "organization"}


def test_spend_body_clear_is_null_amount(app):
    body = app.build_spend_limit_body("user", "00u123", None, "monthly")
    assert body["amount"] is None


@pytest.mark.parametrize("scope_type,scope_id,period", [
    ("bogus", "x", "monthly"),          # unknown scope
    ("user", "", "monthly"),            # user cap without an id
    ("organization", "oops", "monthly"),  # org cap must not carry an id
    ("user", "x", "hourly"),            # unknown period
    ("user", "a\x00b", "monthly"),      # control chars in the id
])
def test_spend_body_rejects(app, scope_type, scope_id, period):
    with pytest.raises(app.SelectionError):
        app.build_spend_limit_body(scope_type, scope_id, "5", period)


# ------------------------------------------------------------- token exp


def test_gateway_token_exp_prefers_jwt_claim(app):
    now = int(time.time())
    payload = app.b64url_encode(json.dumps({"exp": now + 1234}).encode())
    tok = "h." + payload + ".sig"
    assert app.gateway_token_exp({"access_token": tok}, now=now) == now + 1234


def test_gateway_token_exp_falls_back_to_expires_in_then_default(app):
    now = int(time.time())
    assert app.gateway_token_exp({"access_token": "opaque", "expires_in": 300}, now=now) == now + 300
    assert app.gateway_token_exp({"access_token": "opaque"}, now=now) == now + 900


# ------------------------------------------------------------- gating


def test_admin_404_when_feature_disabled(app, config, audit):
    # config (no PORTAL_ADMIN_GROUP) -> the path does not exist.
    cookie = _session_cookie(app, config)
    status, _, _, _ = run_get(app, config, audit, "/portal/admin",
                              cookies={"portal_session": cookie})
    assert status == 404


def test_admin_redirects_anonymous_to_login(app, admin_config, audit):
    status, headers, _, _ = run_get(app, admin_config, audit, "/portal/admin")
    assert status == 302 and headers["Location"] == "/portal/login"


def test_admin_denies_non_member_and_audits(app, admin_config, audit):
    cookie = _session_cookie(app, admin_config, groups=[GROUP])  # downloader only
    status, _, _, body = run_get(app, admin_config, audit, "/portal/admin",
                                 cookies={"portal_session": cookie})
    assert status == 403 and b"Access denied" in body
    assert audit.records and audit.records[0]["event"] == "portal_admin"
    assert audit.records[0]["outcome"] == "denied"


def test_index_links_admin_only_for_members(app, admin_config, audit):
    admin = _session_cookie(app, admin_config)
    plain = _session_cookie(app, admin_config, groups=[GROUP])
    _, _, _, admin_body = run_get(app, admin_config, audit, "/portal",
                                  cookies={"portal_session": admin})
    _, _, _, plain_body = run_get(app, admin_config, audit, "/portal",
                                  cookies={"portal_session": plain})
    assert b"/portal/admin" in admin_body
    assert b"/portal/admin" not in plain_body


# ------------------------------------------------------------- device flow


def test_connect_starts_device_flow_and_sets_txn_cookie(app, admin_config, audit):
    gw = StubGateway()
    cookie, csrf = _admin_session(app, admin_config)
    status, headers, set_cookies, _ = run_post(
        app, admin_config, audit, "/portal/admin/connect", {"csrf": csrf},
        gateway=gw, cookies={"portal_session": cookie})
    assert status == 302 and headers["Location"] == "/portal/admin"
    raw = cookie_value(set_cookies, "portal_gwdev")
    txn = app.verify_cookie(raw, admin_config.session_secret)
    assert txn["dc"] == "dc-123" and txn["int"] == 5
    assert "WDJB-MJHT" in txn["uc"]


def test_pending_page_polls_and_shows_verification_link(app, admin_config, audit):
    gw = StubGateway(poll_results=["pending"])
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gwdev": _gwdev_cookie(app, admin_config)}
    status, _, _, body = run_get(app, admin_config, audit, "/portal/admin",
                                 gateway=gw, cookies=cookies)
    assert status == 200
    assert gw.polled == ["dc-123"]
    assert b"http-equiv='refresh'" in body and b"WDJB-MJHT" in body


def test_poll_success_stores_token_and_redirects(app, admin_config, audit):
    now = int(time.time())
    payload = app.b64url_encode(json.dumps({"exp": now + 600}).encode())
    gw = StubGateway(poll_results=[
        {"access_token": "h." + payload + ".s", "refresh_token": "rt-1"}])
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gwdev": _gwdev_cookie(app, admin_config)}
    status, headers, set_cookies, _ = run_get(app, admin_config, audit, "/portal/admin",
                                              gateway=gw, cookies=cookies)
    assert status == 302 and headers["Location"] == "/portal/admin"
    tok = app.verify_cookie(cookie_value(set_cookies, "portal_gw"), admin_config.session_secret)
    assert tok["tok"].startswith("h.") and tok["rt"] == "rt-1"
    assert tok["exp"] <= now + 600      # never outlives the gateway token
    # the device txn cookie is cleared
    assert any(c.startswith("portal_gwdev=;") for c in set_cookies)
    assert any(r["event"] == "portal_admin" and r["outcome"] == "success"
               for r in audit.records)


def test_poll_dead_grant_clears_txn_and_offers_reconnect(app, admin_config, audit):
    gw = StubGateway(poll_results=[app.GatewayError("gateway sign-in expired_token")])
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gwdev": _gwdev_cookie(app, admin_config)}
    status, _, set_cookies, body = run_get(app, admin_config, audit, "/portal/admin",
                                           gateway=gw, cookies=cookies)
    assert status == 200 and b"Connect gateway session" in body
    assert any(c.startswith("portal_gwdev=;") for c in set_cookies)


# ------------------------------------------------------------- connected UI


def test_connected_lists_caps(app, admin_config, audit):
    # Item shape as returned by the real gateway (runtime-verified 2.1.220):
    # nested scope object, no created_by, cleared rows linger with null amount.
    gw = StubGateway(api={("GET", ""): (200, {"data": [
        {"type": "spend_limit", "id": "spl_1",
         "scope": {"type": "user", "user_id": "00u123"}, "amount": "5000",
         "period": "monthly", "currency": "USD",
         "updated_at": "2026-07-25T00:00:00.000Z"},
        {"type": "spend_limit", "id": "spl_2",
         "scope": {"type": "rbac_group", "rbac_group_id": "claude-developers"},
         "amount": None, "period": "monthly", "currency": "USD",
         "updated_at": "2026-07-25T00:00:00.000Z"},
    ]})})
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gw": _gw_cookie(app, admin_config)}
    status, _, _, body = run_get(app, admin_config, audit, "/portal/admin",
                                 gateway=gw, cookies=cookies)
    assert status == 200
    assert b"$50.00" in body and b"00u123" in body
    assert b"(cleared)" in body and b"claude-developers" in body
    # the call rode the admin's own token, no API key anywhere
    assert gw.api_calls[0][1] == "gw-token-abc"


def test_gateway_403_reports_group_misalignment(app, admin_config, audit):
    gw = StubGateway(api={("GET", ""): (403, {"error": "forbidden"})})
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gw": _gw_cookie(app, admin_config)}
    status, _, _, body = run_get(app, admin_config, audit, "/portal/admin",
                                 gateway=gw, cookies=cookies)
    assert status == 403 and b"SpendAdminGroups" in body
    assert any(r["outcome"] == "denied" for r in audit.records)


def test_gateway_401_with_refresh_token_refreshes(app, admin_config, audit):
    now = int(time.time())
    payload = app.b64url_encode(json.dumps({"exp": now + 500}).encode())
    gw = StubGateway(api={("GET", ""): (401, None)},
                     refresh_resp={"access_token": "h2." + payload + ".s",
                                   "refresh_token": "rt-2"})
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gw": _gw_cookie(app, admin_config, rt="rt-1")}
    status, headers, set_cookies, _ = run_get(app, admin_config, audit, "/portal/admin",
                                              gateway=gw, cookies=cookies)
    assert status == 302 and headers["Location"] == "/portal/admin"
    assert gw.refreshed == ["rt-1"]
    tok = app.verify_cookie(cookie_value(set_cookies, "portal_gw"), admin_config.session_secret)
    assert tok["tok"].startswith("h2.") and tok["rt"] == "rt-2"


def test_gateway_401_without_refresh_falls_back_to_connect(app, admin_config, audit):
    gw = StubGateway(api={("GET", ""): (401, None)})
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gw": _gw_cookie(app, admin_config)}
    status, _, set_cookies, body = run_get(app, admin_config, audit, "/portal/admin",
                                           gateway=gw, cookies=cookies)
    assert status == 200 and b"Connect gateway session" in body
    assert any(c.startswith("portal_gw=;") for c in set_cookies)


# ------------------------------------------------------------- set / clear


def test_set_cap_posts_body_and_flashes_success(app, admin_config, audit):
    gw = StubGateway(api={("POST", ""): (200, {"id": "spl_1"})})
    cookie, csrf = _admin_session(app, admin_config)
    cookies = {"portal_session": cookie,
               "portal_gw": _gw_cookie(app, admin_config)}
    status, headers, set_cookies, _ = run_post(
        app, admin_config, audit, "/portal/admin/set",
        {"scope_type": "user", "scope_id": "00u123", "amount": "50.00", "period": "monthly",
         "csrf": csrf},
        gateway=gw, cookies=cookies)
    assert status == 302 and headers["Location"] == "/portal/admin"
    method, token, path, body = gw.api_calls[0]
    assert (method, token) == ("POST", "gw-token-abc")
    assert body["amount"] == "5000" and body["scope"]["user_id"] == "00u123"
    flash = app.verify_cookie(cookie_value(set_cookies, "portal_flash"),
                              admin_config.session_secret)
    assert flash["ok"] is True
    assert any(r["event"] == "portal_admin" and r["outcome"] == "success"
               for r in audit.records)


def test_set_cap_invalid_amount_never_reaches_gateway(app, admin_config, audit):
    gw = StubGateway()
    cookie, csrf = _admin_session(app, admin_config)
    cookies = {"portal_session": cookie,
               "portal_gw": _gw_cookie(app, admin_config)}
    status, _, set_cookies, _ = run_post(
        app, admin_config, audit, "/portal/admin/set",
        {"scope_type": "user", "scope_id": "00u123", "amount": "1.2.3", "period": "monthly",
         "csrf": csrf},
        gateway=gw, cookies=cookies)
    assert status == 302
    assert gw.api_calls == []
    flash = app.verify_cookie(cookie_value(set_cookies, "portal_flash"),
                              admin_config.session_secret)
    assert flash["ok"] is False


def test_clear_cap_sends_null_amount(app, admin_config, audit):
    gw = StubGateway(api={("POST", ""): (200, {})})
    cookie, csrf = _admin_session(app, admin_config)
    cookies = {"portal_session": cookie,
               "portal_gw": _gw_cookie(app, admin_config)}
    run_post(app, admin_config, audit, "/portal/admin/clear",
             {"scope_type": "rbac_group", "scope_id": "claude-developers", "period": "monthly",
              "csrf": csrf},
             gateway=gw, cookies=cookies)
    assert gw.api_calls[0][3]["amount"] is None
    assert gw.api_calls[0][3]["scope"]["rbac_group_id"] == "claude-developers"


def test_set_without_gateway_session_redirects(app, admin_config, audit):
    gw = StubGateway()
    cookie, csrf = _admin_session(app, admin_config)
    status, headers, _, _ = run_post(
        app, admin_config, audit, "/portal/admin/set",
        {"scope_type": "user", "scope_id": "x", "amount": "5", "period": "monthly",
         "csrf": csrf},
        gateway=gw, cookies={"portal_session": cookie})
    assert status == 302 and headers["Location"] == "/portal/admin"
    assert gw.api_calls == []


def test_gateway_error_on_set_flashes_failure_and_audits(app, admin_config, audit):
    gw = StubGateway(api={("POST", ""): (403, {"error": {"message": "requires write:spend_limits"}})})
    cookie, csrf = _admin_session(app, admin_config)
    cookies = {"portal_session": cookie,
               "portal_gw": _gw_cookie(app, admin_config)}
    _, _, set_cookies, _ = run_post(
        app, admin_config, audit, "/portal/admin/set",
        {"scope_type": "organization", "scope_id": "", "amount": "10000", "period": "monthly",
         "csrf": csrf},
        gateway=gw, cookies=cookies)
    flash = app.verify_cookie(cookie_value(set_cookies, "portal_flash"),
                              admin_config.session_secret)
    assert flash["ok"] is False and "write:spend_limits" in flash["msg"]
    assert any(r["outcome"] == "denied" for r in audit.records)


# ------------------------------------------------------------- audit page / disconnect


def test_audit_page_renders_gateway_admin_audit(app, admin_config, audit):
    gw = StubGateway(api={("GET", "/audit"): (200, {"data": [
        {"created_at": "2026-07-24T00:00:00Z", "actor": "oidc:00uAdmin",
         "action": "spend_limit.update", "target_id": "spl_1", "reason": None},
    ]})})
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gw": _gw_cookie(app, admin_config)}
    status, _, _, body = run_get(app, admin_config, audit, "/portal/admin/audit",
                                 gateway=gw, cookies=cookies)
    assert status == 200
    assert b"oidc:00uAdmin" in body and b"spend_limit.update" in body


def test_disconnect_clears_gateway_cookies(app, admin_config, audit):
    cookie, csrf = _admin_session(app, admin_config)
    cookies = {"portal_session": cookie,
               "portal_gw": _gw_cookie(app, admin_config)}
    status, headers, set_cookies, _ = run_post(
        app, admin_config, audit, "/portal/admin/disconnect", {"csrf": csrf},
        gateway=StubGateway(), cookies=cookies)
    assert status == 302
    assert any(c.startswith("portal_gw=;") for c in set_cookies)
    assert any(c.startswith("portal_gwdev=;") for c in set_cookies)


# ------------------------------------------------------------- escaping


def test_limit_rows_escape_gateway_data(app):
    html_out = app._limit_rows([{
        "scope_type": "user", "scope_id": "<script>alert(1)</script>",
        "amount": "100", "period": "monthly", "created_by": "x"}])
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


# ------------------------------------------------------------- gateway down


def test_gateway_unreachable_shows_message_not_500(app, admin_config, audit):
    """A network/TLS failure reaching the gateway (GatewayError from the
    client layer) must render a diagnosable message, never a bare 500."""
    class DeadGateway(StubGateway):
        def spend_api(self, method, token, path="", body=None):
            raise app_error
    app_error = app.GatewayError("gateway unreachable: [SSL: CERTIFICATE_VERIFY_FAILED]")
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gw": _gw_cookie(app, admin_config)}
    status, _, _, body = run_get(app, admin_config, audit, "/portal/admin",
                                 gateway=DeadGateway(), cookies=cookies)
    assert status == 200
    assert b"gateway unreachable" in body and b"CERTIFICATE_VERIFY_FAILED" in body


# ------------------------------------------------------------- csrf


def test_admin_post_without_csrf_is_rejected(app, admin_config, audit):
    """Lax's boundary is the SITE (registrable domain) - a sibling internal
    app is same-site, so admin mutations additionally require the
    synchronizer token."""
    gw = StubGateway(api={("POST", ""): (200, {})})
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gw": _gw_cookie(app, admin_config)}
    status, _, _, _ = run_post(
        app, admin_config, audit, "/portal/admin/set",
        {"scope_type": "user", "scope_id": "x", "amount": "5", "period": "monthly"},
        gateway=gw, cookies=cookies)
    assert status == 403
    assert gw.api_calls == []
    # wrong token is rejected the same way
    status, _, _, _ = run_post(
        app, admin_config, audit, "/portal/admin/set",
        {"scope_type": "user", "scope_id": "x", "amount": "5", "period": "monthly",
         "csrf": "f" * 32},
        gateway=gw, cookies=cookies)
    assert status == 403 and gw.api_calls == []


def test_admin_pages_embed_the_csrf_token(app, admin_config, audit):
    cookie, csrf = _admin_session(app, admin_config)
    _, _, _, body = run_get(app, admin_config, audit, "/portal/admin",
                            gateway=StubGateway(), cookies={
                                "portal_session": cookie,
                                "portal_gw": _gw_cookie(app, admin_config)})
    assert csrf.encode() in body


# ------------------------------------------------------------- slow_down


def test_slow_down_bumps_the_interval_and_resigns_txn(app, admin_config, audit):
    """RFC 8628 3.5: slow_down adds 5s for this and all subsequent polls."""
    gw = StubGateway(poll_results=["slow_down"])
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gwdev": _gwdev_cookie(app, admin_config)}
    status, _, set_cookies, body = run_get(app, admin_config, audit, "/portal/admin",
                                           gateway=gw, cookies=cookies)
    assert status == 200
    assert b"content='11'" in body                     # (5 + 5) + 1
    txn = app.verify_cookie(cookie_value(set_cookies, "portal_gwdev"),
                            admin_config.session_secret)
    assert txn["int"] == 10                            # persisted for subsequent polls


# ------------------------------------------------------------- cookie budget


def test_oversized_gateway_token_drops_refresh_then_errors(app, admin_config, audit):
    now = int(time.time())
    session = {"email": "a@x", "groups": [], "exp": now + 3600}
    small = {"access_token": "t" * 100, "refresh_token": "r" * 3000, "expires_in": 600}
    cookie, ttl = app.build_gw_cookie(small, session, "secret", now=now)
    assert cookie is not None
    payload = app.verify_cookie(cookie, "secret", now=now)
    assert payload["rt"] == ""                         # refresh token dropped to fit
    huge = {"access_token": "t" * 5000, "expires_in": 600}
    cookie, ttl = app.build_gw_cookie(huge, session, "secret", now=now)
    assert cookie is None and ttl == 0                 # explicit failure, not a silent drop


def test_poll_success_with_oversized_token_renders_explicit_error(app, admin_config, audit):
    gw = StubGateway(poll_results=[{"access_token": "t" * 5000, "expires_in": 600}])
    cookies = {"portal_session": _session_cookie(app, admin_config),
               "portal_gwdev": _gwdev_cookie(app, admin_config)}
    status, _, set_cookies, body = run_get(app, admin_config, audit, "/portal/admin",
                                           gateway=gw, cookies=cookies)
    assert status == 200
    assert b"too large" in body
    assert any(c.startswith("portal_gwdev=;") for c in set_cookies)
