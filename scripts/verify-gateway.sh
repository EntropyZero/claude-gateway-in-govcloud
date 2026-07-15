#!/usr/bin/env bash
# Post-deploy verification, run from a developer-side network (VPN/ZPA):
#   1. DNS resolves to private A records only (no AAAA - dualstack breaks /login)
#   2. TLS chain validates against the enterprise CA; prints the fingerprint
#      developers should see at the first-connect pinning prompt
#   3. Gateway OAuth endpoints respond
source "$(dirname "$0")/common.sh"

require_vars GATEWAY_FQDN
FAIL=0

log "1/3 DNS: ${GATEWAY_FQDN}"
A_RECORDS="$(dig +short A "$GATEWAY_FQDN" || true)"
AAAA_RECORDS="$(dig +short AAAA "$GATEWAY_FQDN" | grep -v '\.$' || true)"
if [ -z "$A_RECORDS" ]; then
  echo "    FAIL: no A records - is the corporate CNAME created?" ; FAIL=1
else
  echo "$A_RECORDS" | sed 's/^/    A: /'
  # Claude Code's /login check requires private (RFC1918 / CGNAT) answers.
  echo "$A_RECORDS" | grep -Ev '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.)' \
    | sed 's/^/    WARN public-range answer: /' || true
fi
if [ -n "$AAAA_RECORDS" ]; then
  echo "    FAIL: AAAA records present - ALB must be IPv4-only:" ; FAIL=1
  echo "$AAAA_RECORDS" | sed 's/^/      /'
else
  echo "    no AAAA records (good)"
fi

log "2/3 TLS certificate"
CERT="$(echo | openssl s_client -connect "${GATEWAY_FQDN}:443" -servername "$GATEWAY_FQDN" 2>/dev/null | openssl x509 2>/dev/null || true)"
if [ -z "$CERT" ]; then
  echo "    FAIL: no certificate returned (network path / Zscaler bypass?)" ; FAIL=1
else
  echo "$CERT" | openssl x509 -noout -subject -enddate | sed 's/^/    /'
  echo "    published pinning fingerprint:"
  echo "$CERT" | openssl x509 -noout -fingerprint -sha256 | sed 's/^/    /'
fi

log "3/3 Gateway endpoints"
ISSUER="$(curl -fsS --max-time 10 "https://${GATEWAY_FQDN}/.well-known/oauth-authorization-server" | jq -r .issuer 2>/dev/null || true)"
if [ "$ISSUER" = "https://${GATEWAY_FQDN}" ]; then
  echo "    oauth-authorization-server issuer OK: ${ISSUER}"
else
  echo "    FAIL: issuer '${ISSUER:-<none>}' (expected https://${GATEWAY_FQDN})" ; FAIL=1
fi
USER_CODE="$(curl -fsS --max-time 10 -X POST "https://${GATEWAY_FQDN}/oauth/device_authorization" | jq -r .user_code 2>/dev/null || true)"
if [ -n "$USER_CODE" ] && [ "$USER_CODE" != "null" ]; then
  echo "    device_authorization OK (user_code ${USER_CODE})"
else
  echo "    FAIL: device_authorization returned no user_code" ; FAIL=1
fi

echo
if [ "$FAIL" -eq 0 ]; then
  log "All checks passed - developers can run: claude -> /login -> Cloud gateway"
else
  log "Some checks FAILED - see above"
  exit 1
fi
