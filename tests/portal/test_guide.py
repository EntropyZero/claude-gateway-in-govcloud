"""/portal/guide (viewer) + /portal/guide.pdf (streamed from S3): inline vs
attachment disposition, framing CSP, and the friendly missing-object page."""

from conftest import TEST_ENV, FakeS3, Harness, session_cookie

PDF = b"%PDF-1.7 fake-user-manual"


def _s3():
    return FakeS3({TEST_ENV["USER_GUIDE_KEY"]: PDF})


def test_guide_requires_login():
    resp = Harness().get("/portal/guide")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/login"
    resp = Harness().get("/portal/guide.pdf")
    assert resp.status_code == 302


def test_guide_viewer_embeds_pdf_and_links_download():
    resp = Harness(s3=_s3()).get("/portal/guide",
                                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert b'<iframe' in resp.data and b'src="/portal/guide.pdf"' in resp.data
    assert b"/portal/guide.pdf?download=1" in resp.data
    # The viewer page may frame our own PDF - and nothing else changes.
    csp = resp.headers["Content-Security-Policy"]
    assert "frame-src 'self'" in csp
    assert "default-src 'none'" in csp and "script-src 'self'" in csp


def test_guide_pdf_streams_inline_by_default():
    resp = Harness(s3=_s3()).get("/portal/guide.pdf",
                                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/pdf"
    assert resp.headers["Content-Disposition"].startswith("inline")
    assert resp.headers["Content-Length"] == str(len(PDF))
    assert resp.data == PDF
    # Frameable by our own viewer page only.
    assert "frame-ancestors 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_guide_pdf_download_param_switches_to_attachment():
    resp = Harness(s3=_s3()).get("/portal/guide.pdf?download=1",
                                 cookies={"portal_session": session_cookie()})
    assert resp.headers["Content-Disposition"].startswith("attachment")
    assert resp.data == PDF


def test_guide_pdf_missing_renders_friendly_page_not_500():
    # Empty bucket: the NoSuchKey-shaped error renders the "not published
    # yet" page pointing at the publish script.
    resp = Harness(s3=FakeS3()).get("/portal/guide.pdf",
                                    cookies={"portal_session": session_cookie()})
    assert resp.status_code == 404
    assert b"publish-portal-release.sh" in resp.data
    assert b"Internal error" not in resp.data


def test_guide_pdf_generic_s3_failure_renders_distinct_unavailable_page():
    # AccessDenied/KMS/network failures are NOT the "not published yet" case:
    # a distinct 503 page that points at the task log, never at the publish
    # script (that instruction would be wrong here) - and never a raw 500.
    class BrokenS3:
        def get_object(self, Bucket, Key):
            raise RuntimeError("s3 unavailable")

    resp = Harness(s3=BrokenS3()).get("/portal/guide.pdf",
                                      cookies={"portal_session": session_cookie()})
    assert resp.status_code == 503
    assert b"Could not read the user guide" in resp.data
    assert b"publish-portal-release.sh" not in resp.data
    assert b"Internal error" not in resp.data


def test_guide_key_is_configurable(env):
    env["USER_GUIDE_KEY"] = "docs/custom-guide.pdf"
    s3 = FakeS3({"docs/custom-guide.pdf": PDF})
    resp = Harness(env=env, s3=s3).get("/portal/guide.pdf",
                                       cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200 and resp.data == PDF
