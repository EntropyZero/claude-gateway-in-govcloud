"""The spend-cap admin section: device-flow session handling, group gating
(portal AND gateway layers), the proxied list/set/clear/audit calls, and the
NEW all-users usage table - all against a stub gateway, no network. The design
under test: the portal holds NO write-capable gateway key; every admin call
rides the signed-in admin's own gateway token."""

import json
import time

import pytest

from portal.crypto import b64url_encode, sign_cookie, verify_cookie
from portal.gateway import GatewayError

from conftest import (SECRET, TEST_ENV, FakeS3, Harness, StubGateway, cookie_value,
                      csrf_for_payload, session_cookie, session_payload,
                      set_cookies_of)

GROUP = "claude-gateway-users"
ADMIN_GROUP = "claude-spend-admins"


# ------------------------------------------------------------- fixtures


@pytest.fixture
def admin_env(env):
    env["PORTAL_ADMIN_GROUP"] = ADMIN_GROUP
    return env


def _admin_session():
    """(session cookie, csrf token) for one admin session - POSTs need both."""
    payload = session_payload(email="admin@example.com",
                              groups=[GROUP, ADMIN_GROUP])
    return sign_cookie(payload, SECRET), csrf_for_payload(payload)


def _session(groups=None, email="admin@example.com"):
    return session_cookie(email=email,
                          groups=groups if groups is not None else [GROUP, ADMIN_GROUP])


def _gw_cookie(tok="gw-token-abc", rt="", ttl=900):
    return sign_cookie({"tok": tok, "rt": rt, "exp": int(time.time()) + ttl}, SECRET)


def _gwdev_cookie(dc="dc-123", ttl=600, interval=5):
    return sign_cookie(
        {"dc": dc, "uc": "WDJB-MJHT", "vu": "https://gw/verify", "int": interval,
         "exp": int(time.time()) + ttl}, SECRET)


def _harness(admin_env, gateway=None):
    return Harness(env=admin_env, gateway=gateway if gateway is not None else StubGateway())


# ------------------------------------------------------------- gating


def test_admin_404_when_feature_disabled(env):
    # No PORTAL_ADMIN_GROUP -> the path does not exist (indistinguishable
    # from any other unknown path).
    resp = Harness(env=env).get("/portal/admin",
                                cookies={"portal_session": session_cookie()})
    assert resp.status_code == 404


def test_admin_redirects_anonymous_to_login(admin_env):
    resp = _harness(admin_env).get("/portal/admin")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/login"


def test_admin_denies_non_member_and_audits(admin_env):
    h = _harness(admin_env)
    resp = h.get("/portal/admin",
                 cookies={"portal_session": _session(groups=[GROUP])})
    assert resp.status_code == 403 and b"Access denied" in resp.data
    assert h.audit.records and h.audit.records[0]["event"] == "portal_admin"
    assert h.audit.records[0]["outcome"] == "denied"


def test_home_links_admin_only_for_members(admin_env):
    h = _harness(admin_env)
    admin_body = h.get("/portal", cookies={"portal_session": _session()}).data
    plain_body = h.get("/portal", cookies={"portal_session": _session(groups=[GROUP])}).data
    assert b"/portal/admin" in admin_body
    assert b"/portal/admin/users" in admin_body
    assert b"/portal/admin" not in plain_body


# ------------------------------------------------------------- device flow


def test_connect_starts_device_flow_and_sets_txn_cookie(admin_env):
    h = _harness(admin_env)
    cookie, csrf = _admin_session()
    resp = h.post("/portal/admin/connect", form={"csrf": csrf},
                  cookies={"portal_session": cookie})
    assert resp.status_code == 302 and resp.headers["Location"] == "/portal/admin"
    raw = cookie_value(set_cookies_of(resp), "portal_gwdev")
    txn = verify_cookie(raw, SECRET)
    assert txn["dc"] == "dc-123" and txn["int"] == 5
    assert "WDJB-MJHT" in txn["uc"]


def test_pending_page_polls_and_shows_verification_link(admin_env):
    gw = StubGateway(poll_results=["pending"])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gwdev": _gwdev_cookie()})
    assert resp.status_code == 200
    assert gw.polled == ["dc-123"]
    assert b'http-equiv="refresh"' in resp.data and b"WDJB-MJHT" in resp.data
    assert b'content="6"' in resp.data      # interval 5 + 1


