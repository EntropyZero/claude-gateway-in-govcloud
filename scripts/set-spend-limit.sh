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
#   set-spend-limit.sh --scope user       --id <okta-sub|email> --amount 50.00 [--period monthly]
#   set-spend-limit.sh --scope rbac_group --id claude-developers --amount 2500  [--period monthly]
#   set-spend-limit.sh --scope organization                     --amount 10000 [--period monthly]
#   set-spend-limit.sh --scope user --id <sub> --clear      # remove the cap
#   set-spend-limit.sh --list                               # show current caps
#
# --amount is DOLLARS (accepts 50 or 50.00); the API takes a whole-number
# decimal STRING of CENTS, which this script converts. Periods: daily | weekly
# | monthly (default monthly). Currency is USD-only, enforced by the gateway.
#
# Precedence: a per-user cap wins over group caps. Multiple group caps combine
# per the stack's SpendGroupLimitMode (`min` = most restrictive wins).
#
# TLS: the gateway ALB cert is issued by your internal PKI. If curl fails with
# "unable to get local issuer certificate", point the script at the issuing CA:
#   export GATEWAY_CA_BUNDLE=/path/to/org-ca-chain.pem
# (falls back to EXTRA_CA_CERT_PATH). Never use -k.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${HERE}/common.sh"
[ -f "${HERE}/deploy.env" ] && . "${HERE}/deploy.env"

SCOPE=""; SCOPE_ID=""; AMOUNT=""; PERIOD="monthly"; CLEAR=0; LIST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --scope)  SCOPE="${2:?--scope needs a value}"; shift 2 ;;
    --id)     SCOPE_ID="${2:?--id needs a value}"; shift 2 ;;
    --amount) AMOUNT="${2:?--amount needs a value}"; shift 2 ;;
    --period) PERIOD="${2:?--period needs a value}"; shift 2 ;;
    --clear)  CLEAR=1; shift ;;
    --list)   LIST=1; shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
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
# repo rule is verification-fails-closed); instead point curl at the issuing CA
# bundle. Priority: GATEWAY_CA_BUNDLE (explicit) -> EXTRA_CA_CERT_PATH (the
# enterprise root CA already in deploy.env). curl also natively honors
# CURL_CA_BUNDLE / SSL_CERT_FILE from the environment.
CA_BUNDLE="${GATEWAY_CA_BUNDLE:-${EXTRA_CA_CERT_PATH:-}}"
CURL_CA=()
if [ -n "$CA_BUNDLE" ]; then
  [ -r "$CA_BUNDLE" ] || { echo "CA bundle not readable: $CA_BUNDLE" >&2; exit 1; }
  CURL_CA=(--cacert "$CA_BUNDLE")
fi

# The admin key never goes on a command line (ps/-/proc leak): pull it into a
# variable and hand it to curl via a mode-600 header file.
key_secret_id() { printf '%s/%s' "$NAME_PREFIX" "$1"; }
fetch_key() {
  aws secretsmanager get-secret-value \
    --secret-id "$(key_secret_id "$1")" \
    --query SecretString --output text
}

api() { # $1=method $2=key-name; body on stdin (empty for GET)
  local method="$1" keyname="$2" key hdr body rc
  key="$(fetch_key "$keyname")"
  hdr="$(mktemp)"; chmod 600 "$hdr"
  printf 'x-api-key: %s\n' "$key" > "$hdr"
  unset key
  body="$(mktemp)"; chmod 600 "$body"
  # ${arr[@]+"${arr[@]}"} - safe expansion of a possibly-empty array under set -u
  if [ "$method" = "GET" ]; then
    set +e
    curl -sS --fail-with-body ${CURL_CA[@]+"${CURL_CA[@]}"} -X GET \
      -H @"$hdr" \
      "https://${GATEWAY_FQDN}/v1/organizations/spend_limits" > "$body"
    rc=$?
    set -e
  else
    set +e
    curl -sS --fail-with-body ${CURL_CA[@]+"${CURL_CA[@]}"} -X "$method" \
      -H @"$hdr" -H 'content-type: application/json' \
      --data-binary @- \
      "https://${GATEWAY_FQDN}/v1/organizations/spend_limits" > "$body"
    rc=$?
    set -e
  fi
  rm -f "$hdr"
  cat "$body"; echo
  rm -f "$body"
  if [ "$rc" -ne 0 ] && [ -z "$CA_BUNDLE" ]; then
    echo "[hint] TLS verification failed and no CA bundle is set. The gateway ALB" >&2
    echo "       cert is issued by your internal PKI. Point this script at that CA:" >&2
    echo "         export GATEWAY_CA_BUNDLE=/path/to/org-ca-chain.pem   (or set" >&2
    echo "         EXTRA_CA_CERT_PATH in deploy.env). Do NOT work around it with -k." >&2
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

# amount: dollars -> whole cents (the API regex is ^\d{1,18}$ on a STRING),
# or JSON null to clear the cap.
if [ "$CLEAR" = "1" ]; then
  AMOUNT_JSON='null'
else
  [ -n "$AMOUNT" ] || { echo "--amount is required (or use --clear)" >&2; exit 2; }
  case "$AMOUNT" in
    *[!0-9.]*|*.*.*|'') echo "--amount must be a plain dollar figure, e.g. 50 or 50.00" >&2; exit 2 ;;
  esac
  CENTS="$(dollars_to_cents "$AMOUNT")" || exit 2
  [ "$CENTS" != "0" ] || { echo "--amount must be greater than zero (use --clear to remove a cap)" >&2; exit 2; }
  AMOUNT_JSON="\"${CENTS}\""
fi

case "$SCOPE" in
  user)         SCOPE_JSON="{\"type\":\"user\",\"user_id\":\"${SCOPE_ID}\"}" ;;
  rbac_group)   SCOPE_JSON="{\"type\":\"rbac_group\",\"rbac_group_id\":\"${SCOPE_ID}\"}" ;;
  organization) SCOPE_JSON="{\"type\":\"organization\"}" ;;
esac

require_vars GATEWAY_FQDN NAME_PREFIX

echo "[spend-limit] ${SCOPE}${SCOPE_ID:+ ($SCOPE_ID)} -> ${CLEAR:+(cleared)}${AMOUNT:+\$$AMOUNT} per ${PERIOD}"
printf '{"scope":%s,"amount":%s,"period":"%s","currency":"USD"}' \
  "$SCOPE_JSON" "$AMOUNT_JSON" "$PERIOD" \
  | api POST spend-admin-write-key

echo "[spend-limit] done. Verify with: $0 --list"
