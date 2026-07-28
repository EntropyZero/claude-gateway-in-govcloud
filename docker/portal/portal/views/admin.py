"""Spend-cap admin pages + the all-users usage table.

The portal group gate here is UX + defense in depth; the SECURITY boundary is
the gateway, which verifies the device-flow bearer token and re-checks the
token's groups claim against its own admin_groups on every call. Admins act
AS THEMSELVES (oidc:<sub> in the gateway's admin_audit); the portal holds no
write-capable gateway credential, and the read-only key is deliberately NOT
used on these pages - per-admin attribution stays intact.
"""

import time
import urllib.parse

from flask import Blueprint, g, make_response, redirect, render_template, request

from ..authz import is_authorized
from ..crypto import sign_cookie, verify_cookie
from ..gateway import (SPEND_PERIODS, GatewayError, build_gw_cookie,
                       build_spend_limit_body, gateway_token_sub)
from ..identity import lookup_principal_emails, record_principal_email
from ..money import AmountError, cents_str_to_display, parse_cents, percent_used
from ..selection import SelectionError
from .common import (audit_admin, cfg, clear_cookie, csrf_for, csrf_ok,
                     denied_page, ext, gw_cookie, pop_flash, set_cookie,
                     set_flash)

bp = Blueprint("admin", __name__, url_prefix="/portal")

_PAGE_SIZES = (20, 50, 100)


def _admin_gate():
    """(session, None) when the admin section may be used, else
    (None, response) - redirect/404/403 exactly as the old handler."""
    session = g.portal_session
    if not session:
        return None, redirect("/portal/login")
    c = cfg()
    if not c.admin_groups:
        # Feature disabled: indistinguishable from any other unknown path.
        return None, (render_template("error.html", title="Not found",
                                      message=None), 404)
    if not is_authorized(session.get("groups", []), c.admin_groups):
        audit_admin(session, "denied", "not in an admin group (%s)"
                    % ", ".join(c.admin_groups))
        return None, denied_page(session.get("email", ""))
    return session, None


def _connect_page(session, flash=None, status=200, clear_gw=False,
                  clear_flash=False, clear_gwdev=False):
    resp = make_response(render_template(
        "admin_connect.html", email=session.get("email", ""), is_admin=True,
        version=cfg().release_version,
        flash=flash, csrf=csrf_for(session)), status)
    if clear_gw:
        clear_cookie(resp, "portal_gw")
    if clear_gwdev:
        clear_cookie(resp, "portal_gwdev")
    if clear_flash:
        clear_cookie(resp, "portal_flash")
    return resp


# ---------------------------------------------------------------- caps page


@bp.get("/admin")
def admin():
    session, resp = _admin_gate()
    if resp is not None:
        return resp
    flash, had_flash = pop_flash()

    gw = gw_cookie()
    if gw:
        return _render_connected(session, gw, flash, had_flash)

    c = cfg()
    txn = verify_cookie(request.cookies.get("portal_gwdev", ""), c.session_secret)
    if txn:
        return _poll_device_flow(session, txn, had_flash)
    return _connect_page(session, flash, clear_flash=had_flash)


def _render_connected(session, gw, flash, had_flash):
    email = session.get("email", "")
    gateway = ext()["gateway"]
    try:
        status, doc = gateway.spend_api("GET", gw["tok"], path="?limit=200")
    except GatewayError as exc:
        return _connect_page(session, {"ok": False, "msg": str(exc)},
                             clear_flash=had_flash)
    if status == 401:
        # Gateway session expired: try the refresh token once, then fall
        # back to a fresh connect.
        refreshed = gateway.refresh(gw.get("rt", "")) if gw.get("rt") else None
        if refreshed:
            cookie, ttl = build_gw_cookie(refreshed, session, cfg().session_secret)
            if cookie:
                # Refreshed tokens re-assert the pairing too - a map object
                # deleted or never written at connect heals here.
                record_principal_email(ext()["s3"], cfg().artifacts_bucket,
                                       gateway_token_sub(refreshed), email)
                resp = redirect("/portal/admin")
                set_cookie(resp, "portal_gw", cookie, ttl)
                return resp
        return _connect_page(
            session,
            {"ok": False, "msg": "Your gateway session expired - connect again."},
            clear_gw=True, clear_flash=had_flash)
    if status == 403:
        # The PORTAL let them in but the GATEWAY refused: PORTAL_ADMIN_GROUP
        # and the gateway's SpendAdminGroups disagree. Surface it precisely.
        audit_admin(session, "denied", "gateway refused the admin call (403): "
                    "user is not in the gateway's SpendAdminGroups",
                    gw_sub=gw.get("sub", ""))
        return _connect_page(
            session,
            {"ok": False, "msg":
             "The gateway refused: your account is not in its spend-admin "
             "groups (SpendAdminGroups). Ask the platform team to align it "
             "with the portal's PORTAL_ADMIN_GROUP."},
            status=403, clear_flash=had_flash)
    if status != 200 or not isinstance(doc, dict):
        return _connect_page(
            session,
            {"ok": False, "msg": "Gateway error listing caps (HTTP %s)." % status},
            clear_flash=had_flash)
    resp = make_response(render_template(
        "admin_caps.html", email=email, is_admin=True,
        version=cfg().release_version,
        limits=doc.get("data", []),
        flash=flash, csrf=csrf_for(session), periods=SPEND_PERIODS,
        cents_to_display=cents_str_to_display))
    if had_flash:
        clear_cookie(resp, "portal_flash")
    return resp