def test_poll_success_stores_token_and_redirects(admin_env):
    now = int(time.time())
    payload = b64url_encode(json.dumps({"exp": now + 600}).encode())
    gw = StubGateway(poll_results=[
        {"access_token": "h." + payload + ".s", "refresh_token": "rt-1"}])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gwdev": _gwdev_cookie()})
    assert resp.status_code == 302 and resp.headers["Location"] == "/portal/admin"
    set_cookies = set_cookies_of(resp)
    tok = verify_cookie(cookie_value(set_cookies, "portal_gw"), SECRET)
    assert tok["tok"].startswith("h.") and tok["rt"] == "rt-1"
    assert tok["exp"] <= now + 600      # never outlives the gateway token
    # the device txn cookie is cleared
    assert any(c.startswith("portal_gwdev=;") for c in set_cookies)
    assert any(r["event"] == "portal_admin" and r["outcome"] == "success"
               for r in h.audit.records)


def test_poll_dead_grant_clears_txn_and_offers_reconnect(admin_env):
    gw = StubGateway(poll_results=[GatewayError("gateway sign-in expired_token")])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gwdev": _gwdev_cookie()})
    assert resp.status_code == 200 and b"Connect gateway session" in resp.data
    assert any(c.startswith("portal_gwdev=;") for c in set_cookies_of(resp))


def test_slow_down_bumps_the_interval_and_resigns_txn(admin_env):
    """RFC 8628 3.5: slow_down adds 5s for this and all subsequent polls."""
    gw = StubGateway(poll_results=["slow_down"])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gwdev": _gwdev_cookie()})
    assert resp.status_code == 200
    assert b'content="11"' in resp.data                # (5 + 5) + 1
    txn = verify_cookie(cookie_value(set_cookies_of(resp), "portal_gwdev"), SECRET)
    assert txn["int"] == 10                            # persisted for later polls


def test_poll_success_with_oversized_token_renders_explicit_error(admin_env):
    gw = StubGateway(poll_results=[{"access_token": "t" * 5000, "expires_in": 600}])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gwdev": _gwdev_cookie()})
    assert resp.status_code == 200
    assert b"too large" in resp.data
    assert any(c.startswith("portal_gwdev=;") for c in set_cookies_of(resp))


# ------------------------------------------------------------- connected UI


def test_connected_lists_caps(admin_env):
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
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gw": _gw_cookie()})
    assert resp.status_code == 200
    assert b"$50.00" in resp.data and b"00u123" in resp.data
    assert b"(cleared)" in resp.data and b"claude-developers" in resp.data
    # header release tag renders on admin pages too (shared base.html who-line)
    assert b"release 2.1.207" in resp.data
    # the call rode the admin's own token, no API key anywhere
    assert gw.api_calls[0][1] == "gw-token-abc"


def test_caps_page_escapes_gateway_data(admin_env):
    # A hostile value in a gateway response must render inert (autoescape).
    gw = StubGateway(api={("GET", ""): (200, {"data": [
        {"scope": {"type": "user", "user_id": "<script>alert(1)</script>"},
         "amount": "100", "period": "monthly", "currency": "USD",
         "updated_at": ""},
    ]})})
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gw": _gw_cookie()})
    assert b"<script>alert(1)</script>" not in resp.data
    assert b"&lt;script&gt;" in resp.data


def test_gateway_403_reports_group_misalignment(admin_env):
    gw = StubGateway(api={("GET", ""): (403, {"error": "forbidden"})})
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gw": _gw_cookie()})
    assert resp.status_code == 403 and b"SpendAdminGroups" in resp.data
    assert any(r["outcome"] == "denied" for r in h.audit.records)


def test_gateway_401_with_refresh_token_refreshes(admin_env):
    now = int(time.time())
    payload = b64url_encode(json.dumps({"exp": now + 500}).encode())
    gw = StubGateway(api={("GET", ""): (401, None)},
                     refresh_resp={"access_token": "h2." + payload + ".s",
                                   "refresh_token": "rt-2"})
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gw": _gw_cookie(rt="rt-1")})
    assert resp.status_code == 302 and resp.headers["Location"] == "/portal/admin"
    assert gw.refreshed == ["rt-1"]
    tok = verify_cookie(cookie_value(set_cookies_of(resp), "portal_gw"), SECRET)
    assert tok["tok"].startswith("h2.") and tok["rt"] == "rt-2"


