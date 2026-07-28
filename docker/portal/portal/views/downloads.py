"""Installer download: form page (JS chained dropdowns, noscript two-step
fallback) + the streamed ZIP.

Server-side validate_selection remains the enforcement regardless of how the
form was driven; the client-side chaining is convenience only.
"""

from flask import Blueprint, Response, g, render_template, request

from ..artifacts import (build_install_cmd, build_readme, read_s3_bytes,
                         release_sha256, s3_chunks, zip_stream)
from ..selection import SelectionError, validate_cost_center, validate_selection
from .common import (audit_denied, audit_success, cfg, ext, login_required,
                     session_is_admin)

bp = Blueprint("downloads", __name__, url_prefix="/portal")


def _render_form(session, cost_center=None, error=None, status=200):
    """Stage 1 (no cost_center): pick a cost center; portal.js upgrades this
    into the chained two-select form. Stage 2 (validated cost_center): pick
    one of ITS teams - the noscript round-trip path."""
    c = cfg()
    return render_template(
        "download.html",
        email=session.get("email", ""),
        version=c.release_version,
        is_admin=session_is_admin(session),
        cost_center=cost_center,
        cost_centers=c.cost_centers,
        teams=c.cost_center_teams.get(cost_center, []) if cost_center else [],
        cc_map=c.cost_center_teams,
        error=error,
    ), status


@bp.get("/download-page")
@login_required
def download_page():
    session = g.portal_session
    cost_center = request.args.get("cost_center")
    error = None
    if cost_center is not None:
        try:
            cost_center = validate_cost_center(cost_center, cfg())
        except SelectionError as exc:
            cost_center, error = None, str(exc)
    return _render_form(session, cost_center=cost_center, error=error,
                        status=200 if error is None else 400)


@bp.get("/download")
@login_required
def download():
    session = g.portal_session
    c = cfg()
    s3 = ext()["s3"]
    email = session.get("email", "")
    groups = session.get("groups", [])
    team = request.args.get("team")
    cost_center = request.args.get("cost_center")
    try:
        team, cost_center = validate_selection(team, cost_center, c)
    except SelectionError as exc:
        audit_denied(email, groups, "invalid selection: %s" % exc,
                     team=team, cost_center=cost_center)
        # Back to stage 2 if the cost center itself was valid, else stage 1.
        try:
            page_cc = validate_cost_center(cost_center, c)
        except SelectionError:
            page_cc = None
        return _render_form(session, cost_center=page_cc, error=str(exc),
                            status=400)

    # The manifest, installer, and extra-CA reads happen BEFORE the response
    # starts, so a problem there is a rendered error page. The claude.exe
    # GetObject does NOT: s3_chunks is a generator, so it runs after headers
    # are sent and a failure aborts the stream mid-body (deliberate - the
    # chunked encoding makes the truncation detectable, and a 500 page must
    # never be written into a ZIP body).
    sha256 = release_sha256(s3, c)
    install_cmd = build_install_cmd(
        c.gateway_url, sha256, team, cost_center,
        c.disable_updates, c.bundle_extra_ca,
    )
    readme = build_readme(
        c.gateway_url, c.release_version, sha256, team, cost_center,
        c.bundle_extra_ca,
    )
    installer_bytes = read_s3_bytes(s3, c.artifacts_bucket, c.installer_key)
    extra_ca_bytes = None
    if c.bundle_extra_ca:
        extra_ca_bytes = read_s3_bytes(s3, c.artifacts_bucket, c.extra_ca_key)
    exe_key = "releases/%s/claude.exe" % c.release_version

    # Audit BEFORE streaming: a mid-stream client disconnect must not lose
    # the record of an authorized, validated download request.
    audit_success(email, groups, team, cost_center, sha256)

    # No Content-Length: gunicorn emits the body chunked (HTTP/1.1), so a
    # truncated download (S3 read error mid-stream, task recycle, ALB cut)
    # omits the terminating 0-chunk and the client DETECTS it. A failure
    # mid-generator aborts the connection - never a 500 page in the body.
    gen = zip_stream(
        s3_chunks(s3, c.artifacts_bucket, exe_key),
        installer_bytes, install_cmd, readme, extra_ca_bytes,
    )
    resp = Response(gen, mimetype="application/zip")
    resp.headers["Content-Disposition"] = (
        'attachment; filename="claude-code-%s.zip"' % c.release_version)
    return resp
