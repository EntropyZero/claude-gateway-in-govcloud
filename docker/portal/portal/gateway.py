"""Gateway client: OAuth device flow (RFC 8628), spend-limits admin API, and
the effective-usage read API.

Spend-cap ADMIN calls act AS the signed-in admin: the portal deliberately
holds NO write-capable gateway key. Each admin obtains their own gateway
session token through the device flow (the same endpoints Claude Code itself
signs in with) and the gateway authorizes each call by the token's Okta groups
claim against its admin_groups config, recording the individual (oidc:<sub>)
in admin_audit.

The effective-usage READ path additionally accepts the deployment's read-only
admin key (x-api-key) - used server-side only, for the signed-in user's OWN
rows on /portal/me.
"""

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException

from .crypto import b64url_decode, sign_cookie
from .money import dollars_to_cents
from .selection import SelectionError

SPEND_SCOPES = ("user", "rbac_group", "organization")
SPEND_PERIODS = ("daily", "weekly", "monthly")


class GatewayError(Exception):
    """A gateway call failed in a way the UI should surface."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects: urllib's default handler forwards ALL
    request headers - including Authorization: Bearer - to the Location
    target with no same-host check, so a redirect (e.g. a later ALB
    listener-rule change) would replay the admin's gateway token to an
    arbitrary host. A 3xx from the gateway is returned as its status and
    treated as an unexpected response instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayClient:
    """Device flow + spend-limits API. Network methods are thin so tests can
    override them, mirroring OidcClient."""

    def __init__(self, config):
        self.config = config
        self.base = config.gateway_url

    # -- network primitive (overridable in tests) --
    def _http(self, method, url, headers=None, body=None):  # pragma: no cover - network
        """Return (status, parsed-json-or-None). 4xx/5xx do not raise: device
        flow and admin errors arrive as JSON bodies on 4xx responses."""
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Accept", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        ctx = ssl.create_default_context()
        opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPSHandler(context=ctx))
        try:
            with opener.open(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8"))
            except Exception:
                return exc.code, None
        except urllib.error.URLError as exc:
            # Network / TLS failure, not an HTTP status. Surfaced as a
            # GatewayError so the UI shows a diagnosable message instead of a
            # bare 500. A TLS verify error here means the container trust
            # store lacks the gateway ALB cert's chain - build-and-push-portal
            # stages GATEWAY_CA_BUNDLE/EXTRA_CA_CERT_PATH into the image.
            raise GatewayError("gateway unreachable: %s" % getattr(exc, "reason", exc))
        except (OSError, HTTPException, ValueError) as exc:
            # urllib wraps only errors during OPEN into URLError. A timeout /
            # connection reset / short read DURING the body read, or a 200
            # whose body is not JSON (LB error page), arrives as one of
            # these - the callers' contract is (status, doc) or GatewayError.
            raise GatewayError("gateway response failed: %r" % exc)

    def _post_form(self, url, data):
        body = urllib.parse.urlencode(data).encode("ascii")
        return self._http("POST", url, headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body)

    # -- device flow --
    def device_authorize(self):
        """Start the device flow. Returns the RFC 8628 authorization response
        (device_code, user_code, verification_uri[_complete], expires_in,
        interval). The empty body (no client_id) is gateway-specific behavior,
        runtime-verified on 2.1.220 - RFC 8628 nominally REQUIRES client_id
        for public clients, so revisit if a gateway upgrade starts rejecting
        this call."""
        status, doc = self._post_form(self.base + "/oauth/device_authorization", {})
        if status != 200 or not isinstance(doc, dict) or "device_code" not in doc:
            raise GatewayError("device authorization failed (HTTP %s)" % status)
        return doc

    def poll_token(self, device_code):
        """One token poll. Returns the token response dict on success,
        'pending' while authorization is outstanding, 'slow_down' when the
        gateway asks for backoff (RFC 8628: add 5s to the interval), or raises
        GatewayError when the grant is dead (expired/denied)."""
        status, doc = self._post_form(self.base + "/oauth/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        })
        if status == 200 and isinstance(doc, dict) and doc.get("access_token"):
            return doc
        err = (doc or {}).get("error", "")
        if err == "authorization_pending":
            return "pending"
        if err == "slow_down":
            return "slow_down"
        raise GatewayError("gateway sign-in %s" % (err or "failed (HTTP %s)" % status))

    def refresh(self, refresh_token):
        """Exchange a refresh token; returns the new token response or None
        (callers fall back to a fresh device-flow connect - including on a
        network failure, so a gateway blip degrades to a reconnect button)."""
        try:
            status, doc = self._post_form(self.base + "/oauth/token", {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            })
        except GatewayError:
            return None
        if status == 200 and isinstance(doc, dict) and doc.get("access_token"):
            return doc
        return None

    # -- spend-limits admin API (bearer = the admin's own gateway token) --
    def spend_api(self, method, token, path="", body=None):
        headers = {"Authorization": "Bearer " + token}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        url = self.base + "/v1/organizations/spend_limits" + path
        return self._http(method, url, headers=headers, body=data)

    # -- effective usage (caps + period-to-date spend) --
    def effective_usage(self, auth, *, periods=None, user_ids=None, q=None,
                        sort=None, page=None, limit=None):
        """GET /v1/organizations/spend_limits/effective.

        auth is ("bearer", <gateway session token>) for the per-admin path or
        ("api_key", <read key>) for the server-side self-view. Query-param
        contract (binary-verified against the 2.1.211 mirror):
          limit     1..1000 (default 20)
          period[]  repeatable, daily|weekly|monthly; default all three
                    (one row per user PER period)
          user_ids[] repeatable, <=100; disables paging (next_page null)
          q         substring search (<=256 chars)
          sort      'spend_desc' only; REQUIRES exactly one period[]
          page      opaque token from the previous response's next_page
        Returns (status, {"data": [...], "next_page": token-or-null}).
        """
        kind, cred = auth
        if kind == "bearer":
            headers = {"Authorization": "Bearer " + cred}
        elif kind == "api_key":
            headers = {"x-api-key": cred}
        else:
            raise ValueError("auth must be ('bearer', token) or ('api_key', key)")
        params = []
        if limit is not None:
            params.append(("limit", str(limit)))
        for p in periods or []:
            params.append(("period[]", p))
        for u in user_ids or []:
            params.append(("user_ids[]", u))
        if q:
            params.append(("q", q))
        if sort:
            params.append(("sort", sort))
        if page:
            params.append(("page", page))
        url = self.base + "/v1/organizations/spend_limits/effective"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._http("GET", url, headers=headers)


# Gateway spend-limit ids (spl_<hex>). A form-supplied id is interpolated
# into the DELETE URL path, so anything outside this shape is rejected
# before it reaches the wire.
SPEND_LIMIT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def resolve_user_email(gateway, token, email):
    """Resolve an email address to the gateway's user principal (oidc:<sub>).

    The gateway matches user-scope caps by EXACT principal string only
    (binary-verified 2.1.220, confirmed live 2026-07-29): a cap keyed by
    email or bare sub is accepted and stored but never applies. So the
    portal resolves the email first, via the effective-usage search (q= is
    a case-insensitive substring match; we then require exactly one user
    whose email matches exactly). Raises SelectionError with a
    user-renderable message on no match / ambiguity / gateway error."""
    status, doc = gateway.effective_usage(
        ("bearer", token), periods=["monthly"], q=email, limit=100)
    if status != 200 or not isinstance(doc, dict):
        raise SelectionError(
            "Gateway error resolving %r to a user id (HTTP %s) - the cap was "
            "NOT applied." % (email, status))
    wanted = email.lower()
    principals = {}
    for row in doc.get("data") or []:
        actor = (row or {}).get("actor") or {}
        addr = actor.get("email_address")
        if addr and addr.lower() == wanted and actor.get("user_id"):
            principals[actor["user_id"]] = addr
    if not principals:
        raise SelectionError(
            "No gateway user has the email %r. The user must have signed in "
            "to the gateway at least once; otherwise set the cap by their "
            "oidc:<sub> id (the Id column on the All-users page)." % email)
    if len(principals) > 1:
        raise SelectionError(
            "Email %r matches %d gateway users (%s) - set the cap by "
            "oidc:<sub> id instead." % (email, len(principals),
                                        ", ".join(sorted(principals))))
    return next(iter(principals))


def find_spend_limit_id(gateway, token, scope_type, scope_ids, period):
    """Locate a spend-limit row id for (scope_type, one of scope_ids, period)
    via the caps listing. scope_ids is an ordered preference list - callers
    pass [entered_id, resolved_principal] so a legacy email-keyed row is
    found before (never shadowed by) the principal-keyed one. Raises
    SelectionError when absent (or unlistable)."""
    status, doc = gateway.spend_api("GET", token, path="?limit=200")
    if status != 200 or not isinstance(doc, dict):
        raise SelectionError(
            "Gateway error listing caps (HTTP %s) - nothing was removed."
            % status)
    rows = {}
    for item in doc.get("data") or []:
        scope = (item or {}).get("scope") or {}
        item_scope_id = scope.get("user_id") or scope.get("rbac_group_id") or ""
        if (scope.get("type") == scope_type
                and item.get("period", "monthly") == period
                and item.get("id")):
            rows.setdefault(item_scope_id, item["id"])
    for scope_id in scope_ids:
        limit_id = rows.get(scope_id)
        if limit_id:
            if not SPEND_LIMIT_ID_RE.match(limit_id):
                raise SelectionError(
                    "Gateway returned an unexpected cap row id - nothing "
                    "was removed.")
            return limit_id
    raise SelectionError(
        "No %s cap row found for %s (%s) - nothing was removed."
        % (scope_type, " / ".join(scope_ids) or "organization", period))


def lookup_user_emails(gateway, token, user_ids):
    """principal -> email for the caps grid, via batched effective-usage
    user_ids[] lookups (<=100 per call, verified untruncated by limit).
    Best-effort: a failed batch just leaves those principals unmapped."""
    emails = {}
    ids = sorted(user_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            status, doc = gateway.effective_usage(
                ("bearer", token), periods=["monthly"], user_ids=chunk,
                limit=100)
        except GatewayError:
            continue
        if status != 200 or not isinstance(doc, dict):
            continue
        for row in doc.get("data") or []:
            actor = (row or {}).get("actor") or {}
            if actor.get("user_id") and actor.get("email_address"):
                emails[actor["user_id"]] = actor["email_address"]
    return emails


def build_spend_limit_body(scope_type, scope_id, amount, period):
    """The POST body for the gateway's spend-limits API (the same shape
    scripts/set-spend-limit.sh sends). amount is a DOLLAR string, converted
    to the API's cents-as-string. A None amount is REFUSED: posting
    amount:null does not clear a cap - it stores an UNLIMITED-override row
    that beats group/org caps (verified 2.1.220); removal is DELETE-by-id.
    Raises SelectionError / AmountError with a user-renderable message."""
    if amount is None:
        raise SelectionError(
            "a cap needs an amount - removing one deletes the row instead")
    if scope_type not in SPEND_SCOPES:
        raise SelectionError("scope must be one of: %s" % ", ".join(SPEND_SCOPES))
    if period not in SPEND_PERIODS:
        raise SelectionError("period must be one of: %s" % ", ".join(SPEND_PERIODS))
    if scope_type == "organization":
        if scope_id:
            raise SelectionError("the organization scope takes no user/group id")
        scope = {"type": "organization"}
    else:
        if not scope_id or len(scope_id) > 320 or any(ord(c) < 32 for c in scope_id):
            raise SelectionError("a %s cap needs a plain user/group id" % scope_type)
        key = "user_id" if scope_type == "user" else "rbac_group_id"
        scope = {"type": scope_type, key: scope_id}
    return {"scope": scope, "amount": dollars_to_cents(amount),
            "period": period, "currency": "USD"}


# Signed-cookie budget for the gateway token. Browsers enforce ~4096 bytes
# per cookie (RFC 6265 minimum) and DROP an oversized Set-Cookie silently -
# which would loop the admin back to the connect page with no error after a
# SUCCESSFUL device grant. Gateway session JWTs embed the Okta groups claim,
# so users with very many groups can genuinely hit this.
GW_COOKIE_BUDGET = 3800


def build_gw_cookie(token_response, session, secret, now=None):
    """The signed portal_gw cookie value for a token response, honoring the
    browser cookie cap: past the budget the refresh token is dropped first
    (re-connect on expiry is fine), then None is returned so the caller can
    render an explicit error instead of letting the browser discard the
    cookie silently. Returns (cookie, ttl_seconds) or (None, 0)."""
    now = int(time.time()) if now is None else now
    exp = min(gateway_token_exp(token_response, now=now), session["exp"])
    sub = gateway_token_sub(token_response)
    for rt in (token_response.get("refresh_token", ""), ""):
        cookie = sign_cookie(
            {"tok": token_response["access_token"], "rt": rt, "sub": sub,
             "exp": exp}, secret)
        if len(cookie) <= GW_COOKIE_BUDGET:
            return cookie, max(exp - now, 1)
    return None, 0


def gateway_token_exp(token_response, now=None):
    """Cookie expiry for a device-flow token: the JWT's own exp claim when it
    parses (the gateway session token is a JWS), else now+expires_in, else a
    conservative 15 minutes. The payload is NOT verified - the portal cannot
    (HS256, gateway-held key) and need not: the gateway re-verifies on every
    call; this only sizes the cookie lifetime."""
    now = int(time.time()) if now is None else now
    tok = token_response.get("access_token", "")
    parts = tok.split(".")
    if len(parts) == 3:
        try:
            exp = json.loads(b64url_decode(parts[1])).get("exp")
            if isinstance(exp, int):
                return exp
        except Exception:
            pass
    expires_in = token_response.get("expires_in")
    if isinstance(expires_in, int) and expires_in > 0:
        return now + expires_in
    return now + 900


def gateway_token_sub(token_response):
    """The gateway session token's sub claim - the exact value the gateway
    records as `oidc:<sub>` in admin_audit. Like gateway_token_exp, the
    payload is decoded UNVERIFIED: this feeds audit attribution/display only,
    never an authorization decision (the gateway verifies the token itself on
    every call). Empty string when the token has no parseable sub."""
    tok = token_response.get("access_token", "")
    parts = tok.split(".")
    if len(parts) == 3:
        try:
            sub = json.loads(b64url_decode(parts[1])).get("sub")
            if isinstance(sub, str):
                return sub
        except Exception:
            pass
    return ""
