"""Signed session/transaction cookies + PKCE."""

import hashlib
import time


SECRET = "unit-test-session-secret"


def test_roundtrip(app):
    raw = app.sign_cookie({"email": "a@b.com", "exp": int(time.time()) + 60}, SECRET)
    payload = app.verify_cookie(raw, SECRET)
    assert payload["email"] == "a@b.com"


def test_tampered_body_rejected(app):
    raw = app.sign_cookie({"email": "a@b.com", "exp": int(time.time()) + 60}, SECRET)
    body, _, sig = raw.partition(".")
    # Swap in a different body with the old signature.
    forged = app.b64url_encode(b'{"email":"admin@b.com","exp":9999999999}') + "." + sig
    assert app.verify_cookie(forged, SECRET) is None


def test_wrong_secret_rejected(app):
    raw = app.sign_cookie({"email": "a@b.com", "exp": int(time.time()) + 60}, SECRET)
    assert app.verify_cookie(raw, "different-secret") is None


def test_expired_rejected(app):
    raw = app.sign_cookie({"email": "a@b.com", "exp": int(time.time()) - 1}, SECRET)
    assert app.verify_cookie(raw, SECRET) is None


def test_missing_exp_rejected(app):
    raw = app.sign_cookie({"email": "a@b.com"}, SECRET)
    assert app.verify_cookie(raw, SECRET) is None


def test_garbage_rejected(app):
    assert app.verify_cookie("", SECRET) is None
    assert app.verify_cookie("no-dot", SECRET) is None
    assert app.verify_cookie("a.b.c", SECRET) is None


def test_pkce_pair_is_s256(app):
    verifier, challenge = app.generate_pkce()
    expected = app.b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    assert challenge == expected
    # verifier/challenge are URL-safe, unpadded.
    assert "=" not in verifier and "=" not in challenge
    assert "+" not in challenge and "/" not in challenge


def test_pkce_pairs_are_unique(app):
    assert app.generate_pkce()[0] != app.generate_pkce()[0]