def test_gateway_401_without_refresh_falls_back_to_connect(admin_env):
    gw = StubGateway(api={("GET", ""): (401, None)})
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gw": _gw_cookie()})
    assert resp.status_code == 200 and b"Connect gateway session" in resp.data
    assert any(c.startswith("portal_gw=;") for c in set_cookies_of(resp))


def test_gateway_unreachable_shows_message_not_500(admin_env):
    """A network/TLS failure reaching the gateway (GatewayError from the
    client layer) must render a diagnosable message, never a bare 500."""
    class DeadGateway(StubGateway):
        def spend_api(self, method, token, path="", body=None):
            raise GatewayError(
                "gateway unreachable: [SSL: CERTIFICATE_VERIFY_FAILED]")
    h = _harness(admin_env, DeadGateway())
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gw": _gw_cookie()})
    assert resp.status_code == 200
    assert b"gateway unreachable" in resp.data
    assert b"CERTIFICATE_VERIFY_FAILED" in resp.data


# ------------------------------------------------------------- set / clear


def test_set_cap_posts_body_and_flashes_success(admin_env):
    gw = StubGateway(api={("POST", ""): (200, {"id": "spl_1"})})
    h = _harness(admin_env, gw)
    cookie, csrf = _admin_session()
    resp = h.post("/portal/admin/set",
                  form={"scope_type": "user", "scope_id": "00u123",
                        "amount": "50.00", "period": "monthly", "csrf": csrf},
                  cookies={"portal_session": cookie, "portal_gw": _gw_cookie()})
    assert resp.status_code == 302 and resp.headers["Location"] == "/portal/admin"
    method, token, path, body = gw.api_calls[0]
    assert (method, token) == ("POST", "gw-token-abc")
    assert body["amount"] == "5000" and body["scope"]["user_id"] == "00u123"
    flash = verify_cookie(cookie_value(set_cookies_of(resp), "portal_flash"), SECRET)
    assert flash["ok"] is True
    assert any(r["event"] == "portal_admin" and r["outcome"] == "success"
               for r in h.audit.records)


def test_set_cap_invalid_amount_never_reaches_gateway(admin_env):
    gw = StubGateway()
    h = _harness(admin_env, gw)
    cookie, csrf = _admin_session()
    resp = h.post("/portal/admin/set",
                  form={"scope_type": "user", "scope_id": "00u123",
                        "amount": "1.2.3", "period": "monthly", "csrf": csrf},
                  cookies={"portal_session": cookie, "portal_gw": _gw_cookie()})
    assert resp.status_code == 302
    assert gw.api_calls == []
    flash = verify_cookie(cookie_value(set_cookies_of(resp), "portal_flash"), SECRET)
    assert flash["ok"] is False


def test_clear_cap_sends_null_amount(admin_env):
    gw = StubGateway(api={("POST", ""): (200, {})})
    h = _harness(admin_env, gw)
    cookie, csrf = _admin_session()
    h.post("/portal/admin/clear",
           form={"scope_type": "rbac_group", "scope_id": "claude-developers",
                 "period": "monthly", "csrf": csrf},
           cookies={"portal_session": cookie, "portal_gw": _gw_cookie()})
    assert gw.api_calls[0][3]["amount"] is None
    assert gw.api_calls[0][3]["scope"]["rbac_group_id"] == "claude-developers"


def test_set_without_gateway_session_redirects_and_flashes_not_applied(admin_env):
    # The portal_gw cookie can expire between rendering the caps page and the
    # submit (TTL = min(gateway token, session)). The change is dropped - say
    # so, never a silent bounce to the connect page.
    gw = StubGateway()
    h = _harness(admin_env, gw)
    cookie, csrf = _admin_session()
    resp = h.post("/portal/admin/set",
                  form={"scope_type": "user", "scope_id": "x", "amount": "5",
                        "period": "monthly", "csrf": csrf},
                  cookies={"portal_session": cookie})
    assert resp.status_code == 302 and resp.headers["Location"] == "/portal/admin"
    assert gw.api_calls == []
    flash = verify_cookie(cookie_value(set_cookies_of(resp), "portal_flash"), SECRET)
    assert flash["ok"] is False and "NOT applied" in flash["msg"]


