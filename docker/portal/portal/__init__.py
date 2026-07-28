"""Okta-secured Claude Code installer download portal (Flask package).

The portal:

  * runs the full OIDC authorization-code flow (state + PKCE + nonce) against
    the SAME Okta issuer the gateway uses, verifying the ID token's RS256
    signature against the issuer's JWKS in pure Python (no crypto dependency),
  * authorizes on Okta GROUP membership (a value the ALB's authenticate-oidc
    cannot evaluate - which is why auth lives in the app, not the listener),
  * streams a per-download installer ZIP (claude.exe STORED and streamed from
    the CMK-encrypted artifacts bucket, install.cmd baked with the selected
    Cost Center / Team, README, optional enterprise CA),
  * shows the signed-in user their spend quotas + usage, the user-guide PDF,
    and the gateway TLS certificate fingerprint, and
  * hosts the spend-cap admin pages (device-flow bearer: admins act as
    THEMSELVES; the portal holds no write-capable gateway key).

Design notes:
  * TLS terminates on the task (self-signed leaf baked into the image; the ALB
    re-encrypts and does not validate it) - SC-8 on the ALB->task hop.
  * Sessions are short-lived HMAC-signed HttpOnly Secure cookies; no
    server-side session store.
  * create_app() takes injectable collaborators so the test suite can fake
    OIDC / S3 / CloudWatch Logs / the gateway without sockets or AWS.
"""

import logging

from flask import Flask

from .audit import AuditLogger
from .config import Config
from .gateway import GatewayClient
from .oidc import OidcClient

log = logging.getLogger("portal")

# boto3 is only needed at runtime (S3 + CloudWatch Logs); importing lazily
# keeps the unit tests free of an AWS dependency when they inject fakes.
try:  # pragma: no cover - exercised in the container, faked in tests
    import boto3
except Exception:  # pragma: no cover
    boto3 = None


def create_app(config=None, *, oidc=None, gateway=None, s3=None, logs=None,
               audit=None):
    """Application factory.

    All keyword collaborators are injectable for tests:
      config  - a Config instance (default: read from os.environ, fail fast)
      oidc    - OidcClient (default: real one against config.issuer)
      gateway - GatewayClient (default: real one against config.gateway_url)
      s3      - boto3 S3 client (default: boto3.client("s3"))
      logs    - boto3 CloudWatch Logs client (default: boto3.client("logs"));
                only created when `audit` is not injected
      audit   - AuditLogger (default: AuditLogger(logs, config.audit_log_group))
    """
    cfg = config if config is not None else Config()

    app = Flask(__name__, static_url_path="/portal/static")
    # Old handler tolerated trailing slashes (path.rstrip("/")); keep that.
    app.url_map.strict_slashes = False
    # Same POST body cap as the old handler's _form() (65536 bytes).
    app.config["MAX_CONTENT_LENGTH"] = 65536

    if s3 is None:
        if boto3 is None:
            raise RuntimeError("boto3 is unavailable and no s3 client was injected")
        s3 = boto3.client("s3")
    if audit is None:
        if logs is None:
            if boto3 is None:
                raise RuntimeError("boto3 is unavailable and no logs client was injected")
            logs = boto3.client("logs")
        audit = AuditLogger(logs, cfg.audit_log_group)

    app.extensions["portal"] = {
        "config": cfg,
        "oidc": oidc if oidc is not None else OidcClient(cfg),
        "gateway": gateway if gateway is not None else GatewayClient(cfg),
        "s3": s3,
        "logs": logs,
        "audit": audit,
    }

    from .views import register_views
    register_views(app)
    return app
