#!/usr/bin/env python3
"""Okta-secured Claude Code installer download portal.

A small, dependency-light HTTP service (stdlib + boto3 only) that:

  * runs the full OIDC authorization-code flow (state + PKCE + nonce) against
    the SAME Okta issuer the gateway uses, verifying the ID token's RS256
    signature against the issuer's JWKS in pure Python (no crypto dependency),
  * authorizes on Okta GROUP membership (a value the ALB's authenticate-oidc
    cannot evaluate - which is why auth lives in the app, not the listener),
  * renders a two-step server-side page - pick a Cost Center, then a Team
    belonging to it - from a cost-center->teams mapping in deployment config
    (no JavaScript: the dependent dropdown is a GET round-trip), and
  * streams a single ZIP per download - claude.exe (stored, streamed from the
    CMK-encrypted artifacts bucket), the unmodified Install-ClaudeCode.ps1, a
    generated install.cmd with the selected options baked in, a README, and an
    optional bundled enterprise CA - logging one audit line per download
    (including denials) to a dedicated CMK-encrypted CloudWatch log group.

Design notes:
  * TLS terminates on the task (self-signed leaf baked into the image, ALB
    re-encrypts and does not validate it) - the ALB->task hop is encrypted
    like the gateway and Grafana tasks (SC-8).
  * No refresh tokens are stored; the session is a short-lived HMAC-signed
    HttpOnly Secure cookie. Re-auth on expiry is fine for a download portal.
  * The core logic is factored into pure functions so it is unit-testable
    without a live socket or a real Okta (see tests/portal/).
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("portal")

# ---------------------------------------------------------------- config

# boto3 is only needed at runtime (S3 + CloudWatch Logs); importing lazily
# keeps the unit tests free of an AWS dependency when they inject fakes.
try:  # pragma: no cover - exercised in the container, faked in tests
    import boto3
except Exception:  # pragma: no cover
    boto3 = None

# Injectable clients (set in main(); tests substitute fakes / moto).
s3 = None
logs = None


class Config:
    """Runtime configuration, read from the environment once at startup."""

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
        self.access_groups = _split_list(env["ACCESS_GROUP"])
        if not self.access_groups:
            raise ValueError("ACCESS_GROUP must name at least one Okta group")
        # Okta group(s) whose members get the spend-cap admin page. Empty
        # (the default) disables the page entirely - unlike ACCESS_GROUP this
        # is an optional feature, not a lockout misconfiguration. The gateway
        # independently re-checks membership (its admin_groups) on every call,
        # so this gate is UX + defense in depth, not the security boundary.
        self.admin_groups = _split_list(env.get("PORTAL_ADMIN_GROUP", ""))
        # Session TTL is configured in hours (CFN parameter); transaction cookie
        # lifetime stays in seconds (short, internal).
        self.session_ttl_seconds = int(env.get("SESSION_TTL_HOURS", "8")) * 3600
        self.transaction_ttl_seconds = int(env.get("TRANSACTION_TTL_SECONDS", "600"))
        # Cost-center -> teams mapping driving the two-step dropdown flow
        # (pick a cost center, then a team belonging to it). Format:
        #   "CC-1000:platform|data,CC-2000:security"
        # Malformed OR empty input is a boot failure, not a silently empty
        # dropdown that rejects every download (same fail-fast posture as
        # ACCESS_GROUP above).
        self.cost_center_teams = _parse_cost_center_teams(
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
        # TLS (baked into the image; overridable for tests).
        self.tls_cert = env.get("PORTAL_TLS_CERT", "/etc/portal/tls/server.crt")
        self.tls_key = env.get("PORTAL_TLS_KEY", "/etc/portal/tls/server.key")
        self.listen_port = int(env.get("PORTAL_PORT", "8080"))


def _split_list(raw):
    return [x.strip() for x in raw.split(",") if x.strip()]


def _parse_cost_center_teams(raw):
    """Parse 'CC-1000:platform|data,CC-2000:security' into an ordered
    {cost_center: [teams]} dict. Every token must survive _clean_token (the
    installer's own argument rules) and the delimiters (: | ,) are reserved,
    so a malformed entry raises ValueError at boot rather than rendering a
    broken or empty dropdown."""
    mapping = {}
    for entry in _split_list(raw):
        cc, sep, teams_raw = entry.partition(":")
        cc = cc.strip()
        teams = [t.strip() for t in teams_raw.split("|") if t.strip()]
        if not sep or not cc or not teams:
            raise ValueError(
                "PORTAL_COST_CENTER_TEAMS entry %r must look like "
                "'<cost-center>:<team>|<team>'" % entry)
        for token in [cc] + teams:
            if not _clean_token(token) or ":" in token or "|" in token:
                raise ValueError(
                    "PORTAL_COST_CENTER_TEAMS value %r must have no spaces, "
                    "commas, colons or pipes" % token)
        if cc in mapping:
            raise ValueError("PORTAL_COST_CENTER_TEAMS lists cost center %r twice" % cc)
        if len(set(teams)) != len(teams):
            raise ValueError(
                "PORTAL_COST_CENTER_TEAMS lists a team twice under %r" % cc)
        mapping[cc] = teams
    return mapping


# ---------------------------------------------------------------- base64url


def b64url_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text):
    if isinstance(text, str):
        text = text.encode("ascii")
    pad = -len(text) % 4
    return base64.urlsafe_b64decode(text + b"=" * pad)


# ---------------------------------------------------------------- cookies
# A cookie is  base64url(json_payload) "." base64url(hmac_sha256(secret, p1)).
# Payloads always carry "exp" (unix seconds); verify checks the MAC in
# constant time and the expiry.


def sign_cookie(payload, secret):
    body = b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    mac = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return body + "." + b64url_encode(mac)


def verify_cookie(raw, secret, now=None):
    """Return the payload dict, or None if the MAC is bad or it has expired."""
    now = int(time.time()) if now is None else now
    if not raw or "." not in raw:
        return None
    body, _, sig = raw.partition(".")
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        got = b64url_decode(sig)
    except Exception:
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        payload = json.loads(b64url_decode(body))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < now:
        return None
    return payload


# ---------------------------------------------------------------- PKCE


def generate_pkce():
    """Return (code_verifier, code_challenge) for S256 PKCE (RFC 7636)."""
    verifier = b64url_encode(secrets.token_bytes(32))
    challenge = b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ---------------------------------------------------------------- RS256 verify
# Pure-Python RSASSA-PKCS1-v1_5 verification: public-key RSA is just modular
# exponentiation, which Python's built-in pow() does. No crypto dependency.

# DER-encoded DigestInfo prefix for SHA-256 (RFC 8017 section 9.2).
_SHA256_DIGESTINFO_PREFIX = bytes(
    [0x30, 0x31, 0x30, 0x0D, 0x06, 0x09, 0x60, 0x86, 0x48, 0x01, 0x65,
     0x03, 0x04, 0x02, 0x01, 0x05, 0x00, 0x04, 0x20]
)


def rsa_pkcs1v15_sha256_verify(n, e, signing_input, signature):
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        return False
    em = pow(sig_int, e, n).to_bytes(k, "big")
    digest = hashlib.sha256(signing_input).digest()
    t = _SHA256_DIGESTINFO_PREFIX + digest
    ps_len = k - len(t) - 3
    if ps_len < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * ps_len + b"\x00" + t
    return hmac.compare_digest(em, expected)


class JwtError(Exception):
    pass


def _jwk_to_rsa(jwk):
    n = int.from_bytes(b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(b64url_decode(jwk["e"]), "big")
    return n, e


def verify_jwt(token, jwks, issuer, audience, nonce=None, now=None, leeway=60):
    """Verify an Okta ID token: RS256 signature against the JWKS, then the
    iss / aud / exp / nonce claims. Returns the claims dict or raises JwtError.

    jwks is the parsed JWKS document ({"keys": [...]}).
    """
    now = int(time.time()) if now is None else now
    parts = token.split(".")
    if len(parts) != 3:
        raise JwtError("token is not a JWS compact serialization")
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(b64url_decode(header_b64))
        claims = json.loads(b64url_decode(payload_b64))
        signature = b64url_decode(sig_b64)
    except Exception as exc:
        raise JwtError("malformed token: %s" % exc)

    if header.get("alg") != "RS256":
        raise JwtError("unexpected alg %r (only RS256 accepted)" % header.get("alg"))
    kid = header.get("kid")
    key = _find_jwk(jwks, kid)
    if key is None:
        raise JwtError("no JWKS key matches kid %r" % kid)
    n, e = _jwk_to_rsa(key)
    signing_input = (header_b64 + "." + payload_b64).encode("ascii")
    if not rsa_pkcs1v15_sha256_verify(n, e, signing_input, signature):
        raise JwtError("bad signature")

    if claims.get("iss") != issuer:
        raise JwtError("iss mismatch: %r != %r" % (claims.get("iss"), issuer))
    aud = claims.get("aud")
    aud_ok = audience == aud or (isinstance(aud, list) and audience in aud)
    if not aud_ok:
        raise JwtError("aud mismatch: %r does not contain %r" % (aud, audience))
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or now > exp + leeway:
        raise JwtError("token expired")
    if nonce is not None and claims.get("nonce") != nonce:
        raise JwtError("nonce mismatch")
    return claims


def _find_jwk(jwks, kid):
    for key in jwks.get("keys", []):
        # Signing keys only: skip an enc key that ever shared a kid.
        if key.get("kid") == kid and key.get("kty") == "RSA" and key.get("use", "sig") != "enc":
            return key
    return None


# ---------------------------------------------------------------- OIDC client


class OidcClient:
    """Discovery + token exchange + JWKS + userinfo. Network methods are thin
    so tests can override them; verify_id_token runs the real crypto."""

    # Minimum seconds between forced JWKS refetches. Okta rotates signing keys
    # a few times a year and pre-publishes the next key, so an unknown kid is
    # rare; throttling the forced refetch stops a flood of forged tokens
    # carrying random kids from turning verification into an unauthenticated
    # outbound-request amplifier (Okta's own JWKS-caching guidance).
    _min_refetch_interval = 300

    def __init__(self, config):
        self.config = config
        self._discovery = None
        self._jwks = None
        self._jwks_fetched_at = 0.0

    # -- network primitives (overridable in tests) --
    def _http_get_json(self, url, headers=None):
        return _http_json("GET", url, headers=headers)

    def _http_post_form(self, url, data, headers=None):
        body = urllib.parse.urlencode(data).encode("ascii")
        return _http_json("POST", url, body=body, headers=headers)

    # -- discovery + keys --
    def discovery(self):
        if self._discovery is None:
            url = self.config.issuer + "/.well-known/openid-configuration"
            self._discovery = self._http_get_json(url)
            # Okta's discovery 'issuer' is authoritative for token validation.
            if self._discovery.get("issuer"):
                self.config.issuer = self._discovery["issuer"].rstrip("/")
        return self._discovery

    def jwks(self, force=False):
        now = time.time()
        if self._jwks is None or (force and now - self._jwks_fetched_at >= self._min_refetch_interval):
            self._jwks = self._http_get_json(self.discovery()["jwks_uri"])
            self._jwks_fetched_at = now
        return self._jwks

    # -- flow --
    def authorize_url(self, state, nonce, code_challenge):
        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "scope": "openid profile email groups",
            "redirect_uri": self.config.redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return self.discovery()["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)

    def exchange_code(self, code, code_verifier):
        auth = base64.b64encode(
            ("%s:%s" % (self.config.client_id, self.config.client_secret)).encode("utf-8")
        ).decode("ascii")
        return self._http_post_form(
            self.discovery()["token_endpoint"],
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
                "code_verifier": code_verifier,
            },
            headers={"Authorization": "Basic " + auth},
        )

    def userinfo(self, access_token):
        return self._http_get_json(
            self.discovery()["userinfo_endpoint"],
            headers={"Authorization": "Bearer " + access_token},
        )

    def verify_id_token(self, id_token, nonce):
        """Verify signature+claims, refetching the JWKS once on an unknown kid
        (handles Okta signing-key rotation without a restart)."""
        try:
            return verify_jwt(
                id_token, self.jwks(), self.config.issuer, self.config.client_id, nonce
            )
        except JwtError as exc:
            if "no JWKS key matches kid" in str(exc):
                return verify_jwt(
                    id_token,
                    self.jwks(force=True),
                    self.config.issuer,
                    self.config.client_id,
                    nonce,
                )
            raise


def _http_json(method, url, body=None, headers=None):  # pragma: no cover - network
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    # urllib honors HTTPS_PROXY/NO_PROXY from the environment via the default
    # opener's ProxyHandler; the image's trust store carries the enterprise CA.
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- groups / authz


def groups_from_claims(id_claims, userinfo_claims):
    """Union the 'groups' claim from the ID token and (userinfo fallback) the
    userinfo response. Okta may deliver groups in either depending on the
    authorization server's claim config - mirror the gateway's
    userinfo_fallback: check both."""
    out = []
    for source in (id_claims or {}, userinfo_claims or {}):
        g = source.get("groups")
        if isinstance(g, str):
            g = [g]
        if isinstance(g, list):
            for item in g:
                if item not in out:
                    out.append(item)
    return out


def is_authorized(groups, access_groups):
    """True if the user belongs to ANY of the configured access groups.

    access_groups is a list; a bare string is coerced to a single-group list
    so callers (and tests) passing one group name still work - and never fall
    into set("name") iterating characters.
    """
    if isinstance(access_groups, str):
        access_groups = [access_groups]
    user = set(groups or [])
    return any(g in user for g in access_groups)


# ---------------------------------------------------------------- selection


class SelectionError(Exception):
    pass


# Mirrors Install-ClaudeCode.ps1's ValidatePattern('^[^,\s]*$'): a value that
# would break OTEL_RESOURCE_ATTRIBUTES parsing or the install.cmd argument.
def _clean_token(value):
    return value != "" and not any(c.isspace() for c in value) and "," not in value


def validate_cost_center(cost_center, config):
    """Reject anything not a configured cost center (and, defensively,
    anything with whitespace/commas). Returns cost_center or raises."""
    if cost_center is None:
        raise SelectionError("cost_center is required")
    if not _clean_token(cost_center):
        raise SelectionError("cost_center must not contain spaces or commas")
    if cost_center not in config.cost_center_teams:
        raise SelectionError("cost_center %r is not an allowed value" % cost_center)
    return cost_center


def validate_selection(team, cost_center, config):
    """Reject anything not in the configured mapping - the team must belong
    to the selected cost center, not merely appear somewhere in the config.
    Returns (team, cost_center) or raises."""
    if team is None or cost_center is None:
        raise SelectionError("both team and cost_center are required")
    cost_center = validate_cost_center(cost_center, config)
    if not _clean_token(team):
        raise SelectionError("team must not contain spaces or commas")
    if team not in config.cost_center_teams[cost_center]:
        raise SelectionError("team %r is not an allowed value for cost center %r"
                             % (team, cost_center))
    return team, cost_center


# ---------------------------------------------------------------- money

# The gateway's spend API takes whole CENTS as a decimal STRING (^\d{1,18}$).
# EXACT integer arithmetic, mirroring common.sh's dollars_to_cents - float
# round-trips put 0.05 on 6 cents, a money bug this repo has already had once.


class AmountError(Exception):
    pass


def dollars_to_cents(amount):
    """'50', '50.5', '50.05' -> '5000'/'5050'/'5005'. Raises AmountError on
    anything that is not a plain non-negative dollar figure with at most two
    decimal places (never rounds money)."""
    amount = (amount or "").strip()
    if not amount or amount.strip(".") == "" or amount.count(".") > 1 \
            or any(c not in "0123456789." for c in amount):
        raise AmountError("amount must be a plain dollar figure, e.g. 50 or 50.00")
    dollars, _, frac = amount.partition(".")
    if len(frac) > 2:
        raise AmountError("amount has more than 2 decimal places")
    cents = int(dollars or "0") * 100 + int((frac + "00")[:2])
    if cents <= 0:
        raise AmountError("amount must be greater than zero")
    if len(str(cents)) > 18:
        raise AmountError("amount is too large")
    return str(cents)


def cents_to_dollars(cents):
    """'5005' -> '$50.05' for display. Anything non-numeric renders verbatim
    (defensive: the value comes from the gateway API)."""
    s = str(cents)
    if not s.isdigit():
        return s
    return "$%d.%02d" % (int(s) // 100, int(s) % 100)


_SPEND_SCOPES = ("user", "rbac_group", "organization")
_SPEND_PERIODS = ("daily", "weekly", "monthly")


def build_spend_limit_body(scope_type, scope_id, amount, period):
    """The POST body for the gateway's spend-limits API (the same shape
    scripts/set-spend-limit.sh sends). amount None clears the cap; otherwise
    it is a DOLLAR string, converted to the API's cents-as-string. Raises
    SelectionError / AmountError with a user-renderable message."""
    if scope_type not in _SPEND_SCOPES:
        raise SelectionError("scope must be one of: %s" % ", ".join(_SPEND_SCOPES))
    if period not in _SPEND_PERIODS:
        raise SelectionError("period must be one of: %s" % ", ".join(_SPEND_PERIODS))
    if scope_type == "organization":
        if scope_id:
            raise SelectionError("the organization scope takes no user/group id")
        scope = {"type": "organization"}
    else:
        if not scope_id or len(scope_id) > 320 or any(ord(c) < 32 for c in scope_id):
            raise SelectionError("a %s cap needs a plain user/group id" % scope_type)
        key = "user_id" if scope_type == "user" else "rbac_group_id"
        scope = {"type": scope_type, key: scope_id}
    cents = None if amount is None else dollars_to_cents(amount)
    return {"scope": scope, "amount": cents, "period": period, "currency": "USD"}


# ---------------------------------------------------------------- gateway client
# Spend-cap admin against the gateway, acting AS the signed-in admin. The
# portal deliberately holds NO gateway admin API key: each admin obtains their
# own gateway session token through the gateway's OAuth 2.0 device flow
# (RFC 8628 - the same endpoints Claude Code itself signs in with), and the
# gateway authorizes each call by the token's Okta groups claim against its
# admin_groups config, recording the individual (oidc:<sub>) in admin_audit.


class GatewayError(Exception):
    """A gateway call failed in a way the UI should surface."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects: urllib's default handler forwards ALL
    request headers - including Authorization: Bearer - to the Location
    target with no same-host check, so a redirect (e.g. a later ALB
    listener-rule change) would replay the admin's gateway token to an
    arbitrary host. A 3xx from the gateway is returned as its status and
    treated as an unexpected response instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayClient:
    """Device flow + spend-limits API. Network methods are thin so tests can
    override them, mirroring OidcClient."""

    def __init__(self, config):
        self.config = config
        self.base = config.gateway_url

    # -- network primitive (overridable in tests) --
    def _http(self, method, url, headers=None, body=None):  # pragma: no cover - network
        """Return (status, parsed-json-or-None). 4xx/5xx do not raise: device
        flow and admin errors arrive as JSON bodies on 4xx responses."""
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Accept", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        ctx = ssl.create_default_context()
        opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPSHandler(context=ctx))
        try:
            with opener.open(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8"))
            except Exception:
                return exc.code, None
        except urllib.error.URLError as exc:
            # Network / TLS failure, not an HTTP status. Surfaced as a
            # GatewayError so the UI shows a diagnosable message instead of a
            # bare 500. A TLS verify error here means the container trust
            # store lacks the gateway ALB cert's chain - build-and-push-portal
            # stages GATEWAY_CA_BUNDLE/EXTRA_CA_CERT_PATH into the image.
            raise GatewayError("gateway unreachable: %s" % getattr(exc, "reason", exc))

    def _post_form(self, url, data):
        body = urllib.parse.urlencode(data).encode("ascii")
        return self._http("POST", url, headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body)

    # -- device flow --
    def device_authorize(self):
        """Start the device flow. Returns the RFC 8628 authorization response
        (device_code, user_code, verification_uri[_complete], expires_in,
        interval). The empty body (no client_id) is gateway-specific behavior,
        runtime-verified on 2.1.220 - RFC 8628 nominally REQUIRES client_id
        for public clients, so revisit if a gateway upgrade starts rejecting
        this call."""
        status, doc = self._post_form(self.base + "/oauth/device_authorization", {})
        if status != 200 or not isinstance(doc, dict) or "device_code" not in doc:
            raise GatewayError("device authorization failed (HTTP %s)" % status)
        return doc

    def poll_token(self, device_code):
        """One token poll. Returns the token response dict on success,
        'pending' while authorization is outstanding, 'slow_down' when the
        gateway asks for backoff (RFC 8628: add 5s to the interval), or raises
        GatewayError when the grant is dead (expired/denied)."""
        status, doc = self._post_form(self.base + "/oauth/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        })
        if status == 200 and isinstance(doc, dict) and doc.get("access_token"):
            return doc
        err = (doc or {}).get("error", "")
        if err == "authorization_pending":
            return "pending"
        if err == "slow_down":
            return "slow_down"
        raise GatewayError("gateway sign-in %s" % (err or "failed (HTTP %s)" % status))

    def refresh(self, refresh_token):
        """Exchange a refresh token; returns the new token response or None
        (callers fall back to a fresh device-flow connect - including on a
        network failure, so a gateway blip degrades to a reconnect button)."""
        try:
            status, doc = self._post_form(self.base + "/oauth/token", {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            })
        except GatewayError:
            return None
        if status == 200 and isinstance(doc, dict) and doc.get("access_token"):
            return doc
        return None

    # -- spend-limits admin API (bearer = the admin's own gateway token) --
    def spend_api(self, method, token, path="", body=None):
        headers = {"Authorization": "Bearer " + token}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        url = self.base + "/v1/organizations/spend_limits" + path
        return self._http(method, url, headers=headers, body=data)


def csrf_token(session, secret):
    """Deterministic per-session CSRF token for the admin POST forms.

    SameSite=Lax blocks cross-SITE posts, but "site" is the registrable
    domain: every sibling app under the corporate domain is SAME-site, so Lax
    alone does not protect admin mutations from a compromised internal page.
    This synchronizer token closes that: it is embedded as a hidden form
    field and required by every admin POST. A same-site attacker can SEND
    requests with the victim's cookies but cannot READ responses (same-origin
    policy), so it cannot learn the token; without the secret it cannot
    forge one."""
    msg = "csrf|%s|%s" % (session.get("email", ""), session.get("exp", 0))
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


# Signed-cookie budget for the gateway token. Browsers enforce ~4096 bytes
# per cookie (RFC 6265 minimum) and DROP an oversized Set-Cookie silently -
# which would loop the admin back to the connect page with no error after a
# SUCCESSFUL device grant. Gateway session JWTs embed the Okta groups claim,
# so users with very many groups can genuinely hit this.
_GW_COOKIE_BUDGET = 3800


def build_gw_cookie(token_response, session, secret, now=None):
    """The signed portal_gw cookie value for a token response, honoring the
    browser cookie cap: past the budget the refresh token is dropped first
    (re-connect on expiry is fine), then None is returned so the caller can
    render an explicit error instead of letting the browser discard the
    cookie silently. Returns (cookie, ttl_seconds) or (None, 0)."""
    now = int(time.time()) if now is None else now
    exp = min(gateway_token_exp(token_response, now=now), session["exp"])
    for rt in (token_response.get("refresh_token", ""), ""):
        cookie = sign_cookie(
            {"tok": token_response["access_token"], "rt": rt, "exp": exp}, secret)
        if len(cookie) <= _GW_COOKIE_BUDGET:
            return cookie, max(exp - now, 1)
    return None, 0


def gateway_token_exp(token_response, now=None):
    """Cookie expiry for a device-flow token: the JWT's own exp claim when it
    parses (the gateway session token is a JWS), else now+expires_in, else a
    conservative 15 minutes. The payload is NOT verified - the portal cannot
    (HS256, gateway-held key) and need not: the gateway re-verifies on every
    call; this only sizes the cookie lifetime."""
    now = int(time.time()) if now is None else now
    tok = token_response.get("access_token", "")
    parts = tok.split(".")
    if len(parts) == 3:
        try:
            exp = json.loads(b64url_decode(parts[1])).get("exp")
            if isinstance(exp, int):
                return exp
        except Exception:
            pass
    expires_in = token_response.get("expires_in")
    if isinstance(expires_in, int) and expires_in > 0:
        return now + expires_in
    return now + 900


# ---------------------------------------------------------------- artifacts


def build_install_cmd(gateway_url, sha256, team, cost_center, disable_updates, bundle_extra_ca):
    """Generate the one-double-click install.cmd wrapper. Windows batch; the
    caller's dropdown selections and the deployment's baked settings become
    Install-ClaudeCode.ps1 arguments."""
    lines = [
        "@echo off",
        "setlocal",
        "rem Claude Code installer - options baked in by the download portal.",
        'set "HERE=%~dp0"',
    ]
    ca_arg = ""
    if bundle_extra_ca:
        # The bundled PEM must live at a STABLE path (the extracted folder is
        # transient); copy it next to the binary, then point the installer there.
        lines += [
            'set "CADEST=%USERPROFILE%\\.local\\bin\\claude-extra-ca.pem"',
            'if exist "%HERE%extra-ca.pem" (',
            '  if not exist "%USERPROFILE%\\.local\\bin" mkdir "%USERPROFILE%\\.local\\bin"',
            '  copy /Y "%HERE%extra-ca.pem" "%CADEST%" >nul',
            ")",
        ]
        ca_arg = ' -ExtraCaCertPath "%CADEST%"'
    args = [
        '-BinaryPath "%HERE%claude.exe"',
        "-Sha256 %s" % sha256,
        '-GatewayUrl "%s"' % gateway_url,
        '-Team "%s"' % team,
        '-CostCenter "%s"' % cost_center,
    ]
    if disable_updates:
        args.append("-DisableUpdates")
    cmd = (
        'powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%Install-ClaudeCode.ps1" '
        + " ".join(args)
        + ca_arg
    )
    lines += [
        "",
        cmd,
        "",
        "if %ERRORLEVEL% NEQ 0 echo Install failed with code %ERRORLEVEL%.",
        "pause",
    ]
    return "\r\n".join(lines) + "\r\n"


def build_readme(gateway_url, version, sha256, team, cost_center, bundle_extra_ca):
    ca_note = (
        "  - extra-ca.pem      : your enterprise/TLS-inspection root CA; install.cmd\n"
        "                        copies it beside claude.exe and trusts it.\n"
        if bundle_extra_ca
        else ""
    )
    return (
        "Claude Code - offline install package\r\n"
        "=====================================\r\n\r\n"
        "Version:      %s\r\n"
        "Gateway:      %s\r\n"
        "Team:         %s\r\n"
        "Cost center:  %s\r\n"
        "claude.exe SHA-256:\r\n  %s\r\n\r\n"
        "To install: double-click install.cmd and follow the prompts.\r\n"
        "No administrator rights are needed: it installs claude.exe to\r\n"
        "%%USERPROFILE%%\\.local\\bin, verifies the SHA-256 and Anthropic's\r\n"
        "Authenticode signature, and writes your telemetry tags and update\r\n"
        "lockdown into your user Claude settings.\r\n\r\n"
        "Package contents:\r\n"
        "  - claude.exe            : the Claude Code binary (win32-x64).\r\n"
        "  - Install-ClaudeCode.ps1: the installer (unmodified).\r\n"
        "  - install.cmd           : runs the installer with your options.\r\n"
        "%s"
        "\r\nSigning in to the gateway:\r\n"
        "  Gateway login needs a one-time policy setting from your IT team,\r\n"
        "  delivered by group policy / MDM - the 'Cloud gateway' login does\r\n"
        "  not appear without it. (Gateway URL, for reference: %s)\r\n"
        "  Once that policy is in place: open a NEW terminal and run  claude .\r\n"
        "  It opens the pre-filled gateway login (no menu, no URL to type; press\r\n"
        "  Enter to connect), then your browser for a one-time sign-in. Confirm\r\n"
        "  the gateway certificate fingerprint with IT at the first-connect prompt.\r\n"
        % (version, gateway_url, team, cost_center, sha256, ca_note, gateway_url)
    )


class ChunkedWriter:
    """HTTP/1.1 chunked-transfer-encoding wrapper around an unseekable response
    stream. Chunked (vs a close-delimited body) makes a truncated download
    DETECTABLE: a premature disconnect omits the terminating 0-length chunk, so
    the client sees an error instead of a silently-corrupt file. zipfile writes
    through this; each write() frames one chunk (empty writes are dropped so
    they never emit the terminator early)."""

    def __init__(self, raw):
        self.raw = raw

    def write(self, data):
        n = len(data)
        if n == 0:
            return 0
        self.raw.write(b"%X\r\n" % n)
        self.raw.write(data)
        self.raw.write(b"\r\n")
        return n

    def flush(self):
        self.raw.flush()

    def close(self):
        self.raw.write(b"0\r\n\r\n")
        self.raw.flush()

    def seekable(self):
        return False

    def tell(self):
        raise OSError("chunked stream is not seekable")

    def seek(self, *a):
        raise OSError("chunked stream is not seekable")


def stream_zip(out, exe_chunks, installer_bytes, install_cmd, readme, extra_ca_bytes=None):
    """Write the download ZIP to the file-like `out` (may be an unseekable HTTP
    response stream). claude.exe is STORED (already compressed) and streamed
    chunk-by-chunk from `exe_chunks` (an iterable of bytes) so the whole binary
    is never held in memory."""
    with zipfile.ZipFile(out, "w") as zf:
        # Stored, streamed: ZipFile.open(...,'w') computes the CRC as it writes
        # and emits a data descriptor, so it works on an unseekable stream.
        info = zipfile.ZipInfo("claude.exe")
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o644 << 16
        with zf.open(info, "w") as dest:
            for chunk in exe_chunks:
                if chunk:
                    dest.write(chunk)
        zf.writestr("Install-ClaudeCode.ps1", installer_bytes)
        zf.writestr("install.cmd", install_cmd)
        zf.writestr("README.txt", readme)
        if extra_ca_bytes is not None:
            zf.writestr("extra-ca.pem", extra_ca_bytes)


# ---------------------------------------------------------------- audit


def build_audit_record(outcome, user_email, user_groups, team, cost_center,
                       version, sha256, source_ip, user_agent, reason=None,
                       event="portal_download"):
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "outcome": outcome,
        "user_email": user_email,
        "user_groups": user_groups,
        "team": team,
        "cost_center": cost_center,
        "version": version,
        "exe_sha256": sha256,
        "source_ip": source_ip,
        "user_agent": user_agent,
    }
    if reason:
        rec["reason"] = reason
    return rec


class AuditLogger:
    """Writes one JSON line per event to the dedicated CloudWatch log group.
    PutLogEvents no longer requires a sequence token (accepted, ignored)."""

    def __init__(self, logs_client, log_group):
        self.logs = logs_client
        self.log_group = log_group
        self.stream = "portal-%s-%d" % (socket.gethostname(), int(time.time()))
        self._ensure_stream()

    def _ensure_stream(self):
        try:
            self.logs.create_log_stream(
                logGroupName=self.log_group, logStreamName=self.stream
            )
        except Exception as exc:  # ResourceAlreadyExists or transient
            log.debug("create_log_stream: %s", exc)

    def write(self, record):
        try:
            self.logs.put_log_events(
                logGroupName=self.log_group,
                logStreamName=self.stream,
                logEvents=[{
                    "timestamp": int(time.time() * 1000),
                    "message": json.dumps(record, separators=(",", ":")),
                }],
            )
        except Exception as exc:  # never let audit failure abort a request path
            log.error("audit write failed: %s", exc)


# ---------------------------------------------------------------- S3 helpers


def read_s3_bytes(bucket, key):
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def s3_chunks(bucket, key, chunk_size=1024 * 1024):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    while True:
        chunk = body.read(chunk_size)
        if not chunk:
            break
        yield chunk


def release_sha256(config):
    """The win32-x64 SHA-256 from the published manifest.json - reusing the
    verified mirror output, never trusting a value from the client."""
    key = "releases/%s/manifest.json" % config.release_version
    manifest = json.loads(read_s3_bytes(config.artifacts_bucket, key))
    return manifest["platforms"]["win32-x64"]["checksum"]


# ---------------------------------------------------------------- HTML


# The Team dropdown depends on the Cost Center pick, and this page ships no
# JavaScript (the CSP has no script-src, and that stays true) - so the
# dependency is a two-step server round-trip: stage 1 submits the cost center
# back to /portal via GET, stage 2 renders only that cost center's teams.
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code download</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;color:#1a1a1a}}
 h1{{font-size:1.4rem}} label{{display:block;margin:1rem 0 .25rem;font-weight:600}}
 select,button{{font-size:1rem;padding:.5rem;width:100%;box-sizing:border-box}}
 button{{margin-top:1.5rem;background:#0b5;color:#fff;border:0;border-radius:.35rem;cursor:pointer}}
 .who{{color:#555;font-size:.85rem;margin-bottom:1.5rem}}
 .err{{background:#fee;border:1px solid #e99;padding:.75rem;border-radius:.35rem}}
 .cc{{margin:1rem 0 0}} .cc a{{font-size:.85rem;font-weight:400;margin-left:.5rem}}
</style></head><body>
<h1>Claude Code installer</h1>
<p class="who">Signed in as {email}. Version {version}.{admin_link}</p>
{error}
{form}
</body></html>"""

_STAGE1_FORM = """<form method="GET" action="/portal">
 <label for="cost_center">Cost center</label>
 <select id="cost_center" name="cost_center" required>{cost_centers}</select>
 <button type="submit">Continue</button>
</form>"""

_STAGE2_FORM = """<p class="cc"><strong>Cost center:</strong> {cost_center}\
 <a href="/portal">(change)</a></p>
<form method="GET" action="/portal/download">
 <input type="hidden" name="cost_center" value="{cost_center}">
 <label for="team">Team</label>
 <select id="team" name="team" required>{teams}</select>
 <button type="submit">Download pre-configured installer</button>
</form>"""


def _options(values):
    return "".join('<option value="%s">%s</option>' % (html.escape(v), html.escape(v)) for v in values)


def render_page(config, email, error=None, is_admin=False, cost_center=None):
    """Stage 1 (no cost_center): pick a cost center. Stage 2 (cost_center is a
    validated configured value): pick one of ITS teams and download."""
    if cost_center is None:
        form = _STAGE1_FORM.format(cost_centers=_options(config.cost_centers))
    else:
        form = _STAGE2_FORM.format(
            cost_center=html.escape(cost_center),
            teams=_options(config.cost_center_teams[cost_center]),
        )
    err_html = '<p class="err">%s</p>' % html.escape(error) if error else ""
    return _PAGE.format(
        email=html.escape(email),
        version=html.escape(config.release_version),
        error=err_html,
        form=form,
        admin_link=' <a href="/portal/admin">Spend-cap admin</a>' if is_admin else "",
    )


# -- spend-cap admin pages. Server-rendered, zero JavaScript (the portal's
# CSP has no script-src and that stays true): the device-flow wait uses a
# meta refresh that re-polls once per page load, and every action is a plain
# form POST. SameSite=Lax cookies are the CSRF control for those POSTs.

_ADMIN_STYLE = """
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:60rem;margin:3rem auto;padding:0 1rem;color:#1a1a1a}
 h1{font-size:1.4rem} h2{font-size:1.1rem;margin-top:2rem}
 .who{color:#555;font-size:.85rem;margin-bottom:1.5rem}
 .err{background:#fee;border:1px solid #e99;padding:.75rem;border-radius:.35rem}
 .ok{background:#efe;border:1px solid #9e9;padding:.75rem;border-radius:.35rem}
 table{border-collapse:collapse;width:100%;font-size:.9rem}
 th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:left}
 th{background:#f5f5f5}
 form.inline{display:inline;margin:0}
 label{display:block;margin:.75rem 0 .25rem;font-weight:600}
 input,select{font-size:1rem;padding:.4rem;box-sizing:border-box}
 button{font-size:.95rem;padding:.45rem .9rem;background:#0b5;color:#fff;border:0;border-radius:.35rem;cursor:pointer}
 button.warn{background:#c33}
 .grid{display:grid;grid-template-columns:repeat(4,minmax(8rem,1fr));gap:.75rem;align-items:end}
 .nav{font-size:.85rem;margin-bottom:1rem}
"""


def _flash_html(flash):
    if not flash:
        return ""
    cls = "ok" if flash.get("ok") else "err"
    return '<p class="%s">%s</p>' % (cls, html.escape(flash.get("msg", "")))


def _csrf_field(csrf):
    return "<input type='hidden' name='csrf' value='%s'>" % html.escape(csrf or "", quote=True)


def render_admin_connect(email, flash=None, csrf=""):
    """No gateway session yet: explain + one button to start the device flow."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Spend caps - connect</title><style>%s</style></head><body>"
        "<h1>Spend-cap administration</h1>"
        "<p class='who'>Signed in as %s.</p>%s"
        "<p>Managing spend caps acts on the gateway <strong>as you</strong>: "
        "the gateway checks your Okta group membership and records your "
        "identity in its admin audit trail. Connect a gateway session to "
        "continue (one browser sign-in; your Okta session usually makes it "
        "a single click).</p>"
        "<form method='POST' action='/portal/admin/connect'>%s"
        "<button type='submit'>Connect gateway session</button></form>"
        "</body></html>"
    ) % (_ADMIN_STYLE, html.escape(email), _flash_html(flash), _csrf_field(csrf))


def render_admin_pending(verify_url, user_code, refresh_seconds, csrf=""):
    """Device flow started: link to the gateway's verification page and
    re-poll (server-side) on every meta refresh until approved."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='%d'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Spend caps - approve sign-in</title><style>%s</style></head><body>"
        "<h1>Approve the gateway sign-in</h1>"
        "<p>Open <a href='%s' target='_blank' rel='noopener'>the gateway "
        "sign-in page</a> in a new tab and approve code <strong>%s</strong>. "
        "This page checks again every few seconds; leave it open.</p>"
        "<p class='who'>Waiting for approval&hellip;</p>"
        "<form method='POST' action='/portal/admin/disconnect' class='inline'>%s"
        "<button type='submit' class='warn'>Cancel</button></form>"
        "</body></html>"
    ) % (refresh_seconds, _ADMIN_STYLE, html.escape(verify_url, quote=True),
         html.escape(user_code), _csrf_field(csrf))


def _limit_rows(limits, csrf=""):
    # Runtime-verified item shape (gateway 2.1.220): scope is a NESTED object
    # ({"scope": {"type": "user", "user_id": ...}}); there is no created_by
    # (attribution lives in the audit trail); a cleared cap REMAINS as a row
    # with amount null.
    rows = []
    for item in limits:
        scope = item.get("scope") or {}
        scope_type = str(scope.get("type", item.get("scope_type", "")))
        scope_id = scope.get("user_id") or scope.get("rbac_group_id") \
            or item.get("scope_id") or ""
        period = str(item.get("period", "monthly"))
        amount = item.get("amount")
        amount_html = "<em>(cleared)</em>" if amount is None \
            else html.escape(cents_to_dollars(amount))
        clear = (
            "<form method='POST' action='/portal/admin/clear' class='inline'>%s"
            "<input type='hidden' name='scope_type' value='%s'>"
            "<input type='hidden' name='scope_id' value='%s'>"
            "<input type='hidden' name='period' value='%s'>"
            "<button type='submit' class='warn'>Clear</button></form>"
        ) % (_csrf_field(csrf),
             html.escape(scope_type, quote=True), html.escape(str(scope_id), quote=True),
             html.escape(period, quote=True))
        rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (html.escape(scope_type), html.escape(str(scope_id)),
               amount_html,
               html.escape(period), html.escape(str(item.get("updated_at", ""))),
               "" if amount is None else clear)
        )
    if not rows:
        rows.append("<tr><td colspan='6'>No spend caps are set - spend is UNLIMITED for everyone.</td></tr>")
    return "".join(rows)


def render_admin_page(email, limits, flash=None, csrf=""):
    """The connected admin UI: current caps + set/clear forms."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Spend caps</title><style>%s</style></head><body>"
        "<h1>Spend-cap administration</h1>"
        "<p class='who'>Signed in as %s - gateway session connected (actions are "
        "recorded under your identity). <a href='/portal'>Downloads</a> | "
        "<a href='/portal/admin/audit'>Audit trail</a></p>%s"
        "<h2>Current caps</h2>"
        "<table><tr><th>Scope</th><th>Id</th><th>Cap</th><th>Period</th>"
        "<th>Updated</th><th></th></tr>%s</table>"
        "<p class='who'>Precedence: a per-user cap beats group caps; several "
        "group caps combine per the gateway's group_limit_mode. No rows = no "
        "enforcement.</p>"
        "<h2>Set / update a cap</h2>"
        "<form method='POST' action='/portal/admin/set'>%s<div class='grid'>"
        "<span><label for='scope_type'>Scope</label>"
        "<select id='scope_type' name='scope_type'>"
        "<option value='user'>user</option>"
        "<option value='rbac_group'>rbac_group (Okta group)</option>"
        "<option value='organization'>organization</option></select></span>"
        "<span><label for='scope_id'>User sub/email or group</label>"
        "<input id='scope_id' name='scope_id' placeholder='empty for organization'></span>"
        "<span><label for='amount'>Amount (US dollars)</label>"
        "<input id='amount' name='amount' placeholder='50.00' required></span>"
        "<span><label for='period'>Period</label>"
        "<select id='period' name='period'>%s</select></span>"
        "</div><p><button type='submit'>Set cap</button></p></form>"
        "<form method='POST' action='/portal/admin/disconnect' class='inline'>%s"
        "<button type='submit' class='warn'>Disconnect gateway session</button></form>"
        "</body></html>"
    ) % (
        _ADMIN_STYLE, html.escape(email), _flash_html(flash), _limit_rows(limits, csrf),
        _csrf_field(csrf),
        "".join("<option value='%s'%s>%s</option>"
                % (p, " selected" if p == "monthly" else "", p) for p in _SPEND_PERIODS),
        _csrf_field(csrf),
    )


def render_admin_audit(email, events):
    rows = []
    for ev in events:
        rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % tuple(html.escape(str(ev.get(k, ""))) for k in
                    ("created_at", "actor", "action", "target_id", "reason"))
        )
    if not rows:
        rows.append("<tr><td colspan='5'>No admin actions recorded yet.</td></tr>")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Spend caps - audit</title><style>%s</style></head><body>"
        "<h1>Spend-cap admin audit trail</h1>"
        "<p class='who'>Signed in as %s. <a href='/portal/admin'>Back to caps</a></p>"
        "<p class='who'>Recorded by the gateway (admin_audit table): every cap "
        "create/update/clear, with the acting identity - oidc:&lt;sub&gt; for "
        "portal admins, admin-key:&lt;id&gt; for the break-glass CLI keys.</p>"
        "<table><tr><th>At</th><th>Actor</th><th>Action</th><th>Target</th>"
        "<th>Reason</th></tr>%s</table>"
        "</body></html>"
    ) % (_ADMIN_STYLE, html.escape(email), "".join(rows))


def denied_page(email):
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Access denied</title></head>"
        "<body style='font-family:system-ui;max-width:36rem;margin:3rem auto'>"
        "<h1>Access denied</h1><p>Your account (%s) is not a member of a group "
        "authorized to download the Claude Code installer. Contact your administrator "
        "to request access.</p></body></html>" % html.escape(email)
    )


# ---------------------------------------------------------------- HTTP handler


class PortalHandler(BaseHTTPRequestHandler):
    server_version = "claude-portal/1.0"
    protocol_version = "HTTP/1.1"

    # -- wiring set on the server object --
    config = None
    oidc = None
    audit = None
    gateway = None

    def log_message(self, fmt, *args):  # route through logging, not stderr
        log.info("%s - %s", self.client_address[0], fmt % args)

    # -- helpers --
    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, _, v = part.strip().partition("=")
                out[k] = v
        return out

    def _client_ip(self):
        # Behind the single ALB, the LAST X-Forwarded-For entry is the peer the
        # ALB itself saw (it appends the connection source) - trustworthy for
        # audit. The first entry is whatever the client sent and is spoofable.
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[-1].strip()
        return self.client_address[0]

    def _set_cookie(self, name, value, max_age):
        self.send_header(
            "Set-Cookie",
            "%s=%s; Path=/portal; HttpOnly; Secure; SameSite=Lax; Max-Age=%d"
            % (name, value, max_age),
        )

    def _clear_cookie(self, name):
        self.send_header(
            "Set-Cookie",
            "%s=; Path=/portal; HttpOnly; Secure; SameSite=Lax; Max-Age=0" % name,
        )

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
        )

    def _send_html(self, status, body, extra=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for fn in extra or []:
            fn()
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location, extra=None):
        self.send_response(302)
        self._security_headers()
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for fn in extra or []:
            fn()
        self.end_headers()

    def _session(self):
        raw = self._cookies().get("portal_session")
        return verify_cookie(raw, self.config.session_secret) if raw else None

    # -- routing --
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/portal"
        # Set once a streaming response has begun sending its body: the
        # catch-all below must NOT then try to write a 500 page into that body.
        self._response_started = False
        try:
            if path == "/portal/healthz":
                return self._send_health()
            if path == "/portal/login":
                return self._handle_login()
            if path == "/portal/oauth/callback":
                return self._handle_callback(urllib.parse.parse_qs(parsed.query))
            if path == "/portal/download":
                return self._handle_download(urllib.parse.parse_qs(parsed.query))
            if path == "/portal/admin":
                return self._handle_admin()
            if path == "/portal/admin/audit":
                return self._handle_admin_audit()
            if path == "/portal":
                return self._handle_index(urllib.parse.parse_qs(parsed.query))
            self._send_html(404, "<h1>Not found</h1>")
        except Exception:  # last-resort guard; never leak a stack trace
            log.exception("unhandled error on %s", self.path)
            if self._response_started:
                # Headers + partial body already sent (e.g. S3 failed mid-ZIP);
                # writing a 500 page now would corrupt the download. Just drop
                # the connection so the client sees a truncated (failed) stream.
                self.close_connection = True
            else:
                self._send_html(500, "<h1>Internal error</h1>")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        self._response_started = False
        try:
            if path == "/portal/admin/connect":
                return self._handle_admin_connect()
            if path == "/portal/admin/set":
                return self._handle_admin_set(clear=False)
            if path == "/portal/admin/clear":
                return self._handle_admin_set(clear=True)
            if path == "/portal/admin/disconnect":
                return self._handle_admin_disconnect()
            self._send_html(404, "<h1>Not found</h1>")
        except Exception:
            log.exception("unhandled error on %s", self.path)
            self._send_html(500, "<h1>Internal error</h1>")

    def _send_health(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_index(self, query=None):
        session = self._session()
        if not session:
            return self._redirect("/portal/login")
        is_admin = bool(self.config.admin_groups) and is_authorized(
            session.get("groups", []), self.config.admin_groups)
        # Stage 2 when a cost center was submitted (and is valid); a bad value
        # falls back to stage 1 with the error shown.
        cost_center = (query or {}).get("cost_center", [None])[0]
        error = None
        if cost_center is not None:
            try:
                cost_center = validate_cost_center(cost_center, self.config)
            except SelectionError as exc:
                cost_center, error = None, str(exc)
        self._send_html(200 if error is None else 400,
                        render_page(self.config, session.get("email", ""),
                                    error=error, is_admin=is_admin,
                                    cost_center=cost_center))

    def _handle_login(self):
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        verifier, challenge = generate_pkce()
        txn = {
            "state": state,
            "nonce": nonce,
            "cv": verifier,
            "exp": int(time.time()) + self.config.transaction_ttl_seconds,
        }
        cookie = sign_cookie(txn, self.config.session_secret)
        url = self.oidc.authorize_url(state, nonce, challenge)
        self._redirect(
            url,
            extra=[lambda: self._set_cookie("portal_txn", cookie, self.config.transaction_ttl_seconds)],
        )

    def _handle_callback(self, query):
        txn = verify_cookie(self._cookies().get("portal_txn", ""), self.config.session_secret)
        if not txn:
            return self._send_html(400, "<h1>Login expired</h1><p>Please <a href='/portal/login'>try again</a>.</p>")
        # Okta returned an error (e.g. access_denied) instead of a code.
        if "error" in query:
            return self._send_html(400, "<h1>Sign-in failed</h1><p>%s</p>"
                                   % html.escape(query.get("error_description", query["error"])[0]))
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        if not code or not hmac.compare_digest(state, txn["state"]):
            return self._send_html(400, "<h1>Invalid sign-in state</h1>")

        token_resp = self.oidc.exchange_code(code, txn["cv"])
        id_token = token_resp.get("id_token")
        access_token = token_resp.get("access_token")
        if not id_token:
            return self._send_html(400, "<h1>Sign-in failed</h1><p>No ID token returned.</p>")
        try:
            claims = self.oidc.verify_id_token(id_token, txn["nonce"])
        except JwtError as exc:
            log.warning("id_token verification failed: %s", exc)
            return self._send_html(400, "<h1>Sign-in failed</h1><p>Token verification failed.</p>")

        userinfo = None
        groups = groups_from_claims(claims, None)
        if not groups and access_token:
            try:
                userinfo = self.oidc.userinfo(access_token)
                groups = groups_from_claims(claims, userinfo)
            except Exception as exc:
                log.warning("userinfo fetch failed: %s", exc)
        email = claims.get("email") or (userinfo or {}).get("email") or claims.get("sub", "")

        if not is_authorized(groups, self.config.access_groups):
            self._audit_denied(email, groups, "not in any access group (%s)"
                               % ", ".join(self.config.access_groups))
            return self._send_html(403, denied_page(email), extra=[lambda: self._clear_cookie("portal_txn")])

        session = {
            "email": email,
            "groups": groups,
            "exp": int(time.time()) + self.config.session_ttl_seconds,
        }
        session_cookie = sign_cookie(session, self.config.session_secret)

        def _cookies():
            self._set_cookie("portal_session", session_cookie, self.config.session_ttl_seconds)
            self._clear_cookie("portal_txn")

        self._redirect("/portal", extra=[_cookies])

    def _handle_download(self, query):
        session = self._session()
        if not session:
            return self._redirect("/portal/login")
        email = session.get("email", "")
        groups = session.get("groups", [])
        team = query.get("team", [None])[0]
        cost_center = query.get("cost_center", [None])[0]
        try:
            team, cost_center = validate_selection(team, cost_center, self.config)
        except SelectionError as exc:
            self._audit_denied(email, groups, "invalid selection: %s" % exc,
                               team=team, cost_center=cost_center)
            is_admin = bool(self.config.admin_groups) and is_authorized(
                groups, self.config.admin_groups)
            # Back to stage 2 if the cost center itself was valid, else stage 1.
            try:
                page_cc = validate_cost_center(cost_center, self.config)
            except SelectionError:
                page_cc = None
            return self._send_html(400, render_page(self.config, email, error=str(exc),
                                                    is_admin=is_admin,
                                                    cost_center=page_cc))

        sha256 = release_sha256(self.config)
        install_cmd = build_install_cmd(
            self.config.gateway_url, sha256, team, cost_center,
            self.config.disable_updates, self.config.bundle_extra_ca,
        )
        readme = build_readme(
            self.config.gateway_url, self.config.release_version, sha256, team, cost_center,
            self.config.bundle_extra_ca,
        )
        installer_bytes = read_s3_bytes(self.config.artifacts_bucket, self.config.installer_key)
        extra_ca_bytes = None
        if self.config.bundle_extra_ca:
            extra_ca_bytes = read_s3_bytes(self.config.artifacts_bucket, self.config.extra_ca_key)
        exe_key = "releases/%s/claude.exe" % self.config.release_version

        # Audit BEFORE streaming: a mid-stream client disconnect must not lose
        # the record of an authorized, validated download request.
        self._audit_success(email, groups, team, cost_center, sha256)

        fname = "claude-code-%s.zip" % self.config.release_version
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % fname)
        # Streaming ZIP of unknown final length: chunked transfer encoding, so a
        # truncated download (S3 read error, task recycle, ALB cut) omits the
        # terminating 0-chunk and the client detects it - a close-delimited body
        # would look successfully complete. Continuous byte flow also keeps the
        # ALB idle timeout (900s on the shared LB) well clear.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        # From here the response body is on the wire: a later failure (S3 read
        # error mid-stream) must abort the connection, not write a 500 page.
        self._response_started = True
        chunked = ChunkedWriter(self.wfile)
        stream_zip(
            chunked,
            s3_chunks(self.config.artifacts_bucket, exe_key),
            installer_bytes, install_cmd, readme, extra_ca_bytes,
        )
        chunked.close()

    # -- spend-cap admin --
    # The portal group gate here is UX + defense in depth; the SECURITY
    # boundary is the gateway, which verifies the bearer token and re-checks
    # the token's groups claim against its own admin_groups on every call.

    def _admin_session(self):
        """The portal session, if it may use the admin section; sends the
        response (redirect/404/403) and returns None otherwise."""
        session = self._session()
        if not session:
            self._redirect("/portal/login")
            return None
        if not self.config.admin_groups:
            # Feature disabled: indistinguishable from any other unknown path.
            self._send_html(404, "<h1>Not found</h1>")
            return None
        if not is_authorized(session.get("groups", []), self.config.admin_groups):
            self._audit_admin(session, "denied", "not in an admin group (%s)"
                              % ", ".join(self.config.admin_groups))
            self._send_html(403, denied_page(session.get("email", "")))
            return None
        return session

    # Gateway-token / device-flow / flash cookies. All signed with the same
    # session secret, all HttpOnly+Secure+SameSite=Lax via _set_cookie.
    def _gw_cookie(self):
        raw = self._cookies().get("portal_gw")
        return verify_cookie(raw, self.config.session_secret) if raw else None

    def _set_flash(self, ok, msg):
        cookie = sign_cookie({"ok": ok, "msg": msg, "exp": int(time.time()) + 60},
                             self.config.session_secret)
        return lambda: self._set_cookie("portal_flash", cookie, 60)

    def _pop_flash(self):
        raw = self._cookies().get("portal_flash")
        flash = verify_cookie(raw, self.config.session_secret) if raw else None
        return flash, (lambda: self._clear_cookie("portal_flash")) if raw else None

    def _form(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _csrf(self, session):
        return csrf_token(session, self.config.session_secret)

    def _csrf_ok(self, session, form):
        """Synchronizer-token check for the admin POSTs (see csrf_token: Lax
        does not protect against same-SITE sibling apps)."""
        return hmac.compare_digest(form.get("csrf", ""), self._csrf(session))

    def _handle_admin(self):
        session = self._admin_session()
        if not session:
            return
        email = session.get("email", "")
        flash, clear_flash = self._pop_flash()
        extra = [clear_flash] if clear_flash else []

        gw = self._gw_cookie()
        if gw:
            return self._render_admin_connected(session, gw, flash, extra)

        txn = verify_cookie(self._cookies().get("portal_gwdev", ""), self.config.session_secret)
        if txn:
            return self._poll_device_flow(session, txn, extra)
        self._send_html(200, render_admin_connect(email, flash, csrf=self._csrf(session)),
                        extra=extra)

    def _render_admin_connected(self, session, gw, flash, extra):
        email = session.get("email", "")
        csrf = self._csrf(session)
        try:
            status, doc = self.gateway.spend_api("GET", gw["tok"], path="?limit=200")
        except GatewayError as exc:
            return self._send_html(
                200, render_admin_connect(email, {"ok": False, "msg": str(exc)}, csrf=csrf),
                extra=extra)
        if status == 401:
            # Gateway session expired: try the refresh token once, then fall
            # back to a fresh connect.
            refreshed = self.gateway.refresh(gw.get("rt", "")) if gw.get("rt") else None
            if refreshed:
                cookie, ttl = build_gw_cookie(refreshed, session, self.config.session_secret)
                if cookie:
                    return self._redirect("/portal/admin", extra=[
                        lambda: self._set_cookie("portal_gw", cookie, ttl)])
            return self._send_html(
                200, render_admin_connect(
                    email, {"ok": False, "msg": "Your gateway session expired - connect again."},
                    csrf=csrf),
                extra=[lambda: self._clear_cookie("portal_gw")] + extra)
        if status == 403:
            # The PORTAL let them in but the GATEWAY refused: PORTAL_ADMIN_GROUP
            # and the gateway's SpendAdminGroups disagree. Surface it precisely.
            self._audit_admin(session, "denied", "gateway refused the admin call (403): "
                              "user is not in the gateway's SpendAdminGroups")
            return self._send_html(
                403, render_admin_connect(
                    email, {"ok": False, "msg":
                            "The gateway refused: your account is not in its spend-admin "
                            "groups (SpendAdminGroups). Ask the platform team to align it "
                            "with the portal's PORTAL_ADMIN_GROUP."},
                    csrf=csrf),
                extra=extra)
        if status != 200 or not isinstance(doc, dict):
            return self._send_html(
                200, render_admin_connect(
                    email, {"ok": False, "msg": "Gateway error listing caps (HTTP %s)." % status},
                    csrf=csrf),
                extra=extra)
        self._send_html(200, render_admin_page(email, doc.get("data", []), flash, csrf=csrf),
                        extra=extra)

    def _poll_device_flow(self, session, txn, extra):
        email = session.get("email", "")
        csrf = self._csrf(session)
        try:
            result = self.gateway.poll_token(txn["dc"])
        except GatewayError as exc:
            return self._send_html(
                200, render_admin_connect(email, {"ok": False, "msg": str(exc)}, csrf=csrf),
                extra=[lambda: self._clear_cookie("portal_gwdev")] + extra)
        if result in ("pending", "slow_down"):
            interval = txn.get("int", 5)
            if result == "slow_down":
                # RFC 8628 3.5: slow_down means add 5 seconds to the polling
                # interval for this and all subsequent requests. The interval
                # lives in the signed txn cookie, so re-sign it bumped.
                interval += 5
                bumped = dict(txn, int=interval)
                cookie = sign_cookie(bumped, self.config.session_secret)
                ttl = max(txn["exp"] - int(time.time()), 1)
                extra = [lambda: self._set_cookie("portal_gwdev", cookie, ttl)] + extra
            return self._send_html(
                200, render_admin_pending(txn["vu"], txn.get("uc", ""), interval + 1, csrf=csrf),
                extra=extra)
        # Granted. The cookie outlives neither the gateway token nor the
        # portal session (least privilege on both axes) - and respects the
        # browser's ~4KB per-cookie cap rather than being dropped silently.
        cookie, ttl = build_gw_cookie(result, session, self.config.session_secret)
        if not cookie:
            return self._send_html(
                200, render_admin_connect(
                    email, {"ok": False, "msg":
                            "Sign-in succeeded but the gateway session token is too large "
                            "to store in a browser cookie (very large Okta groups claim). "
                            "Use scripts/set-spend-limit.sh, or reduce the groups pushed "
                            "into the token."},
                    csrf=csrf),
                extra=[lambda: self._clear_cookie("portal_gwdev")] + extra)
        self._audit_admin(session, "success", "gateway admin session connected")

        def _cookies():
            self._set_cookie("portal_gw", cookie, ttl)
            self._clear_cookie("portal_gwdev")
        self._redirect("/portal/admin", extra=[_cookies])

    def _handle_admin_connect(self):
        session = self._admin_session()
        if not session:
            return
        if not self._csrf_ok(session, self._form()):
            return self._send_html(403, "<h1>Invalid request token</h1>")
        try:
            doc = self.gateway.device_authorize()
        except GatewayError as exc:
            return self._send_html(
                200, render_admin_connect(session.get("email", ""),
                                          {"ok": False, "msg": str(exc)},
                                          csrf=self._csrf(session)))
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
                      or doc.get("verification_uri") or self.config.gateway_url),
            "int": interval,
            "exp": int(time.time()) + expires_in,
        }
        cookie = sign_cookie(txn, self.config.session_secret)
        self._redirect("/portal/admin", extra=[
            lambda: self._set_cookie("portal_gwdev", cookie, expires_in)])

    def _handle_admin_disconnect(self):
        session = self._admin_session()
        if not session:
            return
        if not self._csrf_ok(session, self._form()):
            return self._send_html(403, "<h1>Invalid request token</h1>")

        def _cookies():
            self._clear_cookie("portal_gw")
            self._clear_cookie("portal_gwdev")
        self._redirect("/portal/admin", extra=[_cookies])

    def _handle_admin_set(self, clear):
        session = self._admin_session()
        if not session:
            return
        form = self._form()
        if not self._csrf_ok(session, form):
            return self._send_html(403, "<h1>Invalid request token</h1>")
        gw = self._gw_cookie()
        if not gw:
            return self._redirect("/portal/admin")
        scope_type = form.get("scope_type", "")
        scope_id = form.get("scope_id", "").strip()
        period = form.get("period", "monthly")
        try:
            body = build_spend_limit_body(
                scope_type, scope_id, None if clear else form.get("amount", ""), period)
        except (SelectionError, AmountError) as exc:
            return self._redirect("/portal/admin", extra=[self._set_flash(False, str(exc))])

        action = "%s %s cap for %s (%s)" % (
            "clear" if clear else "set", scope_type, scope_id or "organization", period)
        try:
            status, doc = self.gateway.spend_api("POST", gw["tok"], body=body)
        except GatewayError as exc:
            return self._redirect("/portal/admin", extra=[self._set_flash(False, str(exc))])
        if status in (200, 201):
            self._audit_admin(session, "success", action)
            return self._redirect("/portal/admin", extra=[
                self._set_flash(True, "Done: %s." % action)])
        self._audit_admin(session, "denied", "%s -> gateway HTTP %s" % (action, status))
        detail = ""
        if isinstance(doc, dict):
            err = doc.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message", ""))
            elif err:
                detail = str(err)
        # 401 falls through to the reconnect path on the next GET.
        return self._redirect("/portal/admin", extra=[
            self._set_flash(False, "Gateway refused (%s HTTP %s). %s" % (action, status, detail))])

    def _handle_admin_audit(self):
        session = self._admin_session()
        if not session:
            return
        gw = self._gw_cookie()
        if not gw:
            return self._redirect("/portal/admin")
        try:
            status, doc = self.gateway.spend_api("GET", gw["tok"], path="/audit?limit=200")
        except GatewayError as exc:
            return self._redirect("/portal/admin", extra=[self._set_flash(False, str(exc))])
        if status != 200 or not isinstance(doc, dict):
            return self._redirect("/portal/admin", extra=[
                self._set_flash(False, "Gateway error fetching the audit trail (HTTP %s)." % status)])
        self._send_html(200, render_admin_audit(session.get("email", ""), doc.get("data", [])))

    def _audit_admin(self, session, outcome, reason):
        self.audit.write(build_audit_record(
            outcome, session.get("email", ""), session.get("groups", []),
            None, None, None, None, self._client_ip(),
            self.headers.get("User-Agent", ""), reason=reason, event="portal_admin"))

    # -- audit wrappers --
    def _audit_success(self, email, groups, team, cost_center, sha256):
        self.audit.write(build_audit_record(
            "success", email, groups, team, cost_center,
            self.config.release_version, sha256, self._client_ip(),
            self.headers.get("User-Agent", ""),
        ))

    def _audit_denied(self, email, groups, reason, team=None, cost_center=None):
        self.audit.write(build_audit_record(
            "denied", email, groups, team, cost_center,
            self.config.release_version, None, self._client_ip(),
            self.headers.get("User-Agent", ""), reason=reason,
        ))


# ---------------------------------------------------------------- main


def make_server(config, oidc, audit, handler_cls=PortalHandler, gateway=None):
    httpd = ThreadingHTTPServer(("0.0.0.0", config.listen_port), handler_cls)
    httpd.daemon_threads = True
    handler_cls.config = config
    handler_cls.oidc = oidc
    handler_cls.audit = audit
    handler_cls.gateway = gateway if gateway is not None else GatewayClient(config)
    return httpd


def main():  # pragma: no cover - container entrypoint
    logging.basicConfig(level=os.environ.get("PORTAL_LOG_LEVEL", "INFO"),
                        stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    global s3, logs
    config = Config()
    s3 = boto3.client("s3")
    logs = boto3.client("logs")
    oidc = OidcClient(config)
    audit = AuditLogger(logs, config.audit_log_group)
    httpd = make_server(config, oidc, audit)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(config.tls_cert, config.tls_key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    log.info("portal listening on :%d (TLS)", config.listen_port)
    httpd.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