def _poll_device_flow(session, txn, had_flash):
    c = cfg()
    gateway = ext()["gateway"]
    try:
        result = gateway.poll_token(txn["dc"])
    except GatewayError as exc:
        return _connect_page(session, {"ok": False, "msg": str(exc)},
                             clear_gwdev=True, clear_flash=had_flash)
    if result in ("pending", "slow_down"):
        interval = txn.get("int", 5)
        set_bumped = None
        if result == "slow_down":
            # RFC 8628 3.5: slow_down means add 5 seconds to the polling
            # interval for this and all subsequent requests. The interval
            # lives in the signed txn cookie, so re-sign it bumped.
            interval += 5
            bumped = dict(txn, int=interval)
            set_bumped = (sign_cookie(bumped, c.session_secret),
                          max(txn["exp"] - int(time.time()), 1))
        resp = make_response(render_template(
            "admin_pending.html", email=session.get("email", ""),
            is_admin=True, version=c.release_version, verify_url=txn["vu"],
            user_code=txn.get("uc", ""), refresh_seconds=interval + 1,
            csrf=csrf_for(session)))
        if set_bumped:
            set_cookie(resp, "portal_gwdev", set_bumped[0], set_bumped[1])
        if had_flash:
            clear_cookie(resp, "portal_flash")
        return resp
    # Granted. The cookie outlives neither the gateway token nor the
    # portal session (least privilege on both axes) - and respects the
    # browser's ~4KB per-cookie cap rather than being dropped silently.
    cookie, ttl = build_gw_cookie(result, session, c.session_secret)
    if not cookie:
        return _connect_page(
            session,
            {"ok": False, "msg":
             "Sign-in succeeded but the gateway session token is too large "
             "to store in a browser cookie (very large Okta groups claim). "
             "Use scripts/set-spend-limit.sh, or reduce the groups pushed "
             "into the token."},
            clear_gwdev=True, clear_flash=had_flash)
    # The ONE moment the portal holds both halves of the identity pairing:
    # the session's Okta-verified email and the gateway token's sub (the id
    # admin_audit will record). Persist it for the audit page's Email column.
    sub = gateway_token_sub(result)
    record_principal_email(ext()["s3"], c.artifacts_bucket, sub,
                           session.get("email", ""))
    audit_admin(session, "success", "gateway admin session connected",
                gw_sub=sub)
    resp = redirect("/portal/admin")
    set_cookie(resp, "portal_gw", cookie, ttl)
    clear_cookie(resp, "portal_gwdev")
    return resp


# ---------------------------------------------------------------- POSTs


@bp.post("/admin/connect")
def admin_connect():
    session, resp = _admin_gate()
    if resp is not None:
        return resp
    if not csrf_ok(session, request.form):
        return render_template("error.html", title="Invalid request token",
                               message=None), 403
    gateway = ext()["gateway"]
    try:
        doc = gateway.device_authorize()
    except GatewayError as exc:
        return _connect_page(session, {"ok": False, "msg": str(exc)})
    try:
        interval = max(2, min(int(doc.get("interval", 5)), 60))
    except (TypeError, ValueError):
        interval = 5
    try:
        expires_in = max(60, min(int(doc.get("expires_in", 600)), 1800))
    except (TypeError, ValueError):
        expires_in = 600
    txn = {
        "dc": doc["device_code"],
        "uc": str(doc.get("user_code", "")),
        "vu": str(doc.get("verification_uri_complete")
                  or doc.get("verification_uri") or cfg().gateway_url),
        "int": interval,
        "exp": int(time.time()) + expires_in,
    }
    cookie = sign_cookie(txn, cfg().session_secret)
    out = redirect("/portal/admin")
    set_cookie(out, "portal_gwdev", cookie, expires_in)
    return out


@bp.post("/admin/disconnect")
def admin_disconnect():
    session, resp = _admin_gate()
    if resp is not None:
        return resp
    if not csrf_ok(session, request.form):
        return render_template("error.html", title="Invalid request token",
                               message=None), 403
    out = redirect("/portal/admin")
    clear_cookie(out, "portal_gw")
    clear_cookie(out, "portal_gwdev")
    return out


@bp.post("/admin/set")
def admin_set():
    return _admin_mutate(clear=False)


@bp.post("/admin/clear")
def admin_clear():
    return _admin_mutate(clear=True)


def _admin_mutate(clear):
    session, resp = _admin_gate()
    if resp is not None:
        return resp
    form = request.form
    if not csrf_ok(session, form):
        return render_template("error.html", title="Invalid request token",
                               message=None), 403
    gw = gw_cookie()
    if not gw:
        # The signed gateway cookie expired between rendering the caps page
        # and this submit (its TTL is min(gateway token, portal session), so
        # this is reachable in normal use). Say so explicitly - a silent
        # bounce to the connect page reads as "change applied".
        out = redirect("/portal/admin")
        set_flash(out, False, "Your gateway session expired before the form "
                  "was submitted - the cap change was NOT applied. Connect "
                  "again and retry.")
        return out
    scope_type = form.get("scope_type", "")
    scope_id = form.get("scope_id", "").strip()
    period = form.get("period", "monthly")
    try:
        body = build_spend_limit_body(
            scope_type, scope_id, None if clear else form.get("amount", ""), period)
    except (SelectionError, AmountError) as exc:
        out = redirect("/portal/admin")
        set_flash(out, False, str(exc))
        return out

    action = "%s %s cap for %s (%s)" % (
        "clear" if clear else "set", scope_type, scope_id or "organization", period)
    gateway = ext()["gateway"]
    try:
        status, doc = gateway.spend_api("POST", gw["tok"], body=body)
    except GatewayError as exc:
        out = redirect("/portal/admin")
        set_flash(out, False, str(exc))
        return out
    if status in (200, 201):
        audit_admin(session, "success", action, gw_sub=gw.get("sub", ""))
        out = redirect("/portal/admin")
        set_flash(out, True, "Done: %s." % action)
        return out
    audit_admin(session, "denied", "%s -> gateway HTTP %s" % (action, status),
                gw_sub=gw.get("sub", ""))
    detail = ""
    if isinstance(doc, dict):
        err = doc.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message", ""))
        elif err:
            detail = str(err)
    # 401 falls through to the reconnect path on the next GET.
    out = redirect("/portal/admin")
    set_flash(out, False, "Gateway refused (%s HTTP %s). %s" % (action, status, detail))
    return out


# ---------------------------------------------------------------- audit trail


@bp.get("/admin/audit")
def admin_audit():
    session, resp = _admin_gate()
    if resp is not None:
        return resp
    gw = gw_cookie()
    if not gw:
        return redirect("/portal/admin")
    gateway = ext()["gateway"]
    try:
        status, doc = gateway.spend_api("GET", gw["tok"], path="/audit?limit=200")
    except GatewayError as exc:
        out = redirect("/portal/admin")
        set_flash(out, False, str(exc))
        return out
    if status != 200 or not isinstance(doc, dict):
        out = redirect("/portal/admin")
        set_flash(out, False,
                  "Gateway error fetching the audit trail (HTTP %s)." % status)
        return out
    # Actor is normalized to str here (a non-string actor in a malformed
    # gateway response would make the template's dict lookup throw); the
    # rest of the fields render through Jinja, which stringifies safely.
    events = []
    for ev in doc.get("data", []):
        if isinstance(ev, dict):
            ev = dict(ev)
            ev["actor"] = str(ev.get("actor") or "")
            events.append(ev)
    # Join each oidc:<sub> actor to the identity map captured at connect
    # time; actors without a map object (break-glass keys, admins who never
    # connected through the portal) render as a dash.
    actor_emails = lookup_principal_emails(
        ext()["s3"], cfg().artifacts_bucket,
        {ev["actor"] for ev in events})
    return render_template("admin_audit.html", email=session.get("email", ""),
                           is_admin=True, version=cfg().release_version,
                           events=events, actor_emails=actor_emails)


# ---------------------------------------------------------------- all users


