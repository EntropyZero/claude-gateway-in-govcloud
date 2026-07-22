"""End-to-end HTTP flows through PortalHandler (no socket): login redirect,
OIDC callback (state/nonce/PKCE, sig verify, group allow/deny, userinfo
fallback, session issuance), and the gated download (ZIP + audit)."""

import io
import json
import time
import zipfile

import pytest

from conftest import FakeS3, StubOidc, make_handler, parse_response, cookie_value, dechunk

ISS = "https://issuer.example.com"
AUD = "client-abc"
GROUP = "claude-gateway-users"
SHA = "3f1c" + "0" * 60


def _session_cookie(app, config, email="dev@example.com", groups=None, ttl=3600):
    return app.sign_cookie(
        {"email": email, "groups": groups or [GROUP], "exp": int(time.time()) + ttl},
        config.session_secret,
    )


def _txn_cookie(app, config, state, nonce, cv="verifier123", ttl=600):
    return app.sign_cookie(
        {"state": state, "nonce": nonce, "cv": cv, "exp": int(time.time()) + ttl},
        config.session_secret,
    )


def run(app, config, oidc, audit, path, cookies=None, headers=None):
    h = make_handler(app, config, oidc, audit, cookies=cookies, headers=headers)
    h.path = path
    h.do_GET()
    return parse_response(h)


# ------------------------------------------------------------- health / index


def test_healthz_is_open(app, config, audit):
    status, _, _, body = run(app, config, StubOidc(config, {"keys": []}), audit, "/portal/healthz")
    assert status == 200 and body == b"ok"


def test_index_without_session_redirects_to_login(app, config, audit):
    status, headers, _, _ = run(app, config, StubOidc(config, {"keys": []}), audit, "/portal")
    assert status == 302 and headers["Location"] == "/portal/login"


def test_index_with_session_renders_dropdowns(app, config, audit):
    cookie = _session_cookie(app, config)
    status, _, _, body = run(app, config, StubOidc(config, {"keys": []}), audit,
                             "/portal", cookies={"portal_session": cookie})
    assert status == 200
    assert b"platform" in body and b"CC-1000" in body
    assert b"dev@example.com" in body


# ------------------------------------------------------------- login


def test_login_redirects_to_okta_and_sets_txn(app, config, audit, key):
    oidc = StubOidc(config, key.jwks())
    status, headers, set_cookies, _ = run(app, config, oidc, audit, "/portal/login")
    assert status == 302
    loc = headers["Location"]
    assert loc.startswith(ISS + "/oauth2/v1/authorize")
    assert "code_challenge=" in loc and "code_challenge_method=S256" in loc
    assert "state=" in loc and "nonce=" in loc
    txn_raw = cookie_value(set_cookies, "portal_txn")
    txn = app.verify_cookie(txn_raw, config.session_secret)
    assert txn and "state" in txn and "nonce" in txn and "cv" in txn


# ------------------------------------------------------------- callback


def _callback(app, config, audit, key, *, token_resp, txn, query_extra=""):
    oidc = StubOidc(config, key.jwks(), token_resp=token_resp)
    txn_cookie = _txn_cookie(app, config, txn["state"], txn["nonce"], txn["cv"])
    path = "/portal/oauth/callback?code=thecode&state=%s%s" % (txn["state"], query_extra)
    h = make_handler(app, config, oidc, audit, cookies={"portal_txn": txn_cookie})
    h.path = path
    h.do_GET()
    return parse_response(h), oidc


def test_callback_happy_path_issues_session(app, config, audit, key):
    txn = {"state": "st-123", "nonce": "no-123", "cv": "cv-123"}
    tok = key.id_token(ISS, AUD, nonce="no-123", groups=[GROUP])
    (status, headers, set_cookies, _), _ = _callback(
        app, config, audit, key, token_resp={"id_token": tok, "access_token": "at"}, txn=txn
    )
    assert status == 302 and headers["Location"] == "/portal"
    session = app.verify_cookie(cookie_value(set_cookies, "portal_session"), config.session_secret)
    assert session["email"] == "dev@example.com"
    assert GROUP in session["groups"]
    # txn cookie cleared.
    assert any(c.startswith("portal_txn=") and "Max-Age=0" in c for c in set_cookies)


