"""Fixtures + helpers for the download-portal tests (Flask package edition).

The portal app verifies RS256 in pure Python with no crypto dependency; these
tests use `cryptography` (a test-only dep) to MINT an RSA key, publish it as a
JWKS, and sign test ID tokens - the two halves that let us exercise real
signature verification, key rotation, and the full callback flow without a
live Okta.

The app is driven through `create_app()` + Flask's `test_client()`; all
collaborators (OIDC, gateway, S3, audit) are injected as factory kwargs -
there are no module globals to monkeypatch anymore.
"""

import base64
import io
import json
import os
import sys
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization  # noqa: F401
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# The portal package lives in the portal image build context.
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "docker", "portal")))

import portal.fingerprint as fingerprint_module  # noqa: E402
from portal import create_app  # noqa: E402
from portal.config import Config  # noqa: E402
from portal.crypto import csrf_token, sign_cookie  # noqa: E402
from portal.gateway import GatewayClient  # noqa: E402  (re-export for tests)
from portal.oidc import OidcClient  # noqa: E402


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

# Env var wiring rule: every var here must exist in portal/config.py AND the
# 04 template's Environment/Secrets block (see the brief).
TEST_ENV = {
    "OIDC_ISSUER": "https://issuer.example.com",
    "OIDC_CLIENT_ID": "client-abc",
    "OIDC_CLIENT_SECRET": "topsecret",
    "SESSION_SECRET": "unit-test-session-secret",
    "PUBLIC_URL": "https://claude-gateway.example.com",
    "GATEWAY_URL": "https://claude-gateway.example.com",
    "ACCESS_GROUP": "claude-gateway-users",
    "PORTAL_COST_CENTER_TEAMS": "CC-1000:platform|data,CC-2000:security",
    "ARTIFACTS_BUCKET": "portal-artifacts",
    "RELEASE_VERSION": "2.1.207",
    "AUDIT_LOG_GROUP": "/claude/portal-audit",
    "BUNDLE_EXTRA_CA": "false",
    "DISABLE_UPDATES": "true",
    "SESSION_TTL_HOURS": "8",
    # New in portal v2: read-only spend key (feature-gates /portal/me) and
    # the user-guide object key.
    "SPEND_READ_KEY": "read-key-abc",
    "USER_GUIDE_KEY": "docs/user-manual.pdf",
}

SECRET = TEST_ENV["SESSION_SECRET"]
GROUP = "claude-gateway-users"


@pytest.fixture
def env():
    """A fresh, mutable copy of the test environment (tweak a key, build a
    Config)."""
    return dict(TEST_ENV)


@pytest.fixture
def config():
    return Config(dict(TEST_ENV))


@pytest.fixture(autouse=True)
def _clear_fingerprint_cache():
    """The fingerprint cache is module-global by design; isolate tests."""
    fingerprint_module.clear_cache()
    yield
    fingerprint_module.clear_cache()


# ------------------------------------------------------------- stubs


class StubOidc(OidcClient):
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


class S3NoSuchKey(Exception):
    """Duck-typed botocore ClientError for a missing object (the guide view
    checks exc.response["Error"]["Code"])."""

    def __init__(self, key):
        super().__init__("no such object: %s" % key)
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _FakeBody:
    def __init__(self, data, fail_after=None):
        self._buf = io.BytesIO(data)
        self._fail_after = fail_after
        self.reads = 0

    def read(self, n=None):
        self.reads += 1
        if self._fail_after is not None and self.reads > self._fail_after:
            raise OSError("S3 read failed mid-stream")
        return self._buf.read(n)


