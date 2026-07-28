"""Security headers on EVERY response (CSP without inline script/style,
nosniff, frame denial, referrer policy), static asset serving under /portal/,
and safe JSON-in-HTML embedding of the dropdown mapping."""

import json

import pytest

from conftest import TEST_ENV, FakeS3, Harness, session_cookie

DEFAULT_CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
               "img-src 'self'; form-action 'self'; frame-ancestors 'none'; "
               "object-src 'none'")


def _release_s3():
    manifest = {"platforms": {"win32-x64": {"checksum": "ab" * 32}}}
    return FakeS3({
        "releases/2.1.207/manifest.json": json.dumps(manifest).encode(),
        "releases/2.1.207/claude.exe": b"MZ\x00",
        "Install-ClaudeCode.ps1": b"<PS1>",
        TEST_ENV["USER_GUIDE_KEY"]: b"%PDF-1.7",
    })


# Every route in the app (with a session where needed), and the status we
# expect - the CSP + hardening headers must be present on ALL of them,
# including redirects, error pages and static files.
ROUTES = [
    ("/portal/healthz", 200),
    ("/portal", 200),
    ("/portal/login", 302),
    ("/portal/oauth/callback", 400),        # no txn cookie -> error page
    ("/portal/download-page", 200),
    ("/portal/download-page?cost_center=CC-9999", 400),
    ("/portal/download?team=platform&cost_center=CC-1000", 200),
    ("/portal/me", 200),
    ("/portal/guide", 200),
    ("/portal/guide.pdf", 200),
    ("/portal/fingerprint", 200),
    ("/portal/does-not-exist", 404),
    ("/portal/static/portal.css", 200),
    ("/portal/static/portal.js", 200),
]


@pytest.mark.parametrize("path,expected_status", ROUTES)
def test_hardening_headers_on_every_route(path, expected_status):
    h = Harness(s3=_release_s3())
    resp = h.get(path, cookies={"portal_session": session_cookie()})
    assert resp.status_code == expected_status, path
    csp = resp.headers.get("Content-Security-Policy", "")
    assert csp, "missing CSP on %s" % path
    assert "default-src 'none'" in csp
    # No inline anything, anywhere.
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "object-src 'none'" in csp
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    assert "X-Frame-Options" in resp.headers


def test_default_csp_exact_on_plain_pages():
    h = Harness()
    resp = h.get("/portal", cookies={"portal_session": session_cookie()})
    assert resp.headers["Content-Security-Policy"] == DEFAULT_CSP
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_only_guide_routes_relax_framing():
    """frame-src/frame-ancestors deviations are limited to the PDF viewer
    pair; everything else keeps frame-ancestors 'none'."""
    h = Harness(s3=_release_s3())
    cookies = {"portal_session": session_cookie()}
    for path in ("/portal", "/portal/download-page", "/portal/me",
                 "/portal/fingerprint"):
        csp = h.get(path, cookies=cookies).headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp, path
        assert "frame-src" not in csp.replace("frame-ancestors", ""), path
    guide_csp = h.get("/portal/guide", cookies=cookies).headers["Content-Security-Policy"]
    assert "frame-src 'self'" in guide_csp
    pdf = h.get("/portal/guide.pdf", cookies=cookies)
    assert "frame-ancestors 'self'" in pdf.headers["Content-Security-Policy"]


def test_no_inline_script_or_style_in_rendered_pages():
    """CSP forbids inline code, so the markup must not rely on any: no
    executable inline <script> (the JSON data block is type=application/json,
    which is inert), no style= attributes, no <style> blocks."""
    import re
    h = Harness(s3=_release_s3())
    cookies = {"portal_session": session_cookie()}
    for path in ("/portal", "/portal/download-page", "/portal/me",
                 "/portal/fingerprint", "/portal/guide"):
        body = h.get(path, cookies=cookies).data.decode()
        for m in re.finditer(r"<script\b[^>]*>", body):
            tag = m.group(0)
            assert 'src=' in tag or 'type="application/json"' in tag, \
                "inline script on %s: %s" % (path, tag)
        assert "<style" not in body, path
        assert "style=" not in body, path


# ------------------------------------------------------------- static assets


def test_static_css_resolves_under_portal_prefix():
    resp = Harness().get("/portal/static/portal.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["Content-Type"]
    assert b".bar" in resp.data          # progress-bar styles exist


def test_static_js_resolves_under_portal_prefix():
    resp = Harness().get("/portal/static/portal.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["Content-Type"]
    # The chained-dropdown upgrade + table sort live here.
    assert b"cc-map" in resp.data
    assert b"sortable" in resp.data


def test_css_has_generated_width_classes_for_bars():
    """Progress-bar widths come from .wN classes (inline style is blocked by
    style-src 'self')."""
    body = Harness().get("/portal/static/portal.css").data
    assert b".w0" in body and b".w50" in body and b".w100" in body


# ------------------------------------------------------------- JSON embed


def test_cc_map_json_embed_is_script_safe(env):
    """A team/cost-center value containing '</script>' must not be able to
    close the JSON data block (tojson escapes the angle brackets)."""
    env["PORTAL_COST_CENTER_TEAMS"] = "CC-1000:a</script><script>alert(1)</script>b"
    h = Harness(env=env)
    resp = h.get("/portal/download-page",
                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    body = resp.data
    assert b'id="cc-map"' in body
    # The raw closing tag never appears inside the page.
    assert b"</script><script>alert(1)</script>b" not in body
    # But the mapping is still recoverable by the client JS via JSON.parse.
    start = body.index(b'id="cc-map"')
    frag = body[start:body.index(b"</script>", start)]
    payload = frag.split(b">", 1)[1]
    data = json.loads(payload)
    assert data["CC-1000"] == ["a</script><script>alert(1)</script>b"]


def test_cc_map_embed_matches_config(env):
    resp = Harness(env=env).get("/portal/download-page",
                                cookies={"portal_session": session_cookie()})
    body = resp.data
    start = body.index(b'id="cc-map"')
    frag = body[start:body.index(b"</script>", start)]
    data = json.loads(frag.split(b">", 1)[1])
    assert data == {"CC-1000": ["platform", "data"], "CC-2000": ["security"]}
