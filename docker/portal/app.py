#!/usr/bin/env python3
"""Okta-secured Claude Code installer download portal.

A small, dependency-light HTTP service (stdlib + boto3 only) that:

  * runs the full OIDC authorization-code flow (state + PKCE + nonce) against
    the SAME Okta issuer the gateway uses, verifying the ID token's RS256
    signature against the issuer's JWKS in pure Python (no crypto dependency),
  * authorizes on Okta GROUP membership (a value the ALB's authenticate-oidc
    cannot evaluate - which is why auth lives in the app, not the listener),
  * renders one server-side page with Team and Cost Center dropdowns whose
    option lists come from deployment config, and
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
        self.access_group = env["ACCESS_GROUP"]
        # Session TTL is configured in hours (CFN parameter); transaction cookie
        # lifetime stays in seconds (short, internal).
        self.session_ttl_seconds = int(env.get("SESSION_TTL_HOURS", "8")) * 3600
        self.transaction_ttl_seconds = int(env.get("TRANSACTION_TTL_SECONDS", "600"))
        # Dropdown option lists (comma-delimited; whitespace trimmed).
        self.teams = _split_list(env.get("PORTAL_TEAMS", ""))
        self.cost_centers = _split_list(env.get("PORTAL_COST_CENTERS", ""))
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


def is_authorized(groups, access_group):
    return access_group in (groups or [])


# ---------------------------------------------------------------- selection


class SelectionError(Exception):
    pass


# Mirrors Install-ClaudeCode.ps1's ValidatePattern('^[^,\s]*$'): a value that
# would break OTEL_RESOURCE_ATTRIBUTES parsing or the install.cmd argument.
def _clean_token(value):
    return value != "" and not any(c.isspace() for c in value) and "," not in value


def validate_selection(team, cost_center, config):
    """Reject anything not in the configured lists (and, defensively, anything
    with whitespace/commas). Returns (team, cost_center) or raises."""
    if team is None or cost_center is None:
        raise SelectionError("both team and cost_center are required")
    if not _clean_token(team) or not _clean_token(cost_center):
        raise SelectionError("team/cost_center must not contain spaces or commas")
    if team not in config.teams:
        raise SelectionError("team %r is not an allowed value" % team)
    if cost_center not in config.cost_centers:
        raise SelectionError("cost_center %r is not an allowed value" % cost_center)
    return team, cost_center


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
        "It installs claude.exe to %%USERPROFILE%%\\.local\\bin, verifies the\r\n"
        "SHA-256 and Anthropic's Authenticode signature, and configures Claude\r\n"
        "Code to sign in through the gateway with updates disabled.\r\n\r\n"
        "Package contents:\r\n"
        "  - claude.exe            : the Claude Code binary (win32-x64).\r\n"
        "  - Install-ClaudeCode.ps1: the installer (unmodified).\r\n"
        "  - install.cmd           : runs the installer with your options.\r\n"
        "%s"
        "\r\nAfter installing, open a NEW terminal and run:  claude\r\n"
        "Verify the gateway certificate fingerprint with your IT team before\r\n"
        "first login.\r\n"
        % (version, gateway_url, team, cost_center, sha256, ca_note)
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
                       version, sha256, source_ip, user_agent, reason=None):
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "portal_download",
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
</style></head><body>
<h1>Claude Code installer</h1>
<p class="who">Signed in as {email}. Version {version}.</p>
{error}
<form method="GET" action="/portal/download">
 <label for="team">Team</label>
 <select id="team" name="team" required>{teams}</select>
 <label for="cost_center">Cost center</label>
 <select id="cost_center" name="cost_center" required>{cost_centers}</select>
 <button type="submit">Download pre-configured installer</button>
</form>
</body></html>"""


def _options(values):
    return "".join('<option value="%s">%s</option>' % (html.escape(v), html.escape(v)) for v in values)


def render_page(config, email, error=None):
    err_html = '<p class="err">%s</p>' % html.escape(error) if error else ""
    return _PAGE.format(
        email=html.escape(email),
        version=html.escape(config.release_version),
        error=err_html,
        teams=_options(config.teams),
        cost_centers=_options(config.cost_centers),
    )


def denied_page(email):
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Access denied</title></head>"
        "<body style='font-family:system-ui;max-width:36rem;margin:3rem auto'>"
        "<h1>Access denied</h1><p>Your account (%s) is not a member of the group "
        "required to download the Claude Code installer. Contact your administrator "
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
            if path == "/portal":
                return self._handle_index()
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

    def _send_health(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_index(self):
        session = self._session()
        if not session:
            return self._redirect("/portal/login")
        self._send_html(200, render_page(self.config, session.get("email", "")))

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

        if not is_authorized(groups, self.config.access_group):
            self._audit_denied(email, groups, "not in access group %s" % self.config.access_group)
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
            return self._send_html(400, render_page(self.config, email, error=str(exc)))

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


def make_server(config, oidc, audit, handler_cls=PortalHandler):
    httpd = ThreadingHTTPServer(("0.0.0.0", config.listen_port), handler_cls)
    httpd.daemon_threads = True
    handler_cls.config = config
    handler_cls.oidc = oidc
    handler_cls.audit = audit
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
