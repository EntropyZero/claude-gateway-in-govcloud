"""/portal/fingerprint - the gateway TLS certificate fingerprint, for users
to compare at Claude Code's first-connect prompt."""

from flask import Blueprint, g, render_template

from ..fingerprint import get_fingerprint
from .common import cfg, login_required, session_is_admin

bp = Blueprint("fingerprint", __name__, url_prefix="/portal")


@bp.get("/fingerprint")
@login_required
def fingerprint():
    session = g.portal_session
    c = cfg()
    result = get_fingerprint(c.gateway_url)
    return render_template(
        "fingerprint.html",
        email=session.get("email", ""),
        is_admin=session_is_admin(session),
        version=c.release_version,
        result=result,
    )
