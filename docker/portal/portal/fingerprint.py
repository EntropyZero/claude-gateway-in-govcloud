"""Gateway TLS leaf-certificate fingerprint.

Opens a direct verified TLS connection to the gateway host (SNI = host;
ssl.create_default_context(), so the chain MUST verify against the image
trust store - which carries the enterprise CA), hashes the leaf cert DER with
SHA-256, and renders both common encodings:

  * OpenSSL style: uppercase colon-separated (AA:BB:...) - what
    verify-gateway.sh and IT publish, and
  * bare lowercase hex.

Both encodings are shown so the user can match whichever format their
client displays at its first-connect prompt.

Results are cached module-wide: successes for 300 s, failures for 30 s (so a
down gateway does not add a connect-timeout to every page view, but recovery
shows within half a minute). GIL single-assignment on the cache tuple is
thread-safe enough for gthread workers.
"""

import hashlib
import socket
import ssl
import time
import urllib.parse

_SUCCESS_TTL = 300
_FAILURE_TTL = 30

# (expires_at_monotonic, result_dict) or None
_cache = None


def clear_cache():
    global _cache
    _cache = None


def _host_port(gateway_url):
    parsed = urllib.parse.urlsplit(gateway_url)
    host = parsed.hostname
    if not host:
        raise ValueError("GATEWAY_URL has no host: %r" % gateway_url)
    return host, parsed.port or 443


def _fetch_leaf_der(host, port, timeout=10):  # pragma: no cover - network
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True)


def fingerprint_from_der(der):
    """Both display encodings of sha256(leaf DER)."""
    digest = hashlib.sha256(der).digest()
    return {
        "colon": ":".join("%02X" % b for b in digest),
        "hex": digest.hex(),
    }


def get_fingerprint(gateway_url, fetch=_fetch_leaf_der, now=None):
    """Return {"ok": True, "host": ..., "colon": ..., "hex": ...} or
    {"ok": False, "host": ..., "error": <safe message>}. Never raises for
    TLS/socket failures - the page renders the error instead of a 500."""
    global _cache
    now = time.monotonic() if now is None else now
    cached = _cache
    if cached is not None and now < cached[0]:
        return cached[1]
    host, port = _host_port(gateway_url)
    try:
        der = fetch(host, port)
        result = dict(fingerprint_from_der(der), ok=True, host=host)
        ttl = _SUCCESS_TTL
    except (OSError, ssl.SSLError, socket.timeout) as exc:
        # ssl.SSLCertVerificationError -> trust-store gap; plain OSError ->
        # unreachable. Render the reason, never a stack trace.
        result = {"ok": False, "host": host, "error": str(exc)}
        ttl = _FAILURE_TTL
    _cache = (now + ttl, result)
    return result
