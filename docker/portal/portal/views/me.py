"""/portal/me - the signed-in user's own spend quotas + period-to-date usage.

Server-side call with the deployment's READ-ONLY admin key (x-api-key),
scoped to user_ids[] = [session sub]. The read key never reaches the browser;
the page can only ever show the session's own sub. Feature-gated: an empty
SPEND_READ_KEY renders an explanatory page instead. (The shipped 04 imports
the 02 spend-read-key export unconditionally - deploying it against a
pre-export 02 fails at CloudFormation, not here - so this gate fires only
for hand-modified templates or local/test runs.)
"""

from flask import Blueprint, g, redirect, render_template

from ..gateway import GatewayError
from ..money import cents_str_to_display, parse_cents, percent_used
from .common import audit_denied, cfg, ext, login_required, session_is_admin

bp = Blueprint("me", __name__, url_prefix="/portal")

_PERIOD_ORDER = {"daily": 0, "weekly": 1, "monthly": 2}


def source_label(source):
    """Which scope a cap came from. `source` may be None (no cap applies)."""
    stype = (source or {}).get("type")
    if stype == "user":
        return "your user cap"
    if stype == "rbac_group":
        gid = (source or {}).get("rbac_group_id") or (source or {}).get("group")
        return "group cap (%s)" % gid if gid else "group cap"
    if stype == "organization":
        return "organization-wide cap"
    return None


def build_usage_rows(items):
    """API items -> template row dicts. All money math in Decimal cents
    (period_to_date_spend may be FRACTIONAL cents, e.g. '123.5'); rounding
    happens only at display time."""
    rows = []
    for item in sorted(items or [],
                       key=lambda i: _PERIOD_ORDER.get(i.get("period"), 9)):
        spend_raw = item.get("period_to_date_spend")
        cap_raw = item.get("amount")
        row = {
            "period": str(item.get("period", "")),
            "spend_display": cents_str_to_display(spend_raw if spend_raw is not None else "0"),
            "cap_display": None if cap_raw is None else cents_str_to_display(cap_raw),
            "has_cap": cap_raw is not None,
            "source_label": source_label(item.get("source")),
            "percent": None,
        }
        if cap_raw is not None:
            row["percent"] = percent_used(parse_cents(spend_raw or "0"),
                                          parse_cents(cap_raw))
        rows.append(row)
    return rows


@bp.get("/me")
@login_required
def me():
    session = g.portal_session
    c = cfg()
    email = session.get("email", "")
    is_admin = session_is_admin(session)
    common = dict(email=email, is_admin=is_admin, version=c.release_version)

    if not c.spend_read_key:
        # Feature not wired: explain instead of erroring. Enable = re-run
        # deploy-gateway.sh (02 exports the read-key ARN) then
        # deploy-download-portal.sh (04 injects SPEND_READ_KEY).
        return render_template("me.html", enabled=False, rows=None, error=None,
                               **common)

    sub = session.get("sub", "")
    if not sub:
        # Session predates the sub claim (minted by the previous portal
        # version): a fresh login fixes it.
        return redirect("/portal/login")

    gw = ext()["gateway"]
    try:
        status, doc = gw.effective_usage(("api_key", c.spend_read_key),
                                         user_ids=[sub])
    except GatewayError as exc:
        return render_template("me.html", enabled=True, rows=None,
                               error=str(exc), **common), 502
    if status == 401:
        # Read key rotated stale (02 re-run without a 04 re-deploy).
        audit_denied(email, session.get("groups", []),
                     "gateway refused the spend read key (401)",
                     event="portal_usage")
        return render_template(
            "me.html", enabled=True, rows=None,
            error="The gateway refused the portal's read-only spend key "
                  "(HTTP 401). The key has likely been rotated - re-deploy "
                  "the portal (04) so it picks up the current secret.",
            **common), 502
    if status != 200 or not isinstance(doc, dict):
        return render_template(
            "me.html", enabled=True, rows=None,
            error="Gateway error fetching your usage (HTTP %s)." % status,
            **common), 502

    return render_template("me.html", enabled=True,
                           rows=build_usage_rows(doc.get("data", [])),
                           error=None, **common)
