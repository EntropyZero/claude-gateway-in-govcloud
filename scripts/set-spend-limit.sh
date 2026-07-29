#!/usr/bin/env bash
# Create / update / clear a gateway spend cap (per user, per Okta group, or
# org-wide) via the gateway's admin API.
#
#   POST https://<GATEWAY_FQDN>/v1/organizations/spend_limits
#
# Caps are DATA, not CloudFormation: stack 02 configures the `admin:` block
# (which is what enables spend enforcement at all) and mints the admin keys,
# but the amounts live in the gateway's `spend_limits` table and are set here.
# No cap rows = no enforcement.
#
# Usage:
#   set-spend-limit.sh --scope user       --id <email|oidc-sub> --amount 50.00 [--period monthly]
#   set-spend-limit.sh --scope rbac_group --id claude-developers --amount 2500  [--period monthly]
#   set-spend-limit.sh --scope organization                     --amount 10000 [--period monthly]
#   set-spend-limit.sh --scope user --id <email|oidc-sub> --clear  # REMOVE the cap row
#   set-spend-limit.sh --list                               # show current caps
#
# --amount is DOLLARS (accepts 50 or 50.00); the API takes a whole-number
# decimal STRING of CENTS, which this script converts. Periods: daily | weekly
# | monthly (default monthly). Currency is USD-only, enforced by the gateway.
#
# User-cap identity: the gateway matches user caps by EXACT principal
# (oidc:<sub>) ONLY - an email- or bare-sub-keyed cap is stored but never
# applies (binary-verified 2.1.220, live-confirmed 2026-07-29). An --id
# containing '@' is therefore resolved to the principal first, via the
# gateway's effective-usage search; the user must have signed in at least
# once, else pass their oidc:<sub> directly. An --id already in oidc:...
# form is used verbatim (never resolved - some orgs put emails in subs).
#
# --clear DELETES the cap row. It must: a row left behind with a null amount
# is an explicit UNLIMITED override that beats group/org caps - the user
# would stop falling back to the org quota entirely. When clearing by email,
# a row keyed by the RAW email (a legacy dead row from the pre-2026-07-29
# behavior) is matched before the resolved principal's row, so legacy rows
# are removable here too; run --clear once per row.
#
# Precedence: a per-user cap wins over group caps. Multiple group caps combine
# per the stack's SpendGroupLimitMode (`min` = most restrictive wins).
#
# TLS: the gateway ALB cert is issued by your internal PKI. If curl fails with
# "unable to get local issuer certificate", point the script at the issuing CA
# CHAIN (issuing CA + root):
#   export GATEWAY_CA_BUNDLE=/path/to/org-ca-chain.pem
# GATEWAY_CA_BUNDLE and EXTRA_CA_CERT_PATH are ADDED to the system trust store
# (not swapped in for it), so verification still works when a TLS inspector
# re-signs the connection with a different trusted root. Never use -k.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${HERE}/common.sh"
[ -f "${HERE}/deploy.env" ] && . "${HERE}/deploy.env"

# ---- pure helpers (sourceable for tests via CLAUDE_SETSPEND_DOTSOURCE) ----

