"""Gateway TLS fingerprint: both display encodings (against a real
self-signed cert), the success/failure cache, and the page rendering."""

import datetime
import hashlib
import re

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from portal.fingerprint import (clear_cache, fingerprint_from_der,
                                get_fingerprint)

from conftest import Harness, session_cookie


@pytest.fixture(scope="module")
def cert_der():
    """A self-signed leaf cert's DER bytes (what getpeercert(True) returns)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         "claude-gateway.example.com")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER)


# ------------------------------------------------------------- encodings


def test_fingerprint_both_encodings_match_sha256(cert_der):
    fp = fingerprint_from_der(cert_der)
    digest = hashlib.sha256(cert_der).hexdigest()
    assert fp["hex"] == digest                       # bare lowercase hex
    # OpenSSL style: uppercase, colon-separated pairs.
    assert re.fullmatch(r"([0-9A-F]{2}:){31}[0-9A-F]{2}", fp["colon"])
    assert fp["colon"].replace(":", "").lower() == digest


# ------------------------------------------------------------- caching


def test_get_fingerprint_success_cached_300s(cert_der):
    calls = []

    def fetch(host, port):
        calls.append((host, port))
        return cert_der

    r1 = get_fingerprint("https://gw.example.com", fetch=fetch, now=1000.0)
    assert r1["ok"] is True and r1["host"] == "gw.example.com"
    assert calls == [("gw.example.com", 443)]
    # Within the TTL: served from cache, no second connection.
    r2 = get_fingerprint("https://gw.example.com", fetch=fetch, now=1299.0)
    assert r2 == r1 and len(calls) == 1
    # Past the TTL: refetched.
    get_fingerprint("https://gw.example.com", fetch=fetch, now=1301.0)
    assert len(calls) == 2


def test_get_fingerprint_failure_cached_only_30s(cert_der):
    calls = []

    def failing(host, port):
        calls.append(1)
        raise OSError("connection refused")

    r = get_fingerprint("https://gw.example.com", fetch=failing, now=1000.0)
    assert r["ok"] is False and "connection refused" in r["error"]
    assert r["host"] == "gw.example.com"
    # Failures are cached briefly (no connect timeout on every page view)...
    get_fingerprint("https://gw.example.com", fetch=failing, now=1029.0)
    assert len(calls) == 1
    # ...but recovery shows within ~30s.
    ok = get_fingerprint("https://gw.example.com",
                         fetch=lambda h, p: cert_der, now=1031.0)
    assert ok["ok"] is True


def test_get_fingerprint_honors_port_in_url(cert_der):
    seen = {}

    def fetch(host, port):
        seen["hp"] = (host, port)
        return cert_der

    get_fingerprint("https://gw.example.com:8443", fetch=fetch, now=0.0)
    assert seen["hp"] == ("gw.example.com", 8443)


# ------------------------------------------------------------- page


def _prime_cache(url, cert_der=None, error=None):
    """Populate the module cache the route will hit (the route uses the real
    network fetcher; the cache is the injection point)."""
    import time as _time
    if error is not None:
        def fetch(host, port):
            raise OSError(error)
    else:
        def fetch(host, port):
            return cert_der
    return get_fingerprint(url, fetch=fetch, now=_time.monotonic())


def test_fingerprint_page_requires_login():
    resp = Harness().get("/portal/fingerprint")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/portal/login"


def test_fingerprint_page_shows_both_encodings(cert_der):
    h = Harness()
    primed = _prime_cache(h.cfg.gateway_url, cert_der=cert_der)
    resp = h.get("/portal/fingerprint",
                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert primed["colon"].encode() in resp.data
    assert primed["hex"].encode() in resp.data
    # Guidance: compare at first connect; mismatch = interception.
    assert b"first connect" in resp.data.lower() \
        or b"contact it" in resp.data.lower()


def test_fingerprint_page_renders_failure_not_500(cert_der):
    h = Harness()
    _prime_cache(h.cfg.gateway_url, error="[SSL: CERTIFICATE_VERIFY_FAILED]")
    resp = h.get("/portal/fingerprint",
                 cookies={"portal_session": session_cookie()})
    assert resp.status_code == 200
    assert b"CERTIFICATE_VERIFY_FAILED" in resp.data
    assert b"Internal error" not in resp.data
    # No traceback leakage.
    assert b"Traceback" not in resp.data
