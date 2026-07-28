"""ID-token verification: RS256 signature against the JWKS + claim checks."""

import pytest

from portal.crypto import JwtError, verify_jwt

from conftest import StubOidc

ISS = "https://issuer.example.com"
AUD = "client-abc"


def test_valid_token_verifies(key):
    tok = key.id_token(ISS, AUD, nonce="n1", groups=["claude-gateway-users"])
    claims = verify_jwt(tok, key.jwks(), ISS, AUD, nonce="n1")
    assert claims["email"] == "dev@example.com"
    assert claims["groups"] == ["claude-gateway-users"]


def test_bad_signature_rejected(key):
    tok = key.id_token(ISS, AUD, nonce="n1")
    # Flip the last signature byte.
    head, payload, sig = tok.split(".")
    tampered = head + "." + payload + "." + sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
    with pytest.raises(JwtError):
        verify_jwt(tampered, key.jwks(), ISS, AUD, nonce="n1")


def test_tampered_payload_rejected(key):
    tok = key.id_token(ISS, AUD, nonce="n1", groups=["nope"])
    head, payload, sig = tok.split(".")
    # Re-sign nothing: swap in a different (validly-encoded) payload -> sig fails.
    other = key.id_token(ISS, AUD, nonce="n1", groups=["claude-gateway-users"]).split(".")[1]
    with pytest.raises(JwtError):
        verify_jwt(head + "." + other + "." + sig, key.jwks(), ISS, AUD, nonce="n1")


def test_wrong_audience_rejected(key):
    tok = key.id_token(ISS, "some-other-client", nonce="n1")
    with pytest.raises(JwtError, match="aud"):
        verify_jwt(tok, key.jwks(), ISS, AUD, nonce="n1")


def test_wrong_issuer_rejected(key):
    tok = key.id_token("https://evil.example.com", AUD, nonce="n1")
    with pytest.raises(JwtError, match="iss"):
        verify_jwt(tok, key.jwks(), ISS, AUD, nonce="n1")


def test_expired_token_rejected(key):
    tok = key.id_token(ISS, AUD, nonce="n1", exp_delta=-3600)
    with pytest.raises(JwtError, match="expired"):
        verify_jwt(tok, key.jwks(), ISS, AUD, nonce="n1")


def test_nonce_mismatch_rejected(key):
    tok = key.id_token(ISS, AUD, nonce="n1")
    with pytest.raises(JwtError, match="nonce"):
        verify_jwt(tok, key.jwks(), ISS, AUD, nonce="DIFFERENT")


def test_non_rs256_alg_rejected(key):
    # alg=none style downgrade must be refused even if the rest looks valid.
    tok = key.id_token(ISS, AUD, nonce="n1", alg="none")
    with pytest.raises(JwtError, match="alg"):
        verify_jwt(tok, key.jwks(), ISS, AUD, nonce="n1")


def test_unknown_kid_rejected(key):
    tok = key.id_token(ISS, AUD, nonce="n1", kid="rotated-away")
    with pytest.raises(JwtError, match="kid"):
        verify_jwt(tok, key.jwks(), ISS, AUD, nonce="n1")


def test_aud_list_form_accepted(key):
    tok = key.id_token(ISS, AUD, nonce="n1", extra={"aud": [AUD, "another"]})
    claims = verify_jwt(tok, key.jwks(), ISS, AUD, nonce="n1")
    assert AUD in claims["aud"]


def test_client_refetches_jwks_on_unknown_kid(config, key):
    """verify_id_token refetches the JWKS once on an unknown kid (Okta signing
    key rotation) instead of failing."""
    stale = {"keys": []}
    client = StubOidc(config, stale)
    # The refetch path calls _http_get_json(jwks_uri); return the real JWKS.
    client._http_get_json = lambda url, headers=None: key.jwks()
    tok = key.id_token(config.issuer, config.client_id, nonce="n1")
    claims = client.verify_id_token(tok, "n1")
    assert claims["sub"] == "00u123"