spend_urlencode() { # percent-encode $1 for a query-string value
  python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

# stdin = /spend_limits/effective JSON; $1 = email. Prints the single
# principal (oidc:<sub>) whose email matches exactly (case-insensitive), or
# errors: the q= search is substring, so near-miss rows must be filtered out.
parse_user_resolution() {
  python3 -c '
import json, sys
email = sys.argv[1].lower()
try:
    doc = json.load(sys.stdin)
except ValueError:
    sys.exit("[spend-limit] ERROR: unparseable gateway response (HTTP error?)")
found = {}
for row in doc.get("data", []):
    actor = (row or {}).get("actor") or {}
    addr = actor.get("email_address") or ""
    if addr.lower() == email and actor.get("user_id"):
        found[actor["user_id"]] = addr
if not found:
    sys.exit("[spend-limit] ERROR: no gateway user has the email %r. The user "
             "must have signed in at least once; otherwise pass their "
             "oidc:<sub> id (see --list or the portal All-users page)." % sys.argv[1])
if len(found) > 1:
    sys.exit("[spend-limit] ERROR: email %r matches %d users (%s) - pass the "
             "oidc:<sub> id instead." % (sys.argv[1], len(found), ", ".join(sorted(found))))
print(next(iter(found)))
' "$1"
}

# stdin = /spend_limits listing JSON; $1=scope $2=scope_id $3=period
# [$4=alt_id]. Prints the matching cap row id - the entered id is preferred
# over the resolved-principal alt, so a legacy email-keyed row is found
# before (never shadowed by) the principal-keyed one. Errors when no row.
parse_limit_row_id() {
  python3 -c '
import json, re, sys
scope_type, scope_id, period = sys.argv[1:4]
alt_id = sys.argv[4] if len(sys.argv) > 4 else ""
try:
    doc = json.load(sys.stdin)
except ValueError:
    sys.exit("[spend-limit] ERROR: unparseable gateway response (HTTP error?)")
rows = {}
for item in doc.get("data") or []:
    scope = (item or {}).get("scope") or {}
    item_id = scope.get("user_id") or scope.get("rbac_group_id") or ""
    if (scope.get("type") == scope_type
            and item.get("period", "monthly") == period and item.get("id")):
        rows.setdefault(item_id, item["id"])
for candidate in [scope_id] + ([alt_id] if alt_id else []):
    if candidate in rows:
        if not re.match(r"^[A-Za-z0-9_-]{1,64}$", rows[candidate]):
            sys.exit("[spend-limit] ERROR: gateway returned an unexpected row id %r" % rows[candidate])
        print(rows[candidate])
        sys.exit(0)
searched = scope_id + (" / " + alt_id if alt_id else "")
sys.exit("[spend-limit] ERROR: no %s cap row for %s (%s) - nothing to remove. "
         "Run --list to see the rows." % (scope_type, searched or "organization", period))
' "$1" "$2" "$3" "${4:-}"
}

# Test hook: stop before argument parsing / network setup.
if [ -n "${CLAUDE_SETSPEND_DOTSOURCE:-}" ]; then
  return 0 2>/dev/null || exit 0
fi

SCOPE=""; SCOPE_ID=""; AMOUNT=""; PERIOD="monthly"; CLEAR=0; LIST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --scope)  SCOPE="${2:?--scope needs a value}"; shift 2 ;;
    --id)     SCOPE_ID="${2:?--id needs a value}"; shift 2 ;;
    --amount) AMOUNT="${2:?--amount needs a value}"; shift 2 ;;
    --period) PERIOD="${2:?--period needs a value}"; shift 2 ;;
    --clear)  CLEAR=1; shift ;;
    --list)   LIST=1; shift ;;
    -h|--help) sed -n '2,47p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$PERIOD" in
  daily|weekly|monthly) ;;
  *) echo "--period must be daily, weekly or monthly (got '$PERIOD')" >&2; exit 2 ;;
esac

# CA trust for the gateway ALB cert. The ALB presents a cert for GATEWAY_FQDN
# issued by the org's internal PKI, which the system trust store does not carry,
# so curl fails "unable to get local issuer certificate". We NEVER pass -k (the
# repo rule is verification-fails-closed). --cacert REPLACES curl's default
# store, so handing it just one extra CA breaks whenever the chain curl
# actually sees terminates elsewhere (internal-PKI bundle configured but a
# Zscaler-inspected path presents the inspector's root, or vice versa).
# Instead, build a COMBINED bundle: system store + GATEWAY_CA_BUNDLE +
# EXTRA_CA_CERT_PATH - everything previously trusted stays trusted.
CA_EXTRAS=()
[ -n "${GATEWAY_CA_BUNDLE:-}" ] && CA_EXTRAS+=("$GATEWAY_CA_BUNDLE")
if [ -n "${EXTRA_CA_CERT_PATH:-}" ] && [ "${EXTRA_CA_CERT_PATH}" != "${GATEWAY_CA_BUNDLE:-}" ]; then
  CA_EXTRAS+=("$EXTRA_CA_CERT_PATH")
fi
CURL_CA=()
COMBINED_CA=""
# Every mktemp in this script registers here so the EXIT trap sweeps them on
# ANY exit - including Ctrl-C mid-curl, when the header file still holds the
# admin key.
API_TMPS=()
api_tmp() { local f; f="$(mktemp)"; chmod 600 "$f"; API_TMPS+=("$f"); printf '%s' "$f"; }
# [ -z ... ] || : the && form would make cleanup return 1 when COMBINED_CA is
# empty, and an EXIT trap's status REPLACES the script's own exit code under
# set -e - turning every successful run without CA extras into exit 1.
cleanup() {
  [ -z "$COMBINED_CA" ] || rm -f "$COMBINED_CA"
  rm -f ${API_TMPS[@]+"${API_TMPS[@]}"}
}
trap cleanup EXIT
if [ "${#CA_EXTRAS[@]}" -gt 0 ]; then
  COMBINED_CA="$(mktemp)"
  combined_ca_bundle "$COMBINED_CA" ${CA_EXTRAS[@]+"${CA_EXTRAS[@]}"}
  CURL_CA=(--cacert "$COMBINED_CA")