def test_gateway_error_on_set_flashes_failure_and_audits(admin_env):
    gw = StubGateway(api={("POST", ""): (403, {"error": {"message": "requires write:spend_limits"}})})
    h = _harness(admin_env, gw)
    cookie, csrf = _admin_session()
    resp = h.post("/portal/admin/set",
                  form={"scope_type": "organization", "scope_id": "",
                        "amount": "10000", "period": "monthly", "csrf": csrf},
                  cookies={"portal_session": cookie, "portal_gw": _gw_cookie()})
    flash = verify_cookie(cookie_value(set_cookies_of(resp), "portal_flash"), SECRET)
    assert flash["ok"] is False and "write:spend_limits" in flash["msg"]
    assert any(r["outcome"] == "denied" for r in h.audit.records)


# ------------------------------------------------------------- audit page / disconnect


def test_audit_page_renders_gateway_admin_audit(admin_env):
    gw = StubGateway(api={("GET", "/audit"): (200, {"data": [
        {"created_at": "2026-07-24T00:00:00Z", "actor": "oidc:00uAdmin",
         "action": "spend_limit.update", "target_id": "spl_1", "reason": None},
    ]})})
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin/audit", cookies={"portal_session": _session(),
                                                 "portal_gw": _gw_cookie()})
    assert resp.status_code == 200
    assert b"oidc:00uAdmin" in resp.data and b"spend_limit.update" in resp.data


def test_disconnect_clears_gateway_cookies(admin_env):
    h = _harness(admin_env)
    cookie, csrf = _admin_session()
    resp = h.post("/portal/admin/disconnect", form={"csrf": csrf},
                  cookies={"portal_session": cookie, "portal_gw": _gw_cookie()})
    assert resp.status_code == 302
    set_cookies = set_cookies_of(resp)
    assert any(c.startswith("portal_gw=;") for c in set_cookies)
    assert any(c.startswith("portal_gwdev=;") for c in set_cookies)


# ------------------------------------------------------------- csrf


def test_admin_post_without_csrf_is_rejected(admin_env):
    """Lax's boundary is the SITE (registrable domain) - a sibling internal
    app is same-site, so admin mutations additionally require the
    synchronizer token."""
    gw = StubGateway(api={("POST", ""): (200, {})})
    h = _harness(admin_env, gw)
    cookies = {"portal_session": _session(), "portal_gw": _gw_cookie()}
    resp = h.post("/portal/admin/set",
                  form={"scope_type": "user", "scope_id": "x", "amount": "5",
                        "period": "monthly"},
                  cookies=cookies)
    assert resp.status_code == 403
    assert gw.api_calls == []
    # wrong token is rejected the same way
    resp = h.post("/portal/admin/set",
                  form={"scope_type": "user", "scope_id": "x", "amount": "5",
                        "period": "monthly", "csrf": "f" * 32},
                  cookies=cookies)
    assert resp.status_code == 403 and gw.api_calls == []


def test_admin_pages_embed_the_csrf_token(admin_env):
    h = _harness(admin_env)
    cookie, csrf = _admin_session()
    resp = h.get("/portal/admin", cookies={"portal_session": cookie,
                                           "portal_gw": _gw_cookie()})
    assert csrf.encode() in resp.data


# ------------------------------------------------------------- all users


USAGE_ITEM = {
    "scope": {"type": "user", "user_id": "00u123"},
    "groups": ["claude-gateway-users", "eng"],
    "actor": {"type": "user_actor", "user_id": "00u123", "name": "Dev One",
              "email_address": "dev1@example.com", "deleted": False},
    "amount": "5000", "currency": "USD", "period": "monthly",
    "source": {"type": "rbac_group", "rbac_group_id": "claude-developers"},
    "spend_limit_id": "spl_1", "period_to_date_spend": "123.5",
}

