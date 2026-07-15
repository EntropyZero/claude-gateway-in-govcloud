#!/usr/bin/env bash
# Post-deploy verification, run from a developer-side network (VPN/ZPA):
#   1. DNS resolves to private A records only (no AAAA - dualstack breaks /login)
#   2. TLS chain validates against the enterprise CA; prints the fingerprint
#      developers should see at the first-connect pinning prompt
#   3. Gateway OAuth endpoints respond
#
# ZPA caveat: from behind ZPA, Client Connector answers DNS with SYNTHETIC
# CGNAT (100.64/10) IPs regardless of whether the corporate CNAME actually
# exists - the real lookup happens on the App Connector. So a passing DNS
# check here does NOT prove the CNAME resolves at the connectors, and the
# AAAA check can false-fail on ZPA's synthetic IPv6 ranges. Run the DNS
# assertions from an App Connector host (or any machine using the same
# resolvers) for an authoritative answer.
source "$(dirname "$0")/common.sh"

require_vars GATEWAY_FQDN
FAIL=0

# 100.64.0.0/10 (CGNAT) - both the private-answer allowlist and the
# ZPA-synthetic detector must agree on this range.
CGNAT_RE='100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.'

log "1/3 DNS: ${GATEWAY_FQDN}"
A_RECORDS="$(dig +short A "$GATEWAY_FQDN" || true)"
AAAA_RECORDS="$(dig +short AAAA "$GATEWAY_FQDN" | grep -v '\.$' || true)"
if [ -z "$A_RECORDS" ]; then
  echo "    FAIL: no A records - is the corporate CNAME created?" ; FAIL=1
else
  echo "$A_RECORDS" | sed 's/^/    A: /'
  # Claude Code's /login check requires private (RFC1918 / CGNAT) answers.
  echo "$A_RECORDS" | grep -Ev "^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|${CGNAT_RE})" \
    | sed 's/^/    WARN public-range answer: /' || true
  # CGNAT answers = ZPA synthetic IPs: this machine's DNS view is the ZPA
  # overlay, not the real record. See the ZPA caveat in the header.
  if echo "$A_RECORDS" | grep -Eq "^${CGNAT_RE}"; then
    echo "    NOTE: synthetic ZPA (CGNAT) answers - DNS checks are not authoritative here;"
    echo "          re-run the DNS steps from an App Connector's resolution context."
  fi
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
  FP="$(echo "$CERT" | openssl x509 -noout -fingerprint -sha256)"
  # Authoritative MITM check: compare against the certificate imported into
  # ACM. This catches ZIA SSL inspection even when it re-signs with a
  # CORPORATE intermediate, which an issuer-name heuristic would miss.
  # Best effort - this vantage point may have no AWS credentials.
  ACM_FP=""
  if [ -n "${CERTIFICATE_ARN:-}" ]; then
    ACM_FP="$(aws acm get-certificate --region "$AWS_REGION" \
        --certificate-arn "$CERTIFICATE_ARN" --query Certificate --output text 2>/dev/null \
      | openssl x509 -noout -fingerprint -sha256 2>/dev/null || true)"
  fi
  ISSUER_DN="$(echo "$CERT" | openssl x509 -noout -issuer)"
  if [ -n "$ACM_FP" ] && [ "$FP" != "$ACM_FP" ]; then
    echo "    FAIL: served certificate does NOT match the one imported into ACM -" ; FAIL=1
    echo "          something is intercepting TLS on this path (ZIA inspection?)."
    echo "          Do NOT publish this fingerprint:"
  elif echo "$ISSUER_DN" | grep -qi 'zscaler'; then
    # Heuristic fallback when ACM isn't reachable from this vantage point.
    echo "    FAIL: certificate issued by Zscaler - the FQDN is being SSL-inspected (ZIA)." ; FAIL=1
    echo "          Fix the Zscaler bypass/app segment; this fingerprint must NOT be published:"
  elif [ -n "$ACM_FP" ]; then
    echo "    matches the ACM-imported certificate - published pinning fingerprint:"
  else
    echo "    (no AWS credentials here - could not cross-check against ACM)"
    echo "    published pinning fingerprint:"
  fi
  echo "    ${FP}"
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
