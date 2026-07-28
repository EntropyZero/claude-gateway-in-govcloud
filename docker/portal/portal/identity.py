"""The sub -> email identity map behind the audit page's Email column.

The gateway's admin_audit table records cap changes as `oidc:<sub>` (an
opaque Okta user id) and its schema is not ours to extend - so the portal
captures the pairing itself, at the only moment it holds both halves: an
admin connecting a gateway session (portal session cookie = Okta-verified
email, gateway token = sub). One small JSON object per sub (no read-modify-
write races between tasks) under a reserved prefix of the artifacts bucket;
the audit page joins against it. Only admins who have connected through the
portal since this shipped are mapped - break-glass CLI keys and older rows
render unmapped, which is honest.
"""

import json
import logging
import time

from .artifacts import read_s3_bytes

log = logging.getLogger("portal")

_PRINCIPAL_MAP_PREFIX = "identity/principal-emails/"
_SUB_CHARSET = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@-")


def principal_map_key(sub):
    """The S3 key for a sub's identity-map object, or None when the sub is
    empty/oversized/holds characters we won't put in an S3 key."""
    if not sub or len(sub) > 128 or not set(sub) <= _SUB_CHARSET:
        return None
    return _PRINCIPAL_MAP_PREFIX + sub + ".json"


def record_principal_email(s3, bucket, sub, email):
    """Best-effort persist of sub -> email; never fails the sign-in path."""
    key = principal_map_key(sub)
    if not key or not email:
        return
    body = json.dumps(
        {"sub": sub, "email": email,
         "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        separators=(",", ":")).encode()
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body,
                      ContentType="application/json")
    except Exception as exc:  # noqa: BLE001 - map write must never block admin
        log.error("identity-map write failed for %s: %s", sub, exc)


def lookup_principal_emails(s3, bucket, actors):
    """{actor -> email} for the `oidc:<sub>` actors that have a map object.
    Missing objects (never-connected admins, pre-feature rows) and S3 errors
    just leave the actor unmapped."""
    out = {}
    for actor in actors:
        if not actor.startswith("oidc:"):
            continue
        key = principal_map_key(actor[len("oidc:"):])
        if not key:
            continue
        try:
            doc = json.loads(read_s3_bytes(s3, bucket, key))
        except Exception:  # noqa: BLE001 - unmapped is a normal outcome
            continue
        email = doc.get("email", "") if isinstance(doc, dict) else ""
        if isinstance(email, str) and email:
            out[actor] = email
    return out
