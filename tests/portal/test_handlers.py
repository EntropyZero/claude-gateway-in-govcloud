"""End-to-end HTTP flows through the Flask app: login redirect, OIDC callback
(state/nonce/PKCE, sig verify, group allow/deny, userinfo fallback, session
issuance incl. the new `sub` claim), the home/download pages (JS-upgraded
form + noscript two-step fallback), and the gated download (ZIP + audit +
mid-stream failure semantics)."""

import io
import json
import time
import zipfile

import pytest

from portal.crypto import verify_cookie

from conftest import (SECRET, TEST_ENV, FakeS3, Harness, cookie_value,
                     session_cookie, set_cookies_of, txn_cookie)

ISS = "https://issuer.example.com"
AUD = "client-abc"
GROUP = "claude-gateway-users"
SHA = "3f1c" + "0" * 60
LINUX_SHA = "9a2b" + "1" * 60


def _release_s3(*, version="2.1.207", installer=b"<PS1>", exe=b"MZ\x00exe",
                linux_installer=b"<SH>", linux_bin=b"\x7fELF\x00bin"):
    manifest = {"platforms": {"win32-x64": {"checksum": SHA},
                              "linux-x64": {"checksum": LINUX_SHA}}}
    return FakeS3({
        "releases/%s/manifest.json" % version: json.dumps(manifest).encode(),
        "releases/%s/claude.exe" % version: exe,
        "releases/%s/claude" % version: linux_bin,
        "Install-ClaudeCode.ps1": installer,
        "install-claude-code.sh": linux_installer,
    })


# ------------------------------------------------------------- health / home


def test_healthz_is_open():
    resp = Harness().get("/portal/healthz")
    assert resp.status_code == 200 and resp.data == b"ok"


def test_healthz_tolerates_trailing_slash():
    # The old handler rstrip("/")'d paths; strict_slashes=False keeps that.
    resp = Harness().get("/portal/healthz/")
    assert resp.status_code == 200 and resp.data == b"ok"


def test_home_without_session_redirects_to_login():
    resp = Harness().get("/portal")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/login"


