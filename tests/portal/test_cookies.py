"""Signed session/transaction cookies + PKCE (portal.crypto)."""

import hashlib
import time

from portal.crypto import (b64url_encode, csrf_token, generate_pkce,
                           sign_cookie, verify_cookie)

SECRET = "unit-test-session-secret"


def test_roundtrip():
    raw = sign_cookie({"email": "a@b.com", "exp": int(time.time()) + 60}, SECRET)
    payload = verify_cookie(raw, SECRET)
    assert payload["email"] == "a@b.com"


def test_tampered_body_rejected():
    raw = sign_cookie({"email": "a@b.com", "exp": int(time.time()) + 60}, SECRET)
    body, _, sig = raw.partition(".")
    # Swap in a different body with the old signature.
    forged = b64url_encode(b'{"email":"admin@b.com","exp":9999999999}') + "." + sig
    assert verify_cookie(forged, SECRET) is None


def test_wrong_secret_rejected():
    raw = sign_cookie({"email": "a@b.com", "exp": int(time.time()) + 60}, SECRET)
    assert verify_cookie(raw, "different-secret") is None


def test_expired_rejected():
    raw = sign_cookie({"email": "a@b.com", "exp": int(time.time()) - 1}, SECRET)
    assert verify_cookie(raw, SECRET) is None


def test_missing_exp_rejected():
    raw = sign_cookie({"email": "a@b.com"}, SECRET)
    assert verify_cookie(raw, SECRET) is None


def test_garbage_rejected():
    assert verify_cookie("", SECRET) is None
    assert verify_cookie("no-dot", SECRET) is None
    assert verify_cookie("a.b.c", SECRET) is None


def test_pkce_pair_is_s256():
    verifier, challenge = generate_pkce()
    expected = b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    assert challenge == expected
    # verifier/challenge are URL-safe, unpadded.
    assert "=" not in verifier and "=" not in challenge
    assert "+" not in challenge and "/" not in challenge


def test_pkce_pairs_are_unique():
    assert generate_pkce()[0] != generate_pkce()[0]


def test_csrf_token_is_deterministic_per_session():
    s1 = {"email": "a@b.com", "exp": 1000}
    assert csrf_token(s1, SECRET) == csrf_token(dict(s1), SECRET)
    # Different session or secret -> different token.
    assert csrf_token(s1, SECRET) != csrf_token({"email": "a@b.com", "exp": 2000}, SECRET)
    assert csrf_token(s1, SECRET) != csrf_token(s1, "other-secret")
