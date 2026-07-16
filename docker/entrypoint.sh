#!/bin/sh
# Claude apps gateway entrypoint.
# 1. Materializes /etc/claude/gateway.yaml from GATEWAY_CONFIG_B64 (rendered
#    by CloudFormation; secret placeholders like ${OIDC_CLIENT_SECRET} are
#    left intact for the gateway's own env expansion).
# 2. Assembles GATEWAY_POSTGRES_URL from the PG* variables that ECS injects
#    from the RDS-managed Secrets Manager secret, URL-encoding the password.
set -eu

if [ -z "${GATEWAY_CONFIG_B64:-}" ]; then
  echo "[entrypoint] FATAL: GATEWAY_CONFIG_B64 is not set" >&2
  exit 1
fi

umask 077
printf '%s' "$GATEWAY_CONFIG_B64" | base64 -d > /etc/claude/gateway.yaml

# Optional telemetry block, rendered as a separate env var by CloudFormation
# so the observability stack can be toggled without duplicating the config.
if [ -n "${GATEWAY_TELEMETRY_B64:-}" ]; then
  printf '\n' >> /etc/claude/gateway.yaml
  printf '%s' "$GATEWAY_TELEMETRY_B64" | base64 -d >> /etc/claude/gateway.yaml
fi

# Per-task self-signed TLS cert for the gateway listener (paths match the
# listen.tls block in the rendered config). The ALB re-encrypts to the
# target and does not validate target certificates, so an ephemeral
# per-task cert encrypts the hop while the key never leaves this task.
# Client-side fingerprint pinning is unaffected - developers pin the
# ALB-presented enterprise cert, not this one.
mkdir -p /etc/claude/tls
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout /etc/claude/tls/server.key -out /etc/claude/tls/server.crt \
  -days 3650 -subj "/CN=claude-gateway-task" 2>/dev/null

# Percent-encode every byte that is not RFC 3986 'unreserved', so any
# password RDS generates survives inside a postgres:// URL.
urlencode() {
  _in=$1 _out=''
  while [ -n "$_in" ]; do
    _c=$(printf '%.1s' "$_in")
    _in=${_in#?}
    case $_c in
      [A-Za-z0-9.~_-]) _out="${_out}${_c}" ;;
      *) _out="${_out}$(printf '%%%02X' "'$_c")" ;;
    esac
  done
  printf '%s' "$_out"
}

: "${PGHOST:?}" "${PGPORT:?}" "${PGDATABASE:?}" "${PGUSER:?}" "${PGPASSWORD:?}"
# verify-full: encrypt AND validate the server certificate against the RDS
# CA bundle baked into the image (plain 'require' accepts any cert - no
# protection against an in-VPC MITM).
GATEWAY_POSTGRES_URL="postgres://$(urlencode "$PGUSER"):$(urlencode "$PGPASSWORD")@${PGHOST}:${PGPORT}/${PGDATABASE}?sslmode=verify-full&sslrootcert=/usr/local/share/rds-ca-bundle.pem"
export GATEWAY_POSTGRES_URL
unset PGPASSWORD GATEWAY_CONFIG_B64 GATEWAY_TELEMETRY_B64

echo "[entrypoint] starting claude gateway ($(claude --version 2>/dev/null || echo version-unknown))"
exec claude gateway --config /etc/claude/gateway.yaml
