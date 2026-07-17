#!/usr/bin/env bash
# Build the gateway container from docker/ and push it to ECR.
# Run on a machine with Docker and the verified claude binary staged at
# docker/claude (linux-x64 glibc build — see client/mirror-claude-release.sh).
# Creates the ECR repository if it does not exist.
source "$(dirname "$0")/common.sh"

require_vars CLAUDE_VERSION ECR_REPO_NAME

BINARY="${REPO_ROOT}/docker/claude"
if [ ! -f "$BINARY" ]; then
  echo "FATAL: ${BINARY} not found." >&2
  echo "       Stage the GPG/checksum-verified linux-x64 binary there first" >&2
  echo "       (client/mirror-claude-release.sh downloads and verifies it)." >&2
  exit 1
fi

# RDS CA trust bundle, baked into the image so the gateway can connect with
# sslmode=verify-full (validates the DB server cert, not just encrypts).
# GovCloud has its own truststore domain; for commercial regions override:
#   RDS_CA_BUNDLE_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
RDS_CA_BUNDLE_URL="${RDS_CA_BUNDLE_URL:-https://truststore.pki.us-gov-west-1.rds.amazonaws.com/global/global-bundle.pem}"
log "Fetching RDS CA trust bundle"
curl -fsSL "$RDS_CA_BUNDLE_URL" -o "${REPO_ROOT}/docker/rds-ca-bundle.pem"

# Optional enterprise/TLS-inspection root CA (e.g. Zscaler): trusted by the
# image so inspected egress (the Okta hops) verifies. Staged as an empty file
# when unset - the Dockerfile skips the empty file.
if [ -n "${EXTRA_CA_CERT_PATH:-}" ]; then
  log "Staging extra root CA from ${EXTRA_CA_CERT_PATH}"
  cp "$EXTRA_CA_CERT_PATH" "${REPO_ROOT}/docker/extra-ca.pem"
else
  : > "${REPO_ROOT}/docker/extra-ca.pem"
fi

ensure_ecr_repo "$ECR_REPO_NAME"
REGISTRY="$(ecr_login)"
# Tags are IMMUTABLE: to rebuild the same claude version (Dockerfile fix,
# refreshed CA bundle) set IMAGE_TAG, e.g. IMAGE_TAG=2.1.207-r2. The pushed
# URI is persisted to deploy.env below, so deploy-gateway.sh picks it up.
IMAGE="${REGISTRY}/${ECR_REPO_NAME}:${IMAGE_TAG:-$CLAUDE_VERSION}"

# Amazon Linux 2023 base by default; point at your mirror (pinned by digest)
# for controlled networks: GATEWAY_BASE_IMAGE=<registry>/amazonlinux@sha256:...
GATEWAY_BASE_IMAGE="${GATEWAY_BASE_IMAGE:-public.ecr.aws/amazonlinux/amazonlinux:2023}"

# Listener TLS cert, generated here on the build host (openssl) and baked into
# the image - so the image needs no openssl and no Amazon Linux repo access at
# build. The ALB re-encrypts and does not validate this cert; it only encrypts
# the ALB->task hop. Regenerated each build and gitignored.
TLS_DIR="${REPO_ROOT}/docker/tls"
log "Generating gateway listener TLS cert"
mkdir -p "$TLS_DIR"
( umask 077
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" \
    -days 3650 -subj "/CN=claude-gateway" 2>/dev/null )

log "Building ${IMAGE} (base: ${GATEWAY_BASE_IMAGE})"
docker build \
  --build-arg "CLAUDE_VERSION=${CLAUDE_VERSION}" \
  --build-arg "GATEWAY_BASE_IMAGE=${GATEWAY_BASE_IMAGE}" \
  -t "$IMAGE" \
  "${REPO_ROOT}/docker"

log "Pushing ${IMAGE}"
docker push "$IMAGE"

set_env_var IMAGE_URI "$IMAGE"
log "Done. IMAGE_URI persisted to deploy.env (deploy-gateway.sh uses it)."
