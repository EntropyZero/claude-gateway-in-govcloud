"""Runtime configuration, read from the environment once at startup.

Config errors are BOOT failures (KeyError / ValueError out of __init__): a
misconfigured portal must fail fast in ECS, never serve a silently-broken
page. gunicorn imports wsgi:application per worker, so a bad config kills the
worker boot and the task exits loudly.
"""

import os

from .selection import parse_cost_center_teams


def split_list(raw):
    return [x.strip() for x in raw.split(",") if x.strip()]


class Config:
    def __init__(self, env=None):
        env = env if env is not None else os.environ
        self.issuer = env["OIDC_ISSUER"].rstrip("/")
        self.client_id = env["OIDC_CLIENT_ID"]
        self.client_secret = env.get("OIDC_CLIENT_SECRET", "")
        self.session_secret = env.get("SESSION_SECRET", "")
        self.public_url = env["PUBLIC_URL"].rstrip("/")
        self.redirect_uri = self.public_url + "/portal/oauth/callback"
        # One or more Okta groups (comma-separated); a member of ANY is
        # allowed. A single group name is the common case and parses to a
        # one-element list. Empty is a misconfiguration that would deny
        # everyone - fail fast at boot rather than silently lock out.
        self.access_groups = split_list(env["ACCESS_GROUP"])
        if not self.access_groups:
            raise ValueError("ACCESS_GROUP must name at least one Okta group")
        # Okta group(s) whose members get the spend-cap admin pages. Empty
        # (the default) disables those pages entirely - unlike ACCESS_GROUP
        # this is an optional feature, not a lockout misconfiguration. The
        # gateway independently re-checks membership (its admin_groups) on
        # every call, so this gate is UX + defense in depth, not the security
        # boundary.
        self.admin_groups = split_list(env.get("PORTAL_ADMIN_GROUP", ""))
        # Session TTL is configured in hours (CFN parameter); transaction
        # cookie lifetime stays in seconds (short, internal).
        self.session_ttl_seconds = int(env.get("SESSION_TTL_HOURS", "8")) * 3600
        self.transaction_ttl_seconds = int(env.get("TRANSACTION_TTL_SECONDS", "600"))
        # Cost-center -> teams mapping driving the dependent dropdowns
        # (pick a cost center, then a team belonging to it). Format:
        #   "CC-1000:platform|data,CC-2000:security"
        # Malformed OR empty input is a boot failure, not a silently empty
        # dropdown that rejects every download (same fail-fast posture as
        # ACCESS_GROUP above).
        self.cost_center_teams = parse_cost_center_teams(
            env.get("PORTAL_COST_CENTER_TEAMS", ""))
        if not self.cost_center_teams:
            raise ValueError("PORTAL_COST_CENTER_TEAMS must map at least one "
                             "cost center to its teams")
        self.cost_centers = list(self.cost_center_teams)
        # Artifacts + release.
        self.artifacts_bucket = env["ARTIFACTS_BUCKET"]
        self.release_version = env["RELEASE_VERSION"]
        self.installer_key = env.get("INSTALLER_KEY", "Install-ClaudeCode.ps1")
        self.extra_ca_key = env.get("EXTRA_CA_KEY", "extra-ca.pem")
        self.bundle_extra_ca = env.get("BUNDLE_EXTRA_CA", "false") == "true"
        # Baked installer arguments.
        self.gateway_url = env["GATEWAY_URL"].rstrip("/")
        self.disable_updates = env.get("DISABLE_UPDATES", "true") == "true"
        # Audit.
        self.audit_log_group = env["AUDIT_LOG_GROUP"]
        # Read-only gateway spend-admin key (04 injects it from Secrets
        # Manager). OPTIONAL: empty disables the self-usage view on
        # /portal/me (feature-gated, not a boot failure). The shipped 04
        # imports the 02 spend-read-key export unconditionally, so a missing
        # export fails the 04 deploy at CloudFormation - the empty-key gate
        # only fires for hand-modified templates or local/test runs.
        # This key can LIST caps and spend only; it cannot mutate.
        self.spend_read_key = env.get("SPEND_READ_KEY", "").strip()
        # User-guide PDF object key in the artifacts bucket (published by
        # scripts/publish-portal-release.sh).
        self.user_guide_key = env.get("USER_GUIDE_KEY", "docs/user-manual.pdf")
        # TLS (baked into the image; overridable for tests). Consumed by
        # gunicorn.conf.py, kept here so the wiring is documented in one place.
        self.tls_cert = env.get("PORTAL_TLS_CERT", "/etc/portal/tls/server.crt")
        self.tls_key = env.get("PORTAL_TLS_KEY", "/etc/portal/tls/server.key")
        self.listen_port = int(env.get("PORTAL_PORT", "8080"))
