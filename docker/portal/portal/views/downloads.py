"""Installer download: form page (JS chained dropdowns, noscript two-step
fallback) + the streamed ZIP.

Server-side validate_selection remains the enforcement regardless of how the
form was driven; the client-side chaining is convenience only.
"""

from flask import Blueprint, Response, g, render_template, request

from ..artifacts import (build_install_cmd, build_install_sh, build_readme,
                         read_s3_bytes, release_sha256, s3_chunks, zip_stream)
from ..selection import (PLATFORMS, SelectionError, validate_cost_center,
                         validate_platform, validate_selection)
from .common import (audit_denied, audit_success, cfg, ext, login_required,
                     session_is_admin)

bp = Blueprint("downloads", __name__, url_prefix="/portal")


def _render_form(session, cost_center=None, error=None, status=200,
                 platform=None):
    """Stage 1 (no cost_center): pick a cost center; portal.js upgrades this
    into the chained two-select form. Stage 2 (validated cost_center): pick
    one of ITS teams - the noscript round-trip path. `platform` preselects
    the platform choice so the noscript stage-1 pick (and the pick on an
    error re-render) is carried instead of silently resetting to windows;
    anything not a served platform falls back to the windows default."""
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
        platform=platform if platform in PLATFORMS else "windows",
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
                        status=200 if error is None else 400,
                        platform=request.args.get("platform"))


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
    platform = request.args.get("platform")
    try:
        team, cost_center = validate_selection(team, cost_center, c)
        platform = validate_platform(platform)
    except SelectionError as exc:
        audit_denied(email, groups, "invalid selection: %s" % exc,
                     team=team, cost_center=cost_center, platform=platform)
        # Back to stage 2 if the cost center itself was valid, else stage 1.
        try:
            page_cc = validate_cost_center(cost_center, c)
        except SelectionError:
            page_cc = None
        return _render_form(session, cost_center=page_cc, error=str(exc),
                            status=400, platform=platform)

    # The manifest, installer, and extra-CA reads happen BEFORE the response
    # starts, so a problem there is a rendered error page. The binary's
    # GetObject does NOT: s3_chunks is a generator, so it runs after headers
    # are sent and a failure aborts the stream mid-body (deliberate - the
    # chunked encoding makes the truncation detectable, and a 500 page must
    # never be written into a ZIP body).
    sha256 = release_sha256(s3, c, platform)
    readme = build_readme(
        c.gateway_url, c.release_version, sha256, team, cost_center,
        c.bundle_extra_ca, platform=platform,
    )
    if platform == "linux":
        # Scripts carry 0755 for unzippers that restore modes; the README's
        # `bash install.sh` instruction works either way.
        installer_bytes = read_s3_bytes(s3, c.artifacts_bucket, c.linux_installer_key)
        wrapper = build_install_sh(
            c.gateway_url, sha256, team, cost_center,
            c.disable_updates, c.bundle_extra_ca,
        )
        entries = [
            ("install-claude-code.sh", installer_bytes, 0o755),
            ("install.sh", wrapper, 0o755),
            ("README.txt", readme, 0o644),
        ]
        binary_mode = 0o755
    else:
        installer_bytes = read_s3_bytes(s3, c.artifacts_bucket, c.installer_key)
        install_cmd = build_install_cmd(
            c.gateway_url, sha256, team, cost_center,
            c.disable_updates, c.bundle_extra_ca,
        )
        entries = [
            ("Install-ClaudeCode.ps1", installer_bytes, 0o644),
            ("install.cmd", install_cmd, 0o644),
            ("README.txt", readme, 0o644),
        ]
        binary_mode = 0o644
    if c.bundle_extra_ca:
        entries.append(
            ("extra-ca.pem", read_s3_bytes(s3, c.artifacts_bucket, c.extra_ca_key), 0o644))
    binary_name = PLATFORMS[platform]["binary_name"]
    exe_key = "releases/%s/%s" % (c.release_version, binary_name)

    # Audit BEFORE streaming: a mid-stream client disconnect must not lose
    # the record of an authorized, validated download request.
    audit_success(email, groups, team, cost_center, sha256, platform=platform)

    # No Content-Length: gunicorn emits the body chunked (HTTP/1.1), so a
    # truncated download (S3 read error mid-stream, task recycle, ALB cut)
    # omits the terminating 0-chunk and the client DETECTS it. A failure
    # mid-generator aborts the connection - never a 500 page in the body.
    gen = zip_stream(
        s3_chunks(s3, c.artifacts_bucket, exe_key),
        entries, binary_name=binary_name, binary_mode=binary_mode,
    )
    resp = Response(gen, mimetype="application/zip")
    resp.headers["Content-Disposition"] = (
        'attachment; filename="claude-code-%s-%s.zip"'
        % (c.release_version, platform))
    return resp
