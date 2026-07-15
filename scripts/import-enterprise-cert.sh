#!/usr/bin/env bash
# Import an enterprise-CA-signed certificate into ACM (GovCloud) for the
# gateway ALB listener, and print the ARN to pass to 02-gateway.yaml.
#
# Workflow with your enterprise CA:
#   1. Generate key + CSR here (or on your PKI workstation):
#        ./import-enterprise-cert.sh csr claude-gateway.example.com
#   2. Have the enterprise CA sign the CSR (serverAuth EKU). Collect:
#        - the leaf certificate  (leaf.pem)
#        - the CA chain, intermediates first, root last (chain.pem)
#   3. Import:
#        ./import-enterprise-cert.sh import claude-gateway.example.com leaf.pem key.pem chain.pem
#
# Renewal: re-run 'import' with --certificate-arn to replace in place - the
# ALB listener picks up the new cert with no stack update. Remember rotation
# re-triggers Claude Code's first-connect fingerprint prompt: publish the new
# SHA-256 fingerprint first (this script prints it).
set -euo pipefail

# Helpers only (set_env_var) - this script may run on a PKI workstation with
# no deploy.env, so don't require one.
COMMON_SH_OPTIONAL_ENV=1 source "$(cd "$(dirname "$0")" && pwd)/common.sh"

REGION="${AWS_REGION:-us-gov-west-1}"
CMD="${1:-}"; FQDN="${2:-}"

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[ -n "$CMD" ] && [ -n "$FQDN" ] || usage

case "$CMD" in
  csr)
    # umask before creation so the key is never briefly world-readable on a
    # shared host - and remove any pre-existing key file first, because
    # umask only governs NEW files (openssl overwriting an existing 644 key
    # would keep its permissive mode).
    (
      umask 077
      rm -f "${FQDN}.key.pem"
      openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
        -keyout "${FQDN}.key.pem" -out "${FQDN}.csr" \
        -subj "/CN=${FQDN}" \
        -addext "subjectAltName=DNS:${FQDN}" \
        -addext "keyUsage=digitalSignature" \
        -addext "extendedKeyUsage=serverAuth"
    )
    echo "CSR written to ${FQDN}.csr - submit to the enterprise CA."
    echo "SAN must be exactly: DNS:${FQDN} (the corporate CNAME, not the ALB name)."
    ;;

  import)
    LEAF="${3:?leaf.pem required}"; KEY="${4:?key.pem required}"; CHAIN="${5:?chain.pem required}"
    ARN_FLAG=()
    [ "${6:-}" = "--certificate-arn" ] && ARN_FLAG=(--certificate-arn "${7:?arn required}")

    # Sanity: SAN matches, key matches, EKU includes serverAuth.
    openssl x509 -in "$LEAF" -noout -ext subjectAltName | grep -q "DNS:${FQDN}" \
      || { echo "FATAL: leaf SAN does not contain DNS:${FQDN}" >&2; exit 2; }
    openssl x509 -in "$LEAF" -noout -ext extendedKeyUsage | grep -qi "server auth" \
      || { echo "FATAL: leaf lacks serverAuth EKU" >&2; exit 2; }
    [ "$(openssl x509 -in "$LEAF" -noout -pubkey | openssl sha256)" = \
      "$(openssl pkey -in "$KEY" -pubout | openssl sha256)" ] \
      || { echo "FATAL: private key does not match certificate" >&2; exit 2; }

    ARN=$(aws acm import-certificate \
      --region "$REGION" \
      --certificate "fileb://${LEAF}" \
      --private-key "fileb://${KEY}" \
      --certificate-chain "fileb://${CHAIN}" \
      "${ARN_FLAG[@]}" \
      --query CertificateArn --output text)

    echo "CertificateArn: ${ARN}"
    set_env_var CERTIFICATE_ARN "$ARN"
    echo "Publish this fingerprint to developers (first-connect trust prompt):"
    openssl x509 -in "$LEAF" -noout -fingerprint -sha256
    echo "Reminder: imported certs do NOT auto-renew - alarm on expiry:"
    openssl x509 -in "$LEAF" -noout -enddate
    ;;

  *) usage ;;
esac
