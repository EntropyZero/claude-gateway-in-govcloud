"""Cookie signing, PKCE, pure-Python RS256 JWT verification, CSRF tokens.

Ported verbatim from the battle-tested app.py - do not "improve" the crypto.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

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


# ---------------------------------------------------------------- CSRF


def csrf_token(session, secret):
    """Deterministic per-session CSRF token for the POST forms.

    SameSite=Lax blocks cross-SITE posts, but "site" is the registrable
    domain: every sibling app under the corporate domain is SAME-site, so Lax
    alone does not protect admin mutations from a compromised internal page.
    This synchronizer token closes that: it is embedded as a hidden form
    field and required by every POST. A same-site attacker can SEND
    requests with the victim's cookies but cannot READ responses (same-origin
    policy), so it cannot learn the token; without the secret it cannot
    forge one."""
    msg = "csrf|%s|%s" % (session.get("email", ""), session.get("exp", 0))
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
