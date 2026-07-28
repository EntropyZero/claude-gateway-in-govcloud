"""/portal/me - the signed-in user's own quotas + usage.

Server-side call with the READ-ONLY key (x-api-key), scoped to
user_ids[] = [session sub]. Feature-gated on SPEND_READ_KEY; explicit error
paths for a stale key (401) and an unreachable gateway."""

from portal.gateway import GatewayError
from portal.views.me import build_usage_rows, source_label

from conftest import TEST_ENV, Harness, StubGateway, session_cookie


def _item(period="monthly", amount="5000", spend="123.5", source=None):
    return {
        "scope": {"type": "user", "user_id": "00u123"},
        "groups": ["claude-gateway-users"],
        "actor": {"type": "user_actor", "user_id": "00u123", "name": "Dev",
                  "email_address": "dev@example.com", "deleted": False},
        "amount": amount, "currency": "USD", "period": period,
        "source": source, "spend_limit_id": "spl_1" if amount else None,
        "period_to_date_spend": spend,
    }


# ------------------------------------------------------------- row builder


def test_build_usage_rows_fractional_cents_decimal_math():
    rows = build_usage_rows([_item(amount="5000", spend="123.5",
                                   source={"type": "user"})])
    row = rows[0]
    assert row["spend_display"] == "$1.24"      # 123.5 cents, HALF_UP display
    assert row["cap_display"] == "$50.00"
    assert row["has_cap"] is True
    assert row["percent"]["display"] == "2.5%"  # 123.5/5000, one decimal
    assert row["percent"]["cls"] == "ok"
    assert row["source_label"] == "your user cap"


def test_build_usage_rows_no_cap_reports_spend_without_percent():
    rows = build_usage_rows([_item(amount=None, spend="42", source=None)])
    row = rows[0]
    assert row["has_cap"] is False and row["cap_display"] is None
    assert row["spend_display"] == "$0.42"
    assert row["percent"] is None
    assert row["source_label"] is None


def test_build_usage_rows_sorted_daily_weekly_monthly():
    rows = build_usage_rows([
        _item(period="monthly"), _item(period="daily"), _item(period="weekly")])
    assert [r["period"] for r in rows] == ["daily", "weekly", "monthly"]


def test_build_usage_rows_missing_spend_is_zero():
    rows = build_usage_rows([_item(amount="1000", spend=None)])
    assert rows[0]["spend_display"] == "$0.00"
    assert rows[0]["percent"]["display"] == "0.0%"


def test_source_label_forms():
    assert source_label({"type": "user"}) == "your user cap"
    assert source_label({"type": "rbac_group", "rbac_group_id": "devs"}) \
        == "group cap (devs)"
    assert source_label({"type": "rbac_group"}) == "group cap"
    assert source_label({"type": "organization"}) == "organization-wide cap"
    assert source_label(None) is None


# ------------------------------------------------------------- routes


def test_me_requires_login():
    resp = Harness().get("/portal/me")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/login"


def test_me_queries_own_sub_with_read_key_and_renders_bars():
    gw = StubGateway(effective=[(200, {"data": [
        _item(period="monthly", amount="5000", spend="123.5",
              source={"type": "rbac_group", "rbac_group_id": "claude-developers"}),
        _item(period="daily", amount=None, spend="42"),
    ], "next_page": None})])
    h = Harness(gateway=gw)
    resp = h.get("/portal/me", cookies={"portal_session": session_cookie(sub="00u123")})
    assert resp.status_code == 200
    # Server-side call: READ key via x-api-key, scoped to the session's sub.
    auth, kwargs = gw.effective_calls[0]
    assert auth == ("api_key", TEST_ENV["SPEND_READ_KEY"])
    assert kwargs == {"user_ids": ["00u123"]}
    body = resp.data
    assert b"$50.00" in body and b"$1.24" in body and b"2.5%" in body
    assert b"group cap (claude-developers)" in body
    # div-based progress bar with a generated width class.
    assert b'class="fill ok w' in body.replace(b"class='", b'class="')
    # No-cap daily row: spend shown, no bar, explicit "no cap".
    assert b"no cap - unlimited" in body and b"$0.42" in body


def test_me_without_read_key_renders_feature_gated_page():
    env = dict(TEST_ENV)
    env["SPEND_READ_KEY"] = ""
    gw = StubGateway()
    h = Harness(env=env, gateway=gw)
    resp = h.get("/portal/me", cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert b"not enabled" in resp.data.lower()
    assert b"SPEND_READ_KEY" in resp.data
    # How to enable: the 02 re-run then 04.
    assert b"deploy-gateway.sh" in resp.data
    assert b"deploy-download-portal.sh" in resp.data
    # The gateway was never called without a key.
    assert gw.effective_calls == []


def test_me_pre_upgrade_session_without_sub_redirects_to_login():
    # A session cookie minted by the previous portal version has no sub;
    # a fresh login mints one.
    resp = Harness().get("/portal/me",
                         cookies={"portal_session": session_cookie(sub=None)})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/login"


def test_me_gateway_401_stale_key_explicit_error_and_audit():
    gw = StubGateway(effective=[(401, None)])
    h = Harness(gateway=gw)
    resp = h.get("/portal/me", cookies={"portal_session": session_cookie()})
    assert resp.status_code == 502
    assert b"401" in resp.data
    assert b"rotated" in resp.data
    # Audited as a portal_usage denial.
    assert h.audit.records
    rec = h.audit.records[0]
    assert rec["event"] == "portal_usage" and rec["outcome"] == "denied"
    assert "401" in rec["reason"]


def test_me_gateway_unreachable_renders_error_not_500():
    gw = StubGateway(effective=[GatewayError("gateway unreachable: timed out")])
    h = Harness(gateway=gw)
    resp = h.get("/portal/me", cookies={"portal_session": session_cookie()})
    assert resp.status_code == 502
    assert b"gateway unreachable" in resp.data


def test_me_gateway_5xx_renders_error():
    gw = StubGateway(effective=[(500, None)])
    h = Harness(gateway=gw)
    resp = h.get("/portal/me", cookies={"portal_session": session_cookie()})
    assert resp.status_code == 502
    assert b"500" in resp.data


def test_me_no_usage_yet_renders_empty_state():
    gw = StubGateway(effective=[(200, {"data": [], "next_page": None})])
    h = Harness(gateway=gw)
    resp = h.get("/portal/me", cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert b"No usage recorded" in resp.data