def test_callback_rejects_state_mismatch(app, config, audit, key):
    txn = {"state": "expected", "nonce": "n", "cv": "cv"}
    tok = key.id_token(ISS, AUD, nonce="n", groups=[GROUP])
    txn_cookie = _txn_cookie(app, config, "expected", "n", "cv")
    oidc = StubOidc(config, key.jwks(), token_resp={"id_token": tok})
    h = make_handler(app, config, oidc, audit, cookies={"portal_txn": txn_cookie})
    h.path = "/portal/oauth/callback?code=c&state=ATTACKER"
    h.do_GET()
    status, _, _, body = parse_response(h)
    assert status == 400
    # Never exchanged the code with a bad state.
    assert oidc.exchanged is None


def test_callback_without_txn_cookie_is_rejected(app, config, audit, key):
    oidc = StubOidc(config, key.jwks())
    status, _, _, body = run(app, config, oidc, audit,
                             "/portal/oauth/callback?code=c&state=s")
    assert status == 400 and b"expired" in body.lower()


def test_callback_rejects_bad_token(app, config, audit, key):
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    # Nonce in token does NOT match the transaction nonce -> verification fails.
    tok = key.id_token(ISS, AUD, nonce="WRONG", groups=[GROUP])
    (status, _, _, body), _ = _callback(
        app, config, audit, key, token_resp={"id_token": tok}, txn=txn
    )
    assert status == 400 and b"verification failed" in body.lower()


def test_callback_denies_non_member_and_audits(app, config, audit, key):
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    tok = key.id_token(ISS, AUD, nonce="n", groups=["some-other-group"])
    (status, _, _, body), _ = _callback(
        app, config, audit, key, token_resp={"id_token": tok, "access_token": "at"}, txn=txn
    )
    assert status == 403 and b"Access denied" in body
    assert len(audit.records) == 1
    assert audit.records[0]["outcome"] == "denied"
    assert "access group" in audit.records[0]["reason"]


def test_callback_allows_member_of_any_configured_group(app, env, audit, key):
    # Two groups configured; the user is a member of the SECOND one only.
    cfg = app.Config({**env, "ACCESS_GROUP": "platform-eng,claude-gateway-users"})
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    tok = key.id_token(ISS, AUD, nonce="n", groups=["claude-gateway-users"])
    (status, headers, _, _), _ = _callback(
        app, cfg, audit, key, token_resp={"id_token": tok, "access_token": "at"}, txn=txn
    )
    assert status == 302 and headers["Location"] == "/portal"


def test_callback_multi_group_denial_reason_lists_all_groups(app, env, audit, key):
    cfg = app.Config({**env, "ACCESS_GROUP": "platform-eng,contractors"})
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    tok = key.id_token(ISS, AUD, nonce="n", groups=["some-other-group"])
    (status, _, _, body), _ = _callback(
        app, cfg, audit, key, token_resp={"id_token": tok, "access_token": "at"}, txn=txn
    )
    assert status == 403 and b"Access denied" in body
    reason = audit.records[0]["reason"]
    assert "platform-eng" in reason and "contractors" in reason


def test_callback_uses_userinfo_fallback_for_groups(app, config, audit, key):
    txn = {"state": "s", "nonce": "n", "cv": "cv"}
    # ID token has NO groups claim (Okta org-server behaviour).
    tok = key.id_token(ISS, AUD, nonce="n")
    oidc = StubOidc(config, key.jwks(),
                    token_resp={"id_token": tok, "access_token": "at"},
                    userinfo_resp={"groups": [GROUP], "email": "dev@example.com"})
    txn_cookie = _txn_cookie(app, config, "s", "n", "cv")
    h = make_handler(app, config, oidc, audit, cookies={"portal_txn": txn_cookie})
    h.path = "/portal/oauth/callback?code=c&state=s"
    h.do_GET()
    status, headers, set_cookies, _ = parse_response(h)
    assert status == 302 and headers["Location"] == "/portal"
    assert oidc.userinfo_token == "at"  # userinfo was consulted
    session = app.verify_cookie(cookie_value(set_cookies, "portal_session"), config.session_secret)
    assert GROUP in session["groups"]


# ------------------------------------------------------------- download


def _wire_s3(app, monkeypatch, *, version="2.1.207", installer=b"<PS1>", exe=b"MZ\x00exe"):
    manifest = {"platforms": {"win32-x64": {"checksum": SHA}}}
    objs = {
        "releases/%s/manifest.json" % version: json.dumps(manifest).encode(),
        "releases/%s/claude.exe" % version: exe,
        "Install-ClaudeCode.ps1": installer,
    }
    monkeypatch.setattr(app, "s3", FakeS3(objs))


