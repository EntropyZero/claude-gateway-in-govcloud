"""User-guide PDF: viewer page + streamed PDF from the artifacts bucket.

Published by scripts/publish-portal-release.sh to
s3://<ArtifactsBucket>/<USER_GUIDE_KEY> (default docs/user-manual.pdf).
A missing object renders a friendly "not published yet" page, not a 500;
any OTHER S3 failure renders a distinct "could not read the guide" page so
the two cases are not conflated for operators.
"""

import logging

from flask import Blueprint, Response, g, render_template, request

from .common import cfg, ext, login_required, session_is_admin

log = logging.getLogger("portal")

bp = Blueprint("guide", __name__, url_prefix="/portal")

# The viewer iframes our own /portal/guide.pdf: frame-src 'self' on the
# viewer page, frame-ancestors 'self' on the PDF response. Everything else
# stays as strict as the site default.
_VIEWER_CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
               "img-src 'self'; form-action 'self'; frame-src 'self'; "
               "frame-ancestors 'none'; object-src 'none'")
_PDF_CSP = "default-src 'none'; frame-ancestors 'self'; object-src 'none'"


def _guide_missing(session):
    body = render_template(
        "guide_missing.html",
        email=session.get("email", ""),
        is_admin=session_is_admin(session),
        version=cfg().release_version,
    )
    return body, 404


def _guide_unavailable(session):
    """Non-missing S3 failure (AccessDenied, KMS, throttling, network): a
    distinct page so operators are not sent to the publish script when the
    real cause is a permissions/infra regression (details in the task log)."""
    body = render_template(
        "guide_unavailable.html",
        email=session.get("email", ""),
        is_admin=session_is_admin(session),
        version=cfg().release_version,
    )
    return body, 503


def _is_missing_key_error(exc):
    """True for S3 NoSuchKey/404-shaped ClientErrors (duck-typed so tests can
    fake the client without botocore)."""
    resp = getattr(exc, "response", None)
    if not isinstance(resp, dict):
        return False
    code = str((resp.get("Error") or {}).get("Code", ""))
    return code in ("NoSuchKey", "404", "NotFound")


@bp.get("/guide")
@login_required
def guide():
    session = g.portal_session
    resp = Response(render_template(
        "guide.html",
        email=session.get("email", ""),
        is_admin=session_is_admin(session),
        version=cfg().release_version,
    ))
    resp.headers["Content-Security-Policy"] = _VIEWER_CSP
    return resp


@bp.get("/guide.pdf")
@login_required
def guide_pdf():
    session = g.portal_session
    c = cfg()
    s3 = ext()["s3"]
    try:
        obj = s3.get_object(Bucket=c.artifacts_bucket, Key=c.user_guide_key)
    except Exception as exc:
        if _is_missing_key_error(exc):
            return _guide_missing(session)
        log.error("user-guide fetch failed: %s", exc)
        return _guide_unavailable(session)

    body = obj["Body"]

    def _chunks():
        while True:
            chunk = body.read(64 * 1024)
            if not chunk:
                break
            yield chunk

    resp = Response(_chunks(), mimetype="application/pdf")
    length = obj.get("ContentLength")
    if isinstance(length, int) and length > 0:
        resp.headers["Content-Length"] = str(length)
    disposition = "attachment" if request.args.get("download") == "1" else "inline"
    resp.headers["Content-Disposition"] = (
        '%s; filename="claude-code-user-guide.pdf"' % disposition)
    # Must be frameable by our own viewer page.
    resp.headers["Content-Security-Policy"] = _PDF_CSP
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp
