"""Shared view helpers: extension access, session/cookie handling, authz
gates, flash cookies, CSRF, audit wrappers."""

import hmac
import time
from functools import wraps

from flask import current_app, g, redirect, render_template, request

from ..audit import build_audit_record
from ..authz import is_authorized
from ..crypto import csrf_token, sign_cookie, verify_cookie


def ext():
    return current_app.extensions["portal"]


def cfg():
    return ext()["config"]


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.get("portal_session"):
            return redirect("/portal/login")
        return fn(*args, **kwargs)
    return wrapper


def session_is_admin(session):
    c = cfg()
    return bool(c.admin_groups) and is_authorized(session.get("groups", []), c.admin_groups)


def client_ip():
    # Behind the single ALB, the LAST X-Forwarded-For entry is the peer the
    # ALB itself saw (it appends the connection source) - trustworthy for
    # audit. The first entry is whatever the client sent and is spoofable.
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.remote_addr or ""


# ---------------------------------------------------------------- cookies
# All portal cookies: HttpOnly; Secure; SameSite=Lax; Path=/portal.


def set_cookie(resp, name, value, max_age):
    resp.set_cookie(name, value, max_age=max_age, path="/portal",
                    httponly=True, secure=True, samesite="Lax")


def clear_cookie(resp, name):
    resp.set_cookie(name, "", max_age=0, path="/portal",
                    httponly=True, secure=True, samesite="Lax")


def gw_cookie():
    raw = request.cookies.get("portal_gw")
    return verify_cookie(raw, cfg().session_secret) if raw else None


def set_flash(resp, ok, msg):
    cookie = sign_cookie({"ok": ok, "msg": msg, "exp": int(time.time()) + 60},
                         cfg().session_secret)
    set_cookie(resp, "portal_flash", cookie, 60)


def pop_flash():
    """Returns (flash_or_None, had_cookie). Callers clear the cookie on the
    response they send when had_cookie is True."""
    raw = request.cookies.get("portal_flash")
    flash = verify_cookie(raw, cfg().session_secret) if raw else None
    return flash, bool(raw)


# ---------------------------------------------------------------- CSRF


def csrf_for(session):
    return csrf_token(session, cfg().session_secret)


def csrf_ok(session, form):
    """Synchronizer-token check for every POST (see crypto.csrf_token: Lax
    does not protect against same-SITE sibling apps)."""
    return hmac.compare_digest(form.get("csrf", ""), csrf_for(session))


# ---------------------------------------------------------------- pages


def error_page(status, title, message_html=None, message=None):
    resp = render_template("error.html", title=title, message=message,
                           message_html=message_html)
    return resp, status


def denied_page(email):
    return render_template("denied.html", email=email), 403


# ---------------------------------------------------------------- audit


def audit_success(email, groups, team, cost_center, sha256):
    c = cfg()
    ext()["audit"].write(build_audit_record(
        "success", email, groups, team, cost_center,
        c.release_version, sha256, client_ip(),
        request.headers.get("User-Agent", ""),
    ))


def audit_denied(email, groups, reason, team=None, cost_center=None,
                 event="portal_download"):
    c = cfg()
    ext()["audit"].write(build_audit_record(
        "denied", email, groups, team, cost_center,
        c.release_version, None, client_ip(),
        request.headers.get("User-Agent", ""), reason=reason, event=event,
    ))


def audit_admin(session, outcome, reason):
    ext()["audit"].write(build_audit_record(
        outcome, session.get("email", ""), session.get("groups", []),
        None, None, None, None, client_ip(),
        request.headers.get("User-Agent", ""), reason=reason, event="portal_admin"))