def test_home_renders_cards_and_identity():
    resp = Harness().get("/portal", cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert b"dev@example.com" in resp.data
    assert b"/portal/download-page" in resp.data
    assert b"/portal/me" in resp.data
    assert b"/portal/guide" in resp.data
    assert b"/portal/fingerprint" in resp.data
    assert TEST_ENV["RELEASE_VERSION"].encode() in resp.data


def test_home_legacy_cost_center_url_redirects_to_download_page():
    # /portal?cost_center=... was the old stage-2 URL (bookmarks).
    resp = Harness().get("/portal?cost_center=CC-1000",
                         cookies={"portal_session": session_cookie()})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/download-page?cost_center=CC-1000"


def test_home_marks_self_usage_card_when_read_key_missing():
    env = dict(TEST_ENV)
    env["SPEND_READ_KEY"] = ""
    resp = Harness(env=env).get("/portal",
                                cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert b"Not enabled on this deployment" in resp.data


# ------------------------------------------------------------- download form


def test_download_page_stage1_renders_cost_centers_only():
    resp = Harness().get("/portal/download-page",
                         cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert b"CC-1000" in resp.data and b"CC-2000" in resp.data
    # No team dropdown yet - teams depend on the cost-center pick. (The full
    # mapping IS present as a JSON data block for portal.js.)
    assert b'name="team"' not in resp.data
    assert b'<option value="platform"' not in resp.data
    assert b'id="cc-map"' in resp.data
    assert b"dev@example.com" in resp.data


def test_download_page_stage2_renders_only_selected_cost_centers_teams():
    resp = Harness().get("/portal/download-page?cost_center=CC-1000",
                         cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    # CC-1000's teams as options, and none of CC-2000's.
    assert b'<option value="platform"' in resp.data
    assert b'<option value="data"' in resp.data
    assert b'<option value="security"' not in resp.data
    # The download form carries the chosen cost center.
    assert b'name="cost_center" value="CC-1000"' in resp.data
    # ...and offers the platform choice (windows preselected by default).
    assert b'name="platform"' in resp.data
    assert b'<option value="windows" selected' in resp.data
    assert b'<option value="linux"' in resp.data


def test_download_page_stage2_preselects_carried_platform():
    """The noscript stage-1 GET carries platform to /portal/download-page;
    stage 2 must preselect it instead of silently resetting to windows."""
    resp = Harness().get("/portal/download-page?cost_center=CC-1000&platform=linux",
                         cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert b'<option value="linux" selected' in resp.data
    assert b'<option value="windows" selected' not in resp.data
    # Junk platform values fall back to the windows default, no error.
    resp = Harness().get("/portal/download-page?cost_center=CC-1000&platform=darwin",
                         cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert b'<option value="windows" selected' in resp.data


def test_download_page_invalid_cost_center_falls_back_to_stage1():
    resp = Harness().get("/portal/download-page?cost_center=CC-9999",
                         cookies={"portal_session": session_cookie()})
    assert resp.status_code == 400
    assert b"not an allowed value" in resp.data
    assert b'name="team"' not in resp.data and b"CC-1000" in resp.data


def test_download_page_requires_login():
    resp = Harness().get("/portal/download-page")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/login"


def test_noscript_two_step_flow_end_to_end():
    """Stage 1 GET -> stage 2 GET -> /portal/download: the no-JS path."""
    h = Harness(s3=_release_s3())
    cookies = {"portal_session": session_cookie()}
    stage1 = h.get("/portal/download-page", cookies=cookies)
    assert b'action="/portal/download-page"' in stage1.data     # form round-trips
    stage2 = h.get("/portal/download-page?cost_center=CC-2000", cookies=cookies)
    assert b'action="/portal/download"' in stage2.data
    assert b'<option value="security"' in stage2.data
    dl = h.get("/portal/download?team=security&cost_center=CC-2000", cookies=cookies)
    assert dl.status_code == 200
    assert dl.headers["Content-Type"] == "application/zip"


# ------------------------------------------------------------- login


def test_login_redirects_to_okta_and_sets_txn(key):
    h = Harness(jwks=key.jwks())
    resp = h.get("/portal/login")
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert loc.startswith(ISS + "/oauth2/v1/authorize")
    assert "code_challenge=" in loc and "code_challenge_method=S256" in loc
    assert "state=" in loc and "nonce=" in loc
    set_cookies = set_cookies_of(resp)
    txn_raw = cookie_value(set_cookies, "portal_txn")
    txn = verify_cookie(txn_raw, SECRET)
    assert txn and "state" in txn and "nonce" in txn and "cv" in txn
    # Cookie attributes: HttpOnly; Secure; SameSite=Lax; Path=/portal.
    raw = [c for c in set_cookies if c.startswith("portal_txn=")][0]
    assert "HttpOnly" in raw and "Secure" in raw
    assert "SameSite=Lax" in raw and "Path=/portal" in raw


# ------------------------------------------------------------- callback


def _callback(key, *, token_resp, txn, query_extra="", userinfo_resp=None,
              env=None):
    h = Harness(env=env, jwks=key.jwks(), token_resp=token_resp,
                userinfo_resp=userinfo_resp)
    cookie = txn_cookie(txn["state"], txn["nonce"], txn.get("cv", "cv"))
    path = "/portal/oauth/callback?code=thecode&state=%s%s" % (txn["state"], query_extra)
    resp = h.get(path, cookies={"portal_txn": cookie})
    return resp, h


def test_callback_happy_path_issues_session_with_sub(key):
    txn = {"state": "st-123", "nonce": "no-123", "cv": "cv-123"}
    tok = key.id_token(ISS, AUD, nonce="no-123", groups=[GROUP])
    resp, h = _callback(key, token_resp={"id_token": tok, "access_token": "at"}, txn=txn)
    assert resp.status_code == 302 and resp.headers["Location"] == "/portal"
    set_cookies = set_cookies_of(resp)
    session = verify_cookie(cookie_value(set_cookies, "portal_session"), SECRET)
    assert session["email"] == "dev@example.com"
    assert GROUP in session["groups"]
    # NEW in v2: the session carries the ID token's sub - the gateway spend
    # principal /portal/me queries by.
    assert session["sub"] == "00u123"
    # The code exchange used the txn's PKCE verifier.
    assert h.oidc.exchanged == ("thecode", "cv-123")
    # txn cookie cleared.
    assert any(c.startswith("portal_txn=") and "Max-Age=0" in c for c in set_cookies)


def test_callback_rejects_state_mismatch(key):
    tok = key.id_token(ISS, AUD, nonce="n", groups=[GROUP])
    h = Harness(jwks=key.jwks(), token_resp={"id_token": tok})
    cookie = txn_cookie("expected", "n", "cv")
    resp = h.get("/portal/oauth/callback?code=c&state=ATTACKER",
                 cookies={"portal_txn": cookie})
    assert resp.status_code == 400
    # Never exchanged the code with a bad state.
    assert h.oidc.exchanged is None


def test_callback_without_txn_cookie_is_rejected(key):
    resp = Harness(jwks=key.jwks()).get("/portal/oauth/callback?code=c&state=s")
    assert resp.status_code == 400 and b"expired" in resp.data.lower()


def test_callback_okta_error_is_rendered_not_exchanged(key):
    h = Harness(jwks=key.jwks())
    cookie = txn_cookie("s", "n", "cv")
    resp = h.get("/portal/oauth/callback?state=s&error=access_denied"
                 "&error_description=User+is+not+assigned",
                 cookies={"portal_txn": cookie})
    assert resp.status_code == 400
    assert b"not assigned" in resp.data
    assert h.oidc.exchanged is None


def test_callback_rejects_bad_token(key):
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    # Nonce in token does NOT match the transaction nonce -> verification fails.
    tok = key.id_token(ISS, AUD, nonce="WRONG", groups=[GROUP])
    resp, _ = _callback(key, token_resp={"id_token": tok}, txn=txn)
    assert resp.status_code == 400 and b"verification failed" in resp.data.lower()


def test_callback_denies_non_member_and_audits(key):
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    tok = key.id_token(ISS, AUD, nonce="n", groups=["some-other-group"])
    resp, h = _callback(key, token_resp={"id_token": tok, "access_token": "at"}, txn=txn)
    assert resp.status_code == 403 and b"Access denied" in resp.data
    assert len(h.audit.records) == 1
    assert h.audit.records[0]["outcome"] == "denied"
    assert "access group" in h.audit.records[0]["reason"]


def test_callback_allows_member_of_any_configured_group(key, env):
    # Two groups configured; the user is a member of the SECOND one only.
    env["ACCESS_GROUP"] = "platform-eng,claude-gateway-users"
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    tok = key.id_token(ISS, AUD, nonce="n", groups=["claude-gateway-users"])
    resp, _ = _callback(key, token_resp={"id_token": tok, "access_token": "at"},
                        txn=txn, env=env)
    assert resp.status_code == 302 and resp.headers["Location"] == "/portal"


def test_callback_multi_group_denial_reason_lists_all_groups(key, env):
    env["ACCESS_GROUP"] = "platform-eng,contractors"
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    tok = key.id_token(ISS, AUD, nonce="n", groups=["some-other-group"])
    resp, h = _callback(key, token_resp={"id_token": tok, "access_token": "at"},
                        txn=txn, env=env)
    assert resp.status_code == 403 and b"Access denied" in resp.data
    reason = h.audit.records[0]["reason"]
    assert "platform-eng" in reason and "contractors" in reason


def test_callback_uses_userinfo_fallback_for_groups(key):
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    # ID token has NO groups claim (Okta org-server behaviour).
    tok = key.id_token(ISS, AUD, nonce="n")
    resp, h = _callback(
        key, token_resp={"id_token": tok, "access_token": "at"}, txn=txn,
        userinfo_resp={"groups": [GROUP], "email": "dev@example.com"})
    assert resp.status_code == 302 and resp.headers["Location"] == "/portal"
    assert h.oidc.userinfo_token == "at"  # userinfo was consulted
    session = verify_cookie(
        cookie_value(set_cookies_of(resp), "portal_session"), SECRET)
    assert GROUP in session["groups"]


# ------------------------------------------------------------- download


def test_download_without_session_redirects_to_login():
    resp = Harness().get("/portal/download?team=platform&cost_center=CC-1000")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/login"


def test_download_invalid_selection_is_400_and_audited():
    h = Harness(s3=_release_s3())
    resp = h.get("/portal/download?team=marketing&cost_center=CC-1000",
                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 400
    assert len(h.audit.records) == 1 and h.audit.records[0]["outcome"] == "denied"
    assert "invalid selection" in h.audit.records[0]["reason"]
    # The error page returns to stage 2 (the cost center was valid) with only
    # that cost center's teams.
    assert b'name="cost_center" value="CC-1000"' in resp.data
    assert b'<option value="security"' not in resp.data


def test_download_rejects_team_from_other_cost_center():
    # A hand-crafted URL pairing a real team with the wrong cost center must
    # fail exactly like an unknown team - the mapping is the authority.
    h = Harness(s3=_release_s3())
    resp = h.get("/portal/download?team=security&cost_center=CC-1000",
                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 400
    assert len(h.audit.records) == 1 and h.audit.records[0]["outcome"] == "denied"


def test_download_streams_zip_and_audits_success():
    exe = b"MZ" + b"\x00" * 2048
    h = Harness(s3=_release_s3(exe=exe, installer=b"<installer script>"))
    resp = h.get("/portal/download?team=platform&cost_center=CC-1000",
                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/zip"
    assert "claude-code-2.1.207-windows.zip" in resp.headers["Content-Disposition"]
    # No Content-Length: gunicorn then emits the body CHUNKED (HTTP/1.1), so a
    # truncated download is detectable by the missing 0-chunk terminator.
    assert "Content-Length" not in resp.headers
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    assert zf.testzip() is None
    assert zf.read("claude.exe") == exe
    assert zf.read("Install-ClaudeCode.ps1") == b"<installer script>"
    cmd = zf.read("install.cmd").decode()
    assert SHA in cmd and "platform" in cmd and "CC-1000" in cmd
    # audit success with the manifest sha.
    assert len(h.audit.records) == 1
    rec = h.audit.records[0]
    assert rec["outcome"] == "success" and rec["exe_sha256"] == SHA
    assert rec["team"] == "platform" and rec["cost_center"] == "CC-1000"
    assert rec["platform"] == "windows"


def test_download_without_platform_param_defaults_to_windows():
    # Pre-platform bookmarks (no platform in the query) keep working.
    h = Harness(s3=_release_s3())
    resp = h.get("/portal/download?team=platform&cost_center=CC-1000",
                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert "windows" in resp.headers["Content-Disposition"]
    assert h.audit.records[0]["platform"] == "windows"


def test_download_linux_streams_zip_and_audits_success():
    linux_bin = b"\x7fELF" + b"\x00" * 2048
    h = Harness(s3=_release_s3(linux_bin=linux_bin, linux_installer=b"<sh installer>"))
    resp = h.get("/portal/download?team=platform&cost_center=CC-1000&platform=linux",
                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/zip"
    assert "claude-code-2.1.207-linux.zip" in resp.headers["Content-Disposition"]
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    assert zf.testzip() is None
    assert set(zf.namelist()) == {"claude", "install-claude-code.sh",
                                  "install.sh", "README.txt"}
    assert zf.read("claude") == linux_bin
    assert zf.read("install-claude-code.sh") == b"<sh installer>"
    wrapper = zf.read("install.sh").decode()
    # The wrapper bakes the LINUX manifest sha, not the windows one.
    assert LINUX_SHA in wrapper and SHA not in wrapper
    assert "platform" in wrapper and "CC-1000" in wrapper
    readme = zf.read("README.txt").decode()
    assert "bash install.sh" in readme
    rec = h.audit.records[0]
    assert rec["outcome"] == "success" and rec["exe_sha256"] == LINUX_SHA
    assert rec["platform"] == "linux"


def test_download_linux_includes_extra_ca_when_configured(env):
    env["BUNDLE_EXTRA_CA"] = "true"
    s3 = _release_s3()
    s3.objects["extra-ca.pem"] = b"---ENTERPRISE CA---"
    h = Harness(env=env, s3=s3)
    resp = h.get("/portal/download?team=platform&cost_center=CC-1000&platform=linux",
                 cookies={"portal_session": session_cookie()})
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    assert zf.read("extra-ca.pem") == b"---ENTERPRISE CA---"
    assert "--extra-ca-cert-path" in zf.read("install.sh").decode()


def test_download_invalid_platform_is_400_and_audited():
    h = Harness(s3=_release_s3())
    resp = h.get("/portal/download?team=platform&cost_center=CC-1000&platform=darwin",
                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 400
    assert len(h.audit.records) == 1
    rec = h.audit.records[0]
    assert rec["outcome"] == "denied" and "platform" in rec["reason"]
    # The raw request value is recorded for the audit trail.
    assert rec["platform"] == "darwin"


def test_download_includes_extra_ca_when_configured(env):
    env["BUNDLE_EXTRA_CA"] = "true"
    s3 = _release_s3()
    s3.objects["extra-ca.pem"] = b"---ENTERPRISE CA---"
    h = Harness(env=env, s3=s3)
    resp = h.get("/portal/download?team=platform&cost_center=CC-1000",
                 cookies={"portal_session": session_cookie()})
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    assert zf.read("extra-ca.pem") == b"---ENTERPRISE CA---"


def test_download_uses_last_xff_entry_for_source_ip():
    # A client-spoofed first entry must NOT win: behind the single ALB the LAST
    # entry is the ALB-attested peer. Here 198.51.100.9 is a forged prefix.
    h = Harness(s3=_release_s3())
    h.get("/portal/download?team=security&cost_center=CC-2000",
          cookies={"portal_session": session_cookie()},
          headers={"X-Forwarded-For": "198.51.100.9, 10.0.0.42"})
    assert h.audit.records[0]["source_ip"] == "10.0.0.42"


def test_download_mid_stream_s3_failure_aborts_never_writes_500_page():
    """manifest + installer resolve; claude.exe fails mid-read AFTER headers
    are sent. The exception must propagate out of the streaming body (the WSGI
    server then drops the connection - detectable truncation), never render a
    500 page into the ZIP body. The audit success was recorded pre-stream."""
    s3 = _release_s3(exe=b"MZ" + b"\x00" * 4096)
    s3.fail_after["releases/2.1.207/claude.exe"] = 1
    h = Harness(s3=s3)
    resp = h.get("/portal/download?team=platform&cost_center=CC-1000",
                 cookies={"portal_session": session_cookie()})
    # Headers already committed as a success...
    assert resp.status_code == 200
    # ...but consuming the body surfaces the failure instead of a 500 page.
    with pytest.raises(OSError, match="mid-stream"):
        resp.data
    # audit success was recorded before streaming began.
    assert h.audit.records and h.audit.records[0]["outcome"] == "success"


def test_download_missing_exe_fails_before_zip_not_500_html_in_body():
    """A completely missing claude.exe (partial publish) surfaces as a
    streaming abort on the generator's FIRST read - the exception escapes the
    WSGI body (the test client sees it while starting the response) instead
    of a 500 page being written into the ZIP."""
    manifest = {"platforms": {"win32-x64": {"checksum": SHA}}}
    s3 = FakeS3({
        "releases/2.1.207/manifest.json": json.dumps(manifest).encode(),
        "Install-ClaudeCode.ps1": b"<PS1>",
        # no releases/2.1.207/claude.exe
    })
    h = Harness(s3=s3)
    with pytest.raises(Exception, match="claude.exe"):
        h.get("/portal/download?team=platform&cost_center=CC-1000",
              cookies={"portal_session": session_cookie()})
    assert h.audit.records and h.audit.records[0]["outcome"] == "success"


# ------------------------------------------------------------- misc hardening


def test_expired_session_cookie_is_anonymous():
    cookie = session_cookie(ttl=-10)
    resp = Harness().get("/portal", cookies={"portal_session": cookie})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/login"


def test_session_cookie_signed_with_other_secret_is_anonymous():
    cookie = session_cookie(secret="attacker-secret")
    resp = Harness().get("/portal", cookies={"portal_session": cookie})
    assert resp.status_code == 302


def test_unknown_path_is_404_error_page():
    resp = Harness().get("/portal/nope",
                         cookies={"portal_session": session_cookie()})
    assert resp.status_code == 404
    assert b"Not found" in resp.data


def test_oversized_post_body_is_413():
    env = dict(TEST_ENV, PORTAL_ADMIN_GROUP="claude-spend-admins")
    h = Harness(env=env)
    payload = {"csrf": "x" * 70000}     # > MAX_CONTENT_LENGTH (65536)
    resp = h.post("/portal/admin/connect", form=payload,
                  cookies={"portal_session": session_cookie(
                      groups=[GROUP, "claude-spend-admins"])})
    assert resp.status_code == 413


def test_session_cookie_attributes_on_issue(key):
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    tok = key.id_token(ISS, AUD, nonce="n", groups=[GROUP])
    resp, _ = _callback(key, token_resp={"id_token": tok, "access_token": "at"}, txn=txn)
    raw = [c for c in set_cookies_of(resp) if c.startswith("portal_session=")][0]
    assert "HttpOnly" in raw and "Secure" in raw
    assert "SameSite=Lax" in raw and "Path=/portal" in raw
