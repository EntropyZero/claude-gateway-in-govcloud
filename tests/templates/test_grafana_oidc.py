"""Structural guard for the Grafana OIDC endpoint derivation in
03-observability.yaml: both the org and custom auth-server branches must be
present for each endpoint (text-based so it needs no YAML/intrinsic parser)."""

import os

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "..", "cloudformation", "03-observability.yaml"
)


def _text():
    with open(TEMPLATE) as f:
        return f.read()


def test_both_server_types_derive_each_endpoint():
    t = _text()
    for endpoint in ("authorize", "token", "userinfo"):
        # org: <issuer>/oauth2/v1/<endpoint>
        assert f"${{OktaIssuer}}/oauth2/v1/{endpoint}" in t, f"missing org {endpoint} URL"
        # custom: <issuer>/v1/<endpoint>
        assert f"${{OktaIssuer}}/v1/{endpoint}" in t, f"missing custom {endpoint} URL"


def test_endpoint_choice_keys_off_the_mode_condition():
    t = _text()
    assert "OrgAuthServer: !Equals [!Ref OktaAuthServerType, 'org']" in t
    # the URLs must be selected by that condition, not hard-coded to one form
    assert "!If [OrgAuthServer" in t


def test_org_is_the_default_auth_server_type():
    t = _text()
    block = t[t.index("OktaAuthServerType:"):][:300]
    assert "Default: org" in block, "OktaAuthServerType default should be org"


def test_groups_scope_is_requested():
    # role mapping needs groups in the token; the org server returns them
    # only when the scope is requested.
    assert "openid profile email groups" in _text()


def test_issuer_pattern_accepts_both_forms_and_rejects_trailing_slash():
    import re

    t = _text()
    m = re.search(r"AllowedPattern:\s*'([^']*oauth2[^']*)'", t)
    assert m, "OktaIssuer AllowedPattern not found"
    pat = re.compile(m.group(1))
    assert pat.match("https://customerlogin.thecustomer.gov")          # org
    assert pat.match("https://your-org.okta.com/oauth2/default")       # custom
    assert not pat.match("https://customerlogin.thecustomer.gov/")     # trailing slash
    assert not pat.match("http://x.okta.com")                          # not https