class FakeS3:
    """objects: {key: bytes}. Missing keys raise a NoSuchKey-shaped error;
    fail_after[key]=N makes the Nth+1 Body.read() raise (mid-stream failure)."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.fail_after = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise S3NoSuchKey(Key)
        data = self.objects[Key]
        return {
            "Body": _FakeBody(data, self.fail_after.get(Key)),
            "ContentLength": len(data),
        }


class FakeAudit:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


@pytest.fixture
def audit():
    return FakeAudit()


class StubGateway:
    """Canned gateway. Records every spend_api / effective_usage call; device
    flow, refresh and effective-usage results are scripted per test."""

    def __init__(self, device=None, poll_results=None, api=None, refresh_resp=None,
                 effective=None):
        self.device = device or {
            "device_code": "dc-123", "user_code": "WDJB-MJHT",
            "verification_uri": "https://claude-gateway.example.com/oauth/device",
            "verification_uri_complete": "https://claude-gateway.example.com/oauth/device?code=WDJB-MJHT",
            "expires_in": 600, "interval": 5,
        }
        self.poll_results = list(poll_results or [])
        self.api = dict(api or {})          # (method, path) -> (status, doc)
        self.refresh_resp = refresh_resp
        # Scripted effective-usage results: each entry is (status, doc) or an
        # Exception; when exhausted, an empty 200 page is returned.
        self.effective_results = list(effective or [])
        self.api_calls = []                 # (method, token, path, body)
        self.effective_calls = []           # (auth, kwargs)
        self.polled = []
        self.refreshed = []

    def device_authorize(self):
        if isinstance(self.device, Exception):
            raise self.device
        return self.device

    def poll_token(self, device_code):
        self.polled.append(device_code)
        result = self.poll_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def refresh(self, refresh_token):
        self.refreshed.append(refresh_token)
        return self.refresh_resp

    def spend_api(self, method, token, path="", body=None):
        self.api_calls.append((method, token, path, body))
        return self.api.get((method, path.partition("?")[0]), (200, {"data": []}))

    def effective_usage(self, auth, **kwargs):
        self.effective_calls.append((auth, kwargs))
        if self.effective_results:
            result = self.effective_results.pop(0)
        else:
            result = (200, {"data": [], "next_page": None})
        if isinstance(result, Exception):
            raise result
        return result


# ------------------------------------------------------------- app harness


class Harness:
    """One assembled app + its fakes, reachable for assertions."""

    def __init__(self, config=None, env=None, *, jwks=None, token_resp=None,
                 userinfo_resp=None, s3=None, gateway=None, oidc=None,
                 audit=None):
        self.cfg = config if config is not None else Config(dict(env or TEST_ENV))
        self.oidc = oidc if oidc is not None else StubOidc(
            self.cfg, jwks if jwks is not None else {"keys": []},
            token_resp=token_resp, userinfo_resp=userinfo_resp)
        self.gateway = gateway if gateway is not None else StubGateway()
        self.s3 = s3 if s3 is not None else FakeS3()
        self.audit = audit if audit is not None else FakeAudit()
        self.app = create_app(self.cfg, oidc=self.oidc, gateway=self.gateway,
                              s3=self.s3, audit=self.audit)
        self.client = self.app.test_client()

    def get(self, path, cookies=None, headers=None):
        return get(self.client, path, cookies=cookies, headers=headers)

    def post(self, path, form=None, cookies=None, headers=None):
        return post(self.client, path, form=form, cookies=cookies,
                    headers=headers)


def build_app(config, *, oidc=None, gateway=None, s3=None, audit=None):
    """create_app with fakes for anything not supplied."""
    return create_app(
        config,
        oidc=oidc if oidc is not None else StubOidc(config, {"keys": []}),
        gateway=gateway if gateway is not None else StubGateway(),
        s3=s3 if s3 is not None else FakeS3(),
        audit=audit if audit is not None else FakeAudit(),
    )


def build_client(config, **kwargs):
    return build_app(config, **kwargs).test_client()


def _apply_cookies(client, cookies):
    """Werkzeug's test client manages the Cookie header from its own jar (a
    hand-built Cookie header is overridden), so requests set cookies through
    the jar - cleared first, so each request carries EXACTLY the cookies the
    test names (no leakage from earlier Set-Cookie responses)."""
    jar = getattr(client, "_cookies", None)
    if jar is not None:
        jar.clear()
    for k, v in (cookies or {}).items():
        client.set_cookie(k, v, path="/portal")


def get(client, path, cookies=None, headers=None):
    _apply_cookies(client, cookies)
    return client.get(path, headers=dict(headers or {}))


def post(client, path, form=None, cookies=None, headers=None):
    _apply_cookies(client, cookies)
    return client.post(path, data=form or {}, headers=dict(headers or {}))


# ------------------------------------------------------------- cookie helpers


def session_payload(email="dev@example.com", groups=None, sub="00u123", ttl=3600):
    payload = {"email": email, "groups": groups if groups is not None else [GROUP],
               "exp": int(time.time()) + ttl}
    if sub is not None:
        payload["sub"] = sub
    return payload


def session_cookie(secret=SECRET, **kwargs):
    return sign_cookie(session_payload(**kwargs), secret)


def txn_cookie(state, nonce, cv="verifier123", ttl=600, secret=SECRET):
    return sign_cookie(
        {"state": state, "nonce": nonce, "cv": cv, "exp": int(time.time()) + ttl},
        secret)


def csrf_for_payload(payload, secret=SECRET):
    return csrf_token(payload, secret)


def cookie_value(set_cookies, name):
    for c in set_cookies:
        if c.startswith(name + "="):
            return c.split(";", 1)[0][len(name) + 1:]
    return None


def set_cookies_of(resp):
    return resp.headers.getlist("Set-Cookie")