fi

# The admin key never goes on a command line (ps/-/proc leak): pull it into a
# variable and hand it to curl via a mode-600 header file.
key_secret_id() { printf '%s/%s' "$NAME_PREFIX" "$1"; }
fetch_key() {
  aws secretsmanager get-secret-value \
    --secret-id "$(key_secret_id "$1")" \
    --query SecretString --output text
}

api_raw() { # $1=method $2=key-name $3=path-suffix (may be ''/query); POST body on stdin.
  # Prints the raw response body; curl's exit code passes through. NOTE: no
  # set +e/-e sandwich here - an inner `set -e` would re-enable errexit in
  # the CALLER's suppressed window (verified: it made api()'s whole error
  # path unreachable). `|| rc=$?` captures the status without tripping errexit.
  local method="$1" keyname="$2" path="${3:-}" key hdr rc=0
  key="$(fetch_key "$keyname")"
  hdr="$(api_tmp)"
  printf 'x-api-key: %s\n' "$key" > "$hdr"
  unset key
  # ${arr[@]+"${arr[@]}"} - safe expansion of a possibly-empty array under set -u
  local args=(-sS --fail-with-body ${CURL_CA[@]+"${CURL_CA[@]}"} -X "$method" -H @"$hdr")
  [ "$method" = "POST" ] && args+=(-H 'content-type: application/json' --data-binary @-)
  curl "${args[@]}" "https://${GATEWAY_FQDN}/v1/organizations/spend_limits${path}" || rc=$?
  rm -f "$hdr"
  return $rc
}

api() { # $1=method $2=key-name $3=path-suffix; body on stdin (empty unless POST)
  local body rc=0
  body="$(api_tmp)"
  # `|| rc=$?`: capture the real status without tripping errexit on the
  # failure this function exists to report.
  api_raw "$1" "$2" "${3:-}" > "$body" || rc=$?
  cat "$body"; echo
  rm -f "$body"
  if [ "$rc" -ne 0 ]; then
    if [ "${#CA_EXTRAS[@]}" -eq 0 ]; then
      echo "[hint] if this was a TLS verification failure: the gateway ALB cert is" >&2
      echo "       issued by your internal PKI. Point this script at that CA chain:" >&2
      echo "         export GATEWAY_CA_BUNDLE=/path/to/org-ca-chain.pem   (or set" >&2
      echo "         EXTRA_CA_CERT_PATH in deploy.env). Do NOT work around it with -k." >&2
    else
      echo "[hint] if this was a TLS verification failure: it persisted with the" >&2
      echo "       combined CA bundle (system store + ${CA_EXTRAS[*]})." >&2
      echo "       Check that the file carries the FULL issuing chain for the ALB" >&2
      echo "       cert (issuing CA + root, PEM):" >&2
      echo "         openssl s_client -connect ${GATEWAY_FQDN}:443 -servername ${GATEWAY_FQDN} \\" >&2
      echo "           -showcerts </dev/null | openssl x509 -noout -issuer" >&2
      echo "       and compare that issuer against the CAs in the bundle." >&2
    fi
  fi
  return $rc
}

if [ "$LIST" = "1" ]; then
  require_vars GATEWAY_FQDN NAME_PREFIX
  echo "[spend-limit] current caps on https://${GATEWAY_FQDN}"
  api GET spend-admin-read-key < /dev/null
  exit 0
fi

case "$SCOPE" in
  user|rbac_group)
    [ -n "$SCOPE_ID" ] || { echo "--scope $SCOPE requires --id" >&2; exit 2; } ;;
  organization)
    [ -z "$SCOPE_ID" ] || { echo "--scope organization takes no --id" >&2; exit 2; } ;;
  *) echo "--scope must be user, rbac_group or organization" >&2; exit 2 ;;
esac

# amount: dollars -> whole cents (the API regex is ^\d{1,18}$ on a STRING).
# --clear takes no amount: it DELETES the cap row (below).
if [ "$CLEAR" != "1" ]; then
  [ -n "$AMOUNT" ] || { echo "--amount is required (or use --clear)" >&2; exit 2; }
  case "$AMOUNT" in
    *[!0-9.]*|*.*.*|'') echo "--amount must be a plain dollar figure, e.g. 50 or 50.00" >&2; exit 2 ;;
  esac
  CENTS="$(dollars_to_cents "$AMOUNT")" || exit 2
  [ "$CENTS" != "0" ] || { echo "--amount must be greater than zero (use --clear to remove a cap)" >&2; exit 2; }
  AMOUNT_JSON="\"${CENTS}\""