NO_CAP_ITEM = {
    "scope": {"type": "user", "user_id": "00u456"},
    "groups": [],
    # actor.name / email_address can be null (live-verified) - must render.
    "actor": {"type": "user_actor", "user_id": "00u456", "name": None,
              "email_address": None, "deleted": False},
    "amount": None, "currency": "USD", "period": "monthly",
    "source": None, "spend_limit_id": None, "period_to_date_spend": "42",
}


def test_admin_users_without_gateway_session_redirects_with_flash(admin_env):
    h = _harness(admin_env)
    resp = h.get("/portal/admin/users", cookies={"portal_session": _session()})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/admin"
    flash = verify_cookie(cookie_value(set_cookies_of(resp), "portal_flash"), SECRET)
    assert flash["ok"] is False and "Connect" in flash["msg"]


def test_admin_users_is_admin_gated(admin_env):
    h = _harness(admin_env)
    resp = h.get("/portal/admin/users",
                 cookies={"portal_session": _session(groups=[GROUP])})
    assert resp.status_code == 403
    assert h.audit.records and h.audit.records[0]["outcome"] == "denied"


def test_admin_users_renders_table_defaults(admin_env):
    gw = StubGateway(effective=[
        (200, {"data": [USAGE_ITEM, NO_CAP_ITEM], "next_page": None})])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin/users", cookies={"portal_session": _session(),
                                                 "portal_gw": _gw_cookie()})
    assert resp.status_code == 200
    # Bearer auth with the admin's own token; monthly default; page-size 20;
    # no q / sort / page.
    auth, kwargs = gw.effective_calls[0]
    assert auth == ("bearer", "gw-token-abc")
    assert kwargs == {"periods": ["monthly"], "q": None, "sort": None,
                      "page": None, "limit": 20}
    body = resp.data
    assert b"Dev One" in body and b"dev1@example.com" in body
    assert b"00u123" in body and b"claude-gateway-users, eng" in body
    assert b"$50.00" in body            # cap
    assert b"$1.24" in body             # fractional-cents spend display
    assert b"2.5%" in body              # percent used
    assert b"group cap (claude-developers)" in body
    # No-cap row renders gracefully with null actor fields.
    assert b"00u456" in body and b"no cap" in body and b"$0.42" in body


def test_admin_users_passes_filters_and_requires_single_period_for_sort(admin_env):
    gw = StubGateway(effective=[(200, {"data": [], "next_page": None})])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin/users?period=daily&q=smith&page_size=50&sort=spend",
                 cookies={"portal_session": _session(), "portal_gw": _gw_cookie()})
    assert resp.status_code == 200
    auth, kwargs = gw.effective_calls[0]
    # sort=spend_desc REQUIRES exactly one period[] - held by construction.
    assert kwargs["sort"] == "spend_desc"
    assert kwargs["periods"] == ["daily"]
    assert kwargs["q"] == "smith" and kwargs["limit"] == 50


def test_admin_users_sanitizes_bogus_filter_values(admin_env):
    gw = StubGateway(effective=[(200, {"data": [], "next_page": None})])
    h = _harness(admin_env, gw)
    h.get("/portal/admin/users?period=hourly&page_size=999",
          cookies={"portal_session": _session(), "portal_gw": _gw_cookie()})
    _, kwargs = gw.effective_calls[0]
    assert kwargs["periods"] == ["monthly"] and kwargs["limit"] == 20


def test_admin_users_paging_token_roundtrip(admin_env):
    gw = StubGateway(effective=[
        (200, {"data": [USAGE_ITEM], "next_page": "tok-abc=="}),
        (200, {"data": [NO_CAP_ITEM], "next_page": None}),
    ])
    h = _harness(admin_env, gw)
    cookies = {"portal_session": _session(), "portal_gw": _gw_cookie()}
    page1 = h.get("/portal/admin/users?period=weekly&q=x&page_size=50&sort=spend",
                  cookies=cookies)
    body = page1.data.decode()
    # The Next link carries the opaque token AND every filter param.
    assert "page=tok-abc%3D%3D" in body
    assert "period=weekly" in body and "q=x" in body
    assert "page_size=50" in body and "sort=spend" in body
    # Follow it: the token goes back to the gateway verbatim.
    import re
    m = re.search(r'href="(/portal/admin/users\?[^"]*page=[^"]*)"', body)
    assert m
    next_url = m.group(1).replace("&amp;", "&")
    page2 = h.get(next_url, cookies=cookies)
    assert page2.status_code == 200
    _, kwargs = gw.effective_calls[1]
    assert kwargs["page"] == "tok-abc=="
    assert kwargs["periods"] == ["weekly"]      # filters preserved across pages
    # Last page: no further Next link (no next_page from the gateway).
    assert "page=tok" not in page2.data.decode()


