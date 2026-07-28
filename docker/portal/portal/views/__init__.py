"""Blueprint registration, session loading, security headers, error pages."""

import logging

from flask import g, render_template, request

from ..crypto import verify_cookie

log = logging.getLogger("portal")

# CSP: no inline script/style anywhere - external first-party files only.
# Views that legitimately deviate (the guide viewer iframes our own PDF) set
# their own header; after_request only fills in the default.
_CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
        "img-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "object-src 'none'")


def register_views(app):
    cfg = app.extensions["portal"]["config"]

    @app.before_request
    def _load_session():
        raw = request.cookies.get("portal_session")
        g.portal_session = verify_cookie(raw, cfg.session_secret) if raw else None

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("Content-Security-Policy", _CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    @app.errorhandler(404)
    def _not_found(exc):
        return render_template("error.html", title="Not found", message=None), 404

    @app.errorhandler(500)
    def _server_error(exc):
        # Last-resort guard; never leak a stack trace. Mid-stream failures
        # never reach here: once a streamed body has begun, an exception
        # aborts the connection instead (detectable truncation).
        log.exception("unhandled error on %s", request.path)
        return render_template("error.html", title="Internal error", message=None), 500

    from .admin import bp as admin_bp
    from .auth import bp as auth_bp
    from .downloads import bp as downloads_bp
    from .fingerprint import bp as fingerprint_bp
    from .guide import bp as guide_bp
    from .home import bp as home_bp
    from .me import bp as me_bp

    for bp in (auth_bp, home_bp, downloads_bp, me_bp, guide_bp,
               fingerprint_bp, admin_bp):
        app.register_blueprint(bp)
