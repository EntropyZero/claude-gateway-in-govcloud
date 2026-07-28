"""OIDC client: discovery + token exchange + JWKS + userinfo.

Network methods are thin so tests can override them; verify_id_token runs the
real crypto. Stays on stdlib urllib (proxy handling via HTTPS_PROXY/NO_PROXY
from the environment; the image trust store carries the enterprise CA) - do
NOT swap in `requests`.
"""

import base64
import json
import ssl
import time
import urllib.parse
import urllib.request

from .crypto import JwtError, verify_jwt


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