def test_admin_users_first_link_clears_the_page_token(admin_env):
    gw = StubGateway(effective=[
        (200, {"data": [USAGE_ITEM], "next_page": "tok2"})])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin/users?period=monthly&page=tok1",
                 cookies={"portal_session": _session(), "portal_gw": _gw_cookie()})
    body = resp.data.decode()
    _, kwargs = gw.effective_calls[0]
    assert kwargs["page"] == "tok1"
    # A "first page" link exists without any page= param.
    import re
    first_links = [u for u in re.findall(r'href="(/portal/admin/users\?[^"]*)"', body)
                   if "page=" not in u.replace("page_size=", "")]
    assert first_links


def test_admin_users_401_refreshes_and_retries_same_url(admin_env):
    now = int(time.time())
    payload = b64url_encode(json.dumps({"exp": now + 500}).encode())
    gw = StubGateway(effective=[(401, None)],
                     refresh_resp={"access_token": "h2." + payload + ".s",
                                   "refresh_token": "rt-2"})
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin/users?period=daily",
                 cookies={"portal_session": _session(),
                          "portal_gw": _gw_cookie(rt="rt-1")})
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/portal/admin/users")
    assert "period=daily" in resp.headers["Location"]
    assert gw.refreshed == ["rt-1"]
    tok = verify_cookie(cookie_value(set_cookies_of(resp), "portal_gw"), SECRET)
    assert tok["tok"].startswith("h2.")


def test_admin_users_401_without_refresh_reconnects(admin_env):
    gw = StubGateway(effective=[(401, None)])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin/users",
                 cookies={"portal_session": _session(), "portal_gw": _gw_cookie()})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/admin"
    assert any(c.startswith("portal_gw=;") for c in set_cookies_of(resp))


def test_admin_users_403_reports_group_misalignment(admin_env):
    gw = StubGateway(effective=[(403, {"error": "forbidden"})])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin/users",
                 cookies={"portal_session": _session(), "portal_gw": _gw_cookie()})
    assert resp.status_code == 403 and b"SpendAdminGroups" in resp.data
    assert any(r["outcome"] == "denied" for r in h.audit.records)


def test_admin_users_escapes_gateway_data(admin_env):
    item = dict(USAGE_ITEM)
    item["actor"] = dict(item["actor"], name="<img src=x onerror=alert(1)>")
    gw = StubGateway(effective=[(200, {"data": [item], "next_page": None})])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin/users",
                 cookies={"portal_session": _session(), "portal_gw": _gw_cookie()})
    assert b"<img src=x" not in resp.data
    assert b"&lt;img" in resp.data


# ------------------------------------------------------------- identity map
# The gateway's admin_audit records actors as oidc:<sub> (opaque). At connect
# time the portal holds both halves - the session's Okta-verified email and
# the gateway token's sub - and persists the pairing to S3 so the audit page
# can render an Email column. Capture is best-effort (never blocks sign-in);
# lookup failure renders unmapped, not an error.

from portal.identity import principal_map_key  # noqa: E402

MAP_KEY = "identity/principal-emails/00uAdmin.json"


def _gw_token(sub="00uAdmin", ttl=600):
    """A gateway-session-shaped JWS (payload decodable, signature fake - the
    portal never verifies it)."""
    payload = b64url_encode(json.dumps(
        {"sub": sub, "exp": int(time.time()) + ttl}).encode())
    return "h." + payload + ".s"


def _gw_cookie_sub(tok=None, rt="", ttl=900, sub="00uAdmin"):
    """A portal_gw cookie as build_gw_cookie now writes it (with sub)."""
    return sign_cookie({"tok": tok or _gw_token(sub), "rt": rt, "sub": sub,
                        "exp": int(time.time()) + ttl}, SECRET)


