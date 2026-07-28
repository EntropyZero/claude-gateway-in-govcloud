"""Home page (cards) + health check + legacy two-step redirect."""

import urllib.parse

from flask import Blueprint, Response, g, redirect, render_template, request

from .common import cfg, login_required, session_can_grafana, session_is_admin

bp = Blueprint("home", __name__, url_prefix="/portal")


@bp.get("")
@login_required
def home():
    # Legacy compat: /portal?cost_center=... was the old stage-2 URL; the
    # download form moved to /portal/download-page, which keeps the two-step
    # flow as its noscript fallback.
    cc = request.args.get("cost_center")
    if cc is not None:
        return redirect("/portal/download-page?"
                        + urllib.parse.urlencode({"cost_center": cc}))
    session = g.portal_session
    c = cfg()
    return render_template(
        "home.html",
        email=session.get("email", ""),
        version=c.release_version,
        is_admin=session_is_admin(session),
        spend_self_enabled=bool(c.spend_read_key),
        show_grafana=session_can_grafana(session),
        grafana_url=c.grafana_url,
    )


@bp.get("/healthz")
def healthz():
    return Response("ok", mimetype="text/plain")