def build_user_rows(items):
    """Effective-usage items -> table row dicts (one row per user PER the
    selected period). Money in Decimal cents; display rounding only."""
    rows = []
    for item in items or []:
        actor = item.get("actor") or {}
        scope = item.get("scope") or {}
        spend_raw = item.get("period_to_date_spend")
        cap_raw = item.get("amount")
        source = item.get("source") or {}
        stype = source.get("type")
        if stype == "user":
            source_label = "user cap"
        elif stype == "rbac_group":
            gid = source.get("rbac_group_id")
            source_label = "group cap (%s)" % gid if gid else "group cap"
        elif stype == "organization":
            source_label = "org cap"
        else:
            source_label = "no cap"
        spend = parse_cents(spend_raw if spend_raw is not None else "0")
        rows.append({
            "name": actor.get("name") or "",
            "email": actor.get("email_address") or "",
            "user_id": scope.get("user_id") or actor.get("user_id") or "",
            "groups": ", ".join(item.get("groups") or []),
            "cap_display": None if cap_raw is None else cents_str_to_display(cap_raw),
            "period": str(item.get("period", "")),
            "spend_display": cents_str_to_display(spend_raw if spend_raw is not None else "0"),
            # Raw Decimal cents for the client-side numeric column sort.
            "spend_sort": "" if spend is None else str(spend),
            "percent": None if cap_raw is None else percent_used(
                spend, parse_cents(cap_raw)),
            "source_label": source_label,
        })
    return rows


@bp.get("/admin/users")
def admin_users():
    session, resp = _admin_gate()
    if resp is not None:
        return resp
    gw = gw_cookie()
    if not gw:
        # Connect flow lives on /portal/admin; come back afterwards.
        out = redirect("/portal/admin")
        set_flash(out, False, "Connect a gateway session first, then open "
                  "the All-users page again.")
        return out

    # GET-form controls. The single period select means sort=spend_desc's
    # one-period requirement holds by construction.
    period = request.args.get("period", "monthly")
    if period not in SPEND_PERIODS:
        period = "monthly"
    q = request.args.get("q", "").strip()[:256]
    try:
        page_size = int(request.args.get("page_size", "20"))
    except ValueError:
        page_size = 20
    if page_size not in _PAGE_SIZES:
        page_size = 20
    sort_by_spend = request.args.get("sort") == "spend"
    page_token = request.args.get("page", "").strip()

    gateway = ext()["gateway"]
    try:
        status, doc = gateway.effective_usage(
            ("bearer", gw["tok"]),
            periods=[period],
            q=q or None,
            sort="spend_desc" if sort_by_spend else None,
            page=page_token or None,
            limit=page_size,
        )
    except GatewayError as exc:
        out = redirect("/portal/admin")
        set_flash(out, False, str(exc))
        return out
    if status == 401:
        # Same refresh-then-reconnect flow as /portal/admin.
        refreshed = gateway.refresh(gw.get("rt", "")) if gw.get("rt") else None
        if refreshed:
            cookie, ttl = build_gw_cookie(refreshed, session, cfg().session_secret)
            if cookie:
                record_principal_email(ext()["s3"], cfg().artifacts_bucket,
                                       gateway_token_sub(refreshed),
                                       session.get("email", ""))
                out = redirect(request.full_path if request.query_string
                               else "/portal/admin/users")
                set_cookie(out, "portal_gw", cookie, ttl)
                return out
        out = redirect("/portal/admin")
        set_flash(out, False, "Your gateway session expired - connect again.")
        clear_cookie(out, "portal_gw")
        return out
    if status == 403:
        audit_admin(session, "denied", "gateway refused the usage listing (403): "
                    "user is not in the gateway's SpendAdminGroups",
                    gw_sub=gw.get("sub", ""))
        return _connect_page(
            session,
            {"ok": False, "msg":
             "The gateway refused: your account is not in its spend-admin "
             "groups (SpendAdminGroups)."},
            status=403)
    if status != 200 or not isinstance(doc, dict):
        out = redirect("/portal/admin")
        set_flash(out, False, "Gateway error listing usage (HTTP %s)." % status)
        return out

    filters = {"period": period, "q": q, "page_size": page_size,
               "sort": "spend" if sort_by_spend else ""}
    base_params = {"period": period, "page_size": str(page_size)}
    if q:
        base_params["q"] = q
    if sort_by_spend:
        base_params["sort"] = "spend"
    next_page = doc.get("next_page")
    next_url = None
    if next_page:
        next_url = ("/portal/admin/users?"
                    + urllib.parse.urlencode(dict(base_params, page=next_page)))
    first_url = "/portal/admin/users?" + urllib.parse.urlencode(base_params)

    return render_template(
        "admin_users.html",
        email=session.get("email", ""),
        is_admin=True,
        version=cfg().release_version,
        rows=build_user_rows(doc.get("data", [])),
        filters=filters,
        periods=SPEND_PERIODS,
        page_sizes=_PAGE_SIZES,
        paged=bool(page_token),
        next_url=next_url,
        first_url=first_url,
    )