def test_poll_success_writes_identity_map_and_cookie_sub(admin_env):
    gw = StubGateway(poll_results=[
        {"access_token": _gw_token("00uAdmin"), "refresh_token": "rt-1"}])
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gwdev": _gwdev_cookie()})
    assert resp.status_code == 302
    # sub -> email persisted as one JSON object under the reserved prefix
    assert h.s3.puts == [("portal-artifacts", MAP_KEY)]
    doc = json.loads(h.s3.objects[MAP_KEY])
    assert doc["sub"] == "00uAdmin" and doc["email"] == "admin@example.com"
    assert "updated_at" in doc
    # the signed cookie carries the sub for later audit attribution
    tok = verify_cookie(cookie_value(set_cookies_of(resp), "portal_gw"), SECRET)
    assert tok["sub"] == "00uAdmin"
    # the portal's own audit line joins to the gateway's admin_audit actor
    success = [r for r in h.audit.records if r["outcome"] == "success"]
    assert success and success[0]["gateway_actor"] == "oidc:00uAdmin"


def test_poll_success_survives_identity_map_write_failure(admin_env):
    # The map write is best-effort: an S3 outage must never block the admin
    # sign-in it rides on.
    gw = StubGateway(poll_results=[
        {"access_token": _gw_token("00uAdmin"), "refresh_token": "rt-1"}])
    h = Harness(env=admin_env, gateway=gw, s3=FakeS3(fail_puts=True))
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gwdev": _gwdev_cookie()})
    assert resp.status_code == 302
    assert cookie_value(set_cookies_of(resp), "portal_gw")
    assert any(r["outcome"] == "success" for r in h.audit.records)


def test_refresh_path_rewrites_identity_map(admin_env):
    gw = StubGateway(api={("GET", ""): (401, None)},
                     refresh_resp={"access_token": _gw_token("00uAdmin"),
                                   "refresh_token": "rt-2"})
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin", cookies={"portal_session": _session(),
                                           "portal_gw": _gw_cookie(rt="rt-1")})
    assert resp.status_code == 302
    assert h.s3.puts == [("portal-artifacts", MAP_KEY)]


def test_users_refresh_path_rewrites_identity_map(admin_env):
    gw = StubGateway(effective=[(401, None)],
                     refresh_resp={"access_token": _gw_token("00uAdmin"),
                                   "refresh_token": "rt-2"})
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin/users",
                 cookies={"portal_session": _session(),
                          "portal_gw": _gw_cookie(rt="rt-1")})
    assert resp.status_code == 302
    assert h.s3.puts == [("portal-artifacts", MAP_KEY)]


def test_audit_page_joins_actor_emails(admin_env):
    gw = StubGateway(api={("GET", "/audit"): (200, {"data": [
        {"created_at": "2026-07-24T00:00:00Z", "actor": "oidc:00uAdmin",
         "action": "spend_limit.update", "target_id": "spl_1", "reason": None},
        {"created_at": "2026-07-24T00:01:00Z", "actor": "admin-key:break-glass",
         "action": "spend_limit.update", "target_id": "spl_2", "reason": None},
        {"created_at": "2026-07-24T00:02:00Z", "actor": "oidc:00uNever",
         "action": "spend_limit.delete", "target_id": "spl_3", "reason": None},
    ]})})
    s3 = FakeS3(objects={MAP_KEY: json.dumps(
        {"sub": "00uAdmin", "email": "admin@example.com"}).encode()})
    h = Harness(env=admin_env, gateway=gw, s3=s3)
    resp = h.get("/portal/admin/audit", cookies={"portal_session": _session(),
                                                 "portal_gw": _gw_cookie()})
    assert resp.status_code == 200
    body = resp.data
    assert b"<th>Email</th>" in body
    assert b"admin@example.com" in body
    # break-glass key + never-connected admin render as a dash, and null
    # reasons render empty, not "None"
    assert body.count(b"<td>-</td>") == 2
    assert b"None" not in body


