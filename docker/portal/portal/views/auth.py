"""OIDC login + callback. Ported from app.py; the session now also carries
the ID token's `sub` claim (the gateway spend principal, needed by
/portal/me)."""

import hmac
import logging
import secrets
import time

from flask import Blueprint, redirect, request

from ..authz import groups_from_claims, is_authorized
from ..crypto import JwtError, generate_pkce, sign_cookie, verify_cookie
from .common import (audit_denied, cfg, clear_cookie, denied_page, error_page,
                     ext, set_cookie)

log = logging.getLogger("portal")

bp = Blueprint("auth", __name__, url_prefix="/portal")


@bp.get("/login")
def login():
    c = cfg()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier, challenge = generate_pkce()
    txn = {
        "state": state,
        "nonce": nonce,
        "cv": verifier,
        "exp": int(time.time()) + c.transaction_ttl_seconds,
    }
    cookie = sign_cookie(txn, c.session_secret)
    url = ext()["oidc"].authorize_url(state, nonce, challenge)
    resp = redirect(url)
    set_cookie(resp, "portal_txn", cookie, c.transaction_ttl_seconds)
    return resp


@bp.get("/oauth/callback")
def callback():
    c = cfg()
    oidc = ext()["oidc"]
    txn = verify_cookie(request.cookies.get("portal_txn", ""), c.session_secret)
    if not txn:
        return error_page(
            400, "Login expired",
            message_html="Please <a href='/portal/login'>try again</a>.")
    # Okta returned an error (e.g. access_denied) instead of a code.
    if "error" in request.args:
        return error_page(
            400, "Sign-in failed",
            message=request.args.get("error_description") or request.args["error"])
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not code or not hmac.compare_digest(state, txn["state"]):
        return error_page(400, "Invalid sign-in state")

    token_resp = oidc.exchange_code(code, txn["cv"])
    id_token = token_resp.get("id_token")
    access_token = token_resp.get("access_token")
    if not id_token:
        return error_page(400, "Sign-in failed", message="No ID token returned.")
    try:
        claims = oidc.verify_id_token(id_token, txn["nonce"])
    except JwtError as exc:
        log.warning("id_token verification failed: %s", exc)
        return error_page(400, "Sign-in failed", message="Token verification failed.")

    userinfo = None
    groups = groups_from_claims(claims, None)
    if not groups and access_token:
        try:
            userinfo = oidc.userinfo(access_token)
            groups = groups_from_claims(claims, userinfo)
        except Exception as exc:
            log.warning("userinfo fetch failed: %s", exc)
    email = claims.get("email") or (userinfo or {}).get("email") or claims.get("sub", "")

    if not is_authorized(groups, c.access_groups):
        audit_denied(email, groups, "not in any access group (%s)"
                     % ", ".join(c.access_groups))
        body, status = denied_page(email)
        from flask import make_response
        resp = make_response(body, status)
        clear_cookie(resp, "portal_txn")
        return resp

    session = {
        "email": email,
        # The gateway spend principal is the Okta sub; /portal/me needs it.
        "sub": claims.get("sub", ""),
        "groups": groups,
        "exp": int(time.time()) + c.session_ttl_seconds,
    }
    session_cookie = sign_cookie(session, c.session_secret)
    resp = redirect("/portal")
    set_cookie(resp, "portal_session", session_cookie, c.session_ttl_seconds)
    clear_cookie(resp, "portal_txn")
    return resp