fi

require_vars GATEWAY_FQDN NAME_PREFIX

# fetch_to FILE METHOD PATH - admin GET into FILE; on HTTP/network failure
# prints the response body (the gateway's error JSON - distinguishes a bad
# key or 403 from "user not found") and fails. Keeps auth errors from
# masquerading as identity errors in the parsers downstream.
fetch_to() {
  local out="$1" path="$2"
  if ! api_raw GET spend-admin-write-key "$path" < /dev/null > "$out"; then
    echo "[spend-limit] ERROR: gateway request failed (${path}):" >&2
    cat "$out" >&2; echo >&2
    echo "[spend-limit] (connectivity/auth? try: $0 --list)" >&2
    return 1
  fi
}

# The gateway matches user caps by EXACT principal (oidc:<sub>) only, so an
# email --id must be resolved before anything is written. An id already in
# principal form (oidc:...) is NEVER resolved - in orgs whose Okta subs are
# themselves emails, principals legitimately contain '@'. The write key is
# accepted on the admin GETs (verified 2.1.220), so no read key is needed.
resolve_needed() {
  [ "$SCOPE" = "user" ] || return 1
  case "$SCOPE_ID" in
    oidc:*) return 1 ;;
    *@*)    return 0 ;;
    *)      return 1 ;;
  esac
}
EFFECTIVE_PATH="/effective?q=$(spend_urlencode "$SCOPE_ID")&period%5B%5D=monthly&limit=100"

if [ "$CLEAR" = "1" ]; then
  # DELETE the row. POSTing amount null instead leaves an UNLIMITED-override
  # row that beats group/org caps - the user stops falling back entirely.
  # The ENTERED id is matched first (parse_limit_row_id's preference order):
  # a legacy dead row is keyed by the raw email and must stay removable -
  # and must not shadow-delete the principal-keyed cap. Resolution is
  # best-effort here (an unresolvable email may still name a legacy row).
  ALT_ID=""
  if resolve_needed; then
    RESP="$(api_tmp)"
    if api_raw GET spend-admin-write-key "$EFFECTIVE_PATH" < /dev/null > "$RESP" 2>/dev/null; then
      ALT_ID="$(parse_user_resolution "$SCOPE_ID" < "$RESP" 2>/dev/null || true)"
    fi
    rm -f "$RESP"
  fi
  LISTING="$(api_tmp)"
  fetch_to "$LISTING" "?limit=200" || exit 1
  LIMIT_ID="$(parse_limit_row_id "$SCOPE" "$SCOPE_ID" "$PERIOD" "$ALT_ID" < "$LISTING")" || exit 1
  rm -f "$LISTING"
  echo "[spend-limit] ${SCOPE}${SCOPE_ID:+ ($SCOPE_ID)} -> removing cap row ${LIMIT_ID} (${PERIOD})"
  api DELETE spend-admin-write-key "/${LIMIT_ID}" < /dev/null
  echo "[spend-limit] done. Verify with: $0 --list"
  exit 0
fi

if resolve_needed; then
  echo "[spend-limit] resolving '${SCOPE_ID}' to the gateway principal..."
  RESP="$(api_tmp)"
  fetch_to "$RESP" "$EFFECTIVE_PATH" || exit 1
  PRINCIPAL="$(parse_user_resolution "$SCOPE_ID" < "$RESP")" || exit 1
  rm -f "$RESP"
  echo "[spend-limit] resolved to ${PRINCIPAL}"
  SCOPE_ID="$PRINCIPAL"
fi

case "$SCOPE" in
  user)         SCOPE_JSON="{\"type\":\"user\",\"user_id\":\"${SCOPE_ID}\"}" ;;
  rbac_group)   SCOPE_JSON="{\"type\":\"rbac_group\",\"rbac_group_id\":\"${SCOPE_ID}\"}" ;;
  organization) SCOPE_JSON="{\"type\":\"organization\"}" ;;
esac

echo "[spend-limit] ${SCOPE}${SCOPE_ID:+ ($SCOPE_ID)} -> \$${AMOUNT} per ${PERIOD}"
printf '{"scope":%s,"amount":%s,"period":"%s","currency":"USD"}' \
  "$SCOPE_JSON" "$AMOUNT_JSON" "$PERIOD" \
  | api POST spend-admin-write-key

echo "[spend-limit] done. Verify with: $0 --list"