def test_audit_page_renders_when_map_unreadable(admin_env):
    class BrokenS3(FakeS3):
        def get_object(self, Bucket, Key):
            raise RuntimeError("S3 unavailable")
    gw = StubGateway(api={("GET", "/audit"): (200, {"data": [
        {"created_at": "2026-07-24T00:00:00Z", "actor": "oidc:00uAdmin",
         "action": "spend_limit.update", "target_id": "spl_1", "reason": None},
    ]})})
    h = Harness(env=admin_env, gateway=gw, s3=BrokenS3())
    resp = h.get("/portal/admin/audit", cookies={"portal_session": _session(),
                                                 "portal_gw": _gw_cookie()})
    assert resp.status_code == 200
    assert b"oidc:00uAdmin" in resp.data      # unmapped, but the page renders
    assert b"<td>-</td>" in resp.data


def test_set_cap_audit_records_gateway_actor(admin_env):
    gw = StubGateway(api={("POST", ""): (200, {"id": "spl_1"})})
    h = _harness(admin_env, gw)
    cookie, csrf = _admin_session()
    h.post("/portal/admin/set",
           form={"scope_type": "user", "scope_id": "00u123", "amount": "50",
                 "period": "monthly", "csrf": csrf},
           cookies={"portal_session": cookie, "portal_gw": _gw_cookie_sub()})
    success = [r for r in h.audit.records if r["outcome"] == "success"]
    assert success and success[0]["gateway_actor"] == "oidc:00uAdmin"


def test_legacy_gw_cookie_without_sub_still_works(admin_env):
    # Cookies signed before the sub field shipped keep verifying (the signer
    # is schema-agnostic); mutations succeed and just omit gateway_actor.
    gw = StubGateway(api={("POST", ""): (200, {"id": "spl_1"})})
    h = _harness(admin_env, gw)
    cookie, csrf = _admin_session()
    resp = h.post("/portal/admin/set",
                  form={"scope_type": "user", "scope_id": "00u123",
                        "amount": "50", "period": "monthly", "csrf": csrf},
                  cookies={"portal_session": cookie, "portal_gw": _gw_cookie()})
    assert resp.status_code == 302
    assert gw.api_calls          # the mutation went through
    success = [r for r in h.audit.records if r["outcome"] == "success"]
    assert success and "gateway_actor" not in success[0]


def test_principal_map_key_rejects_unsafe_subs():
    assert principal_map_key("00uAdmin") == MAP_KEY[:-len("00uAdmin.json")] + "00uAdmin.json"
    assert principal_map_key("") is None
    assert principal_map_key("a/../../etc") is None      # path traversal
    assert principal_map_key("a b") is None              # space
    assert principal_map_key("x" * 129) is None          # oversized


def test_audit_page_survives_non_string_actor(admin_env):
    # A malformed gateway response with a list/dict actor must render (the
    # Email join coerces to str), not 500 on an unhashable dict key.
    gw = StubGateway(api={("GET", "/audit"): (200, {"data": [
        {"created_at": "2026-07-24T00:00:00Z", "actor": ["evil"],
         "action": "spend_limit.update", "target_id": "spl_1", "reason": None},
        {"created_at": "2026-07-24T00:01:00Z", "actor": None,
         "action": "spend_limit.update", "target_id": "spl_2", "reason": None},
    ]})})
    h = _harness(admin_env, gw)
    resp = h.get("/portal/admin/audit", cookies={"portal_session": _session(),
                                                 "portal_gw": _gw_cookie()})
    assert resp.status_code == 200
    assert resp.data.count(b"<td>-</td>") == 2      # both unmapped


def test_gateway_403_denials_record_gateway_actor(admin_env):
    # Connected-session denials join to the gateway trail too, not just
    # successes: caps page and users page 403s both carry gateway_actor.
    gw = StubGateway(api={("GET", ""): (403, {"error": "forbidden"})},
                     effective=[(403, {"error": "forbidden"})])
    h = _harness(admin_env, gw)
    cookies = {"portal_session": _session(), "portal_gw": _gw_cookie_sub()}
    h.get("/portal/admin", cookies=cookies)
    h.get("/portal/admin/users", cookies=cookies)
    denied = [r for r in h.audit.records if r["outcome"] == "denied"]
    assert len(denied) == 2
    assert all(r["gateway_actor"] == "oidc:00uAdmin" for r in denied)
