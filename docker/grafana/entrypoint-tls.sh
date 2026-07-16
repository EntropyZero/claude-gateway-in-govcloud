#!/bin/sh
# Serve Grafana over TLS with a per-task self-signed certificate (SC-8 on
# the ALB->Grafana hop). The ALB re-encrypts to the target and does not
# validate target certificates, so the ephemeral cert encrypts the hop
# while the private key never leaves this task.
set -eu

TLS_DIR=/var/lib/grafana/tls
mkdir -p "$TLS_DIR"
(
  umask 077
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" \
    -days 3650 -subj "/CN=claude-gw-grafana" 2>/dev/null
)

export GF_SERVER_PROTOCOL=https
export GF_SERVER_CERT_FILE="$TLS_DIR/server.crt"
export GF_SERVER_CERT_KEY="$TLS_DIR/server.key"

exec /run.sh "$@"