def test_download_without_session_redirects_to_login(app, config, audit):
    status, headers, _, _ = run(app, config, StubOidc(config, {"keys": []}), audit,
                                "/portal/download?team=platform&cost_center=CC-1000")
    assert status == 302 and headers["Location"] == "/portal/login"


def test_download_invalid_selection_is_400_and_audited(app, config, audit, monkeypatch):
    _wire_s3(app, monkeypatch)
    cookie = _session_cookie(app, config)
    status, _, _, body = run(app, config, StubOidc(config, {"keys": []}), audit,
                             "/portal/download?team=marketing&cost_center=CC-1000",
                             cookies={"portal_session": cookie})
    assert status == 400
    assert len(audit.records) == 1 and audit.records[0]["outcome"] == "denied"
    assert "invalid selection" in audit.records[0]["reason"]


def test_download_streams_zip_and_audits_success(app, config, audit, monkeypatch):
    exe = b"MZ" + b"\x00" * 2048
    _wire_s3(app, monkeypatch, exe=exe, installer=b"<installer script>")
    cookie = _session_cookie(app, config)
    status, headers, _, body = run(app, config, StubOidc(config, {"keys": []}), audit,
                                   "/portal/download?team=platform&cost_center=CC-1000",
                                   cookies={"portal_session": cookie})
    assert status == 200
    assert headers["Content-Type"] == "application/zip"
    assert "claude-code-2.1.207.zip" in headers["Content-Disposition"]
    # Chunked so a truncated download is detectable (terminating 0-chunk).
    assert headers.get("Transfer-Encoding") == "chunked"
    zf = zipfile.ZipFile(io.BytesIO(dechunk(body)))
    assert zf.testzip() is None
    assert zf.read("claude.exe") == exe
    assert zf.read("Install-ClaudeCode.ps1") == b"<installer script>"
    cmd = zf.read("install.cmd").decode()
    assert SHA in cmd and "platform" in cmd and "CC-1000" in cmd
    # audit success with the manifest sha.
    assert len(audit.records) == 1
    rec = audit.records[0]
    assert rec["outcome"] == "success" and rec["exe_sha256"] == SHA
    assert rec["team"] == "platform" and rec["cost_center"] == "CC-1000"
    assert rec["source_ip"] == "10.0.0.9"
    # Well-formed chunked stream: ends with the terminating 0-length chunk.
    assert body.endswith(b"0\r\n\r\n")


def test_download_uses_last_xff_entry_for_source_ip(app, config, audit, monkeypatch):
    # A client-spoofed first entry must NOT win: behind the single ALB the LAST
    # entry is the ALB-attested peer. Here 198.51.100.9 is a forged prefix.
    _wire_s3(app, monkeypatch)
    cookie = _session_cookie(app, config)
    h = make_handler(app, config, StubOidc(config, {"keys": []}), audit,
                     cookies={"portal_session": cookie},
                     headers={"X-Forwarded-For": "198.51.100.9, 10.0.0.42"})
    h.path = "/portal/download?team=data&cost_center=CC-2000"
    h.do_GET()
    assert audit.records[0]["source_ip"] == "10.0.0.42"


def test_download_aborts_without_500_page_when_s3_fails_mid_stream(app, config, audit, monkeypatch):
    # manifest + installer resolve, but claude.exe is missing (partial publish):
    # s3_chunks fails AFTER headers are sent. The catch-all must NOT write a 500
    # HTML page into the ZIP body - it drops the connection instead.
    manifest = {"platforms": {"win32-x64": {"checksum": SHA}}}
    objs = {
        "releases/2.1.207/manifest.json": json.dumps(manifest).encode(),
        "Install-ClaudeCode.ps1": b"<PS1>",
        # no releases/2.1.207/claude.exe
    }
    monkeypatch.setattr(app, "s3", FakeS3(objs))
    cookie = _session_cookie(app, config)
    status, headers, _, body = run(app, config, StubOidc(config, {"keys": []}), audit,
                                   "/portal/download?team=platform&cost_center=CC-1000",
                                   cookies={"portal_session": cookie})
    # 200 headers already went out; no "500"/"Internal error" leaked into body.
    assert status == 200
    assert b"Internal error" not in body
    assert b"HTTP/1.1 500" not in body
    # audit success was recorded before streaming began.
    assert audit.records and audit.records[0]["outcome"] == "success"
