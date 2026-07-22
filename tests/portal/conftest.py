"""Fixtures + helpers for the download-portal tests.

The portal app verifies RS256 in pure Python with no crypto dependency; these
tests use `cryptography` (a test-only dep) to MINT an RSA key, publish it as a
JWKS, and sign test ID tokens - the two halves that let us exercise real
signature verification, key rotation, and the full callback flow without a live
Okta.
"""

import base64
import email.message
import io
import json
import os
import sys
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# app.py lives in the portal image build context.
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "docker", "portal")))

import app as app_module  # noqa: E402


# ------------------------------------------------------------- base64url


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64u(n):
    length = (n.bit_length() + 7) // 8
    return _b64u(n.to_bytes(length, "big"))


# ------------------------------------------------------------- RSA / JWKS / JWT


class SigningKey:
    def __init__(self, kid="test-key-1"):
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self):
        nums = self.private.public_key().public_numbers()
        return {
            "kty": "RSA",
            "kid": self.kid,
            "use": "sig",
            "alg": "RS256",
            "n": _int_to_b64u(nums.n),
            "e": _int_to_b64u(nums.e),
        }

    def jwks(self):
        return {"keys": [self.jwk()]}

    def sign(self, header, payload):
        h = _b64u(json.dumps(header, separators=(",", ":")).encode())
        p = _b64u(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = (h + "." + p).encode("ascii")
        sig = self.private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return h + "." + p + "." + _b64u(sig)

    def id_token(self, issuer, audience, *, nonce=None, groups=None, email="dev@example.com",
                 exp_delta=3600, sub="00u123", extra=None, kid=None, alg="RS256"):
        header = {"alg": alg, "kid": kid or self.kid, "typ": "JWT"}
        payload = {
            "iss": issuer,
            "aud": audience,
            "sub": sub,
            "email": email,
            "exp": int(time.time()) + exp_delta,
            "iat": int(time.time()),
        }
        if nonce is not None:
            payload["nonce"] = nonce
        if groups is not None:
            payload["groups"] = groups
        if extra:
            payload.update(extra)
        return self.sign(header, payload)


@pytest.fixture
def key():
    return SigningKey()


# ------------------------------------------------------------- app + config

TEST_ENV = {
    "OIDC_ISSUER": "https://issuer.example.com",
    "OIDC_CLIENT_ID": "client-abc",
    "OIDC_CLIENT_SECRET": "topsecret",
    "SESSION_SECRET": "unit-test-session-secret",
    "PUBLIC_URL": "https://claude-gateway.example.com",
    "GATEWAY_URL": "https://claude-gateway.example.com",
    "ACCESS_GROUP": "claude-gateway-users",
    "PORTAL_TEAMS": "platform,data,security",
    "PORTAL_COST_CENTERS": "CC-1000,CC-2000",
    "ARTIFACTS_BUCKET": "portal-artifacts",
    "RELEASE_VERSION": "2.1.207",
    "AUDIT_LOG_GROUP": "/claude/portal-audit",
    "BUNDLE_EXTRA_CA": "false",
    "DISABLE_UPDATES": "true",
    "SESSION_TTL_HOURS": "8",
}


@pytest.fixture
def app():
    return app_module


@pytest.fixture
def env():
    """A fresh, mutable copy of the test environment (tweak a key, build a
    Config)."""
    return dict(TEST_ENV)


@pytest.fixture
def config(app):
    return app.Config(dict(TEST_ENV))


# ------------------------------------------------------------- stubs


class StubOidc(app_module.OidcClient):
    """Real verify_id_token (exercises the pure-Python RS256 path); network
    calls are canned."""

    def __init__(self, config, jwks, token_resp=None, userinfo_resp=None):
        super().__init__(config)
        self._discovery = {
            "issuer": config.issuer,
            "authorization_endpoint": config.issuer + "/oauth2/v1/authorize",
            "token_endpoint": config.issuer + "/oauth2/v1/token",
            "userinfo_endpoint": config.issuer + "/oauth2/v1/userinfo",
            "jwks_uri": config.issuer + "/oauth2/v1/keys",
        }
        self._jwks = jwks
        self._token_resp = token_resp
        self._userinfo_resp = userinfo_resp
        self.exchanged = None
        self.userinfo_token = None

    def exchange_code(self, code, code_verifier):
        self.exchanged = (code, code_verifier)
        return self._token_resp

    def userinfo(self, access_token):
        self.userinfo_token = access_token
        return self._userinfo_resp


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError("no such object: %s" % Key)
        return {"Body": io.BytesIO(self.objects[Key])}


class FakeAudit:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


@pytest.fixture
def audit():
    return FakeAudit()


# ------------------------------------------------------------- handler harness


def make_handler(app, config, oidc, audit, *, cookies=None, headers=None, client_ip="10.0.0.9"):
    """Build a PortalHandler without a socket; response goes to an in-memory
    BytesIO we can parse."""
    h = app.PortalHandler.__new__(app.PortalHandler)
    h.config = config
    h.oidc = oidc
    h.audit = audit
    h.client_address = (client_ip, 5555)
    h.request_version = "HTTP/1.1"
    h.requestline = "GET /portal HTTP/1.1"
    h.command = "GET"
    h.close_connection = False
    h._headers_buffer = []
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    if cookies:
        msg["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in cookies.items())
    h.headers = msg
    h.wfile = io.BytesIO()
    h.rfile = io.BytesIO()
    return h


def parse_response(h):
    raw = h.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = {}
    set_cookies = []
    for line in lines[1:]:
        k, _, v = line.partition(b": ")
        k = k.decode()
        v = v.decode()
        if k.lower() == "set-cookie":
            set_cookies.append(v)
        else:
            headers[k] = v
    return status, headers, set_cookies, body


def cookie_value(set_cookies, name):
    for c in set_cookies:
        if c.startswith(name + "="):
            return c.split(";", 1)[0][len(name) + 1:]
    return None


def dechunk(body):
    """Decode an HTTP/1.1 chunked-transfer-encoded body."""
    out = bytearray()
    i = 0
    while i < len(body):
        j = body.index(b"\r\n", i)
        size = int(body[i:j], 16)
        if size == 0:
            break
        start = j + 2
        out.extend(body[start:start + size])
        i = start + size + 2  # skip the trailing CRLF
    return bytes(out)
