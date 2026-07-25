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

SCOPE=""; SCOPE_ID=""; AMOUNT=""; PERIOD="monthly"; CLEAR=0; LIST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --scope)  SCOPE="${2:?--scope needs a value}"; shift 2 ;;
    --id)     SCOPE_ID="${2:?--id needs a value}"; shift 2 ;;
    --amount) AMOUNT="${2:?--amount needs a value}"; shift 2 ;;
    --period) PERIOD="${2:?--period needs a value}"; shift 2 ;;
    --clear)  CLEAR=1; shift ;;
    --list)   LIST=1; shift ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
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
# [ -z ... ] || : the && form would make cleanup return 1 when COMBINED_CA is
# empty, and an EXIT trap's status REPLACES the script's own exit code under
# set -e - turning every successful run without CA extras into exit 1.
cleanup() { [ -z "$COMBINED_CA" ] || rm -f "$COMBINED_CA"; }
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

# $CLEAR is "0" or "1" (never empty), so ${CLEAR:+...} would ALWAYS expand.
CLEAR_TXT=""; [ "$CLEAR" = "1" ] && CLEAR_TXT="(cleared)"
echo "[spend-limit] ${SCOPE}${SCOPE_ID:+ ($SCOPE_ID)} -> ${CLEAR_TXT}${AMOUNT:+\$$AMOUNT} per ${PERIOD}"
printf '{"scope":%s,"amount":%s,"period":"%s","currency":"USD"}' \
  "$SCOPE_JSON" "$AMOUNT_JSON" "$PERIOD" \
  | api POST spend-admin-write-key

echo "[spend-limit] done. Verify with: $0 --list"
