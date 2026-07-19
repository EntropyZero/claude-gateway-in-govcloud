#!/usr/bin/env bash
# Build the download-portal image from docker/portal/ and push to ECR.
# For controlled networks, mirror the Python base image into your registry
# first and pass PORTAL_BASE_IMAGE pointing at it (pin by digest).
source "$(dirname "$0")/common.sh"

# The repo is tag-IMMUTABLE: bump PORTAL_VERSION for every app.py change (a
# same-tag rebuild cannot be pushed, and an unchanged PORTAL_IMAGE parameter
# would leave the deployed service on the old code).
PORTAL_VERSION="${PORTAL_VERSION:-1.0.0}"
PORTAL_BASE_IMAGE="${PORTAL_BASE_IMAGE:-public.ecr.aws/docker/library/python:3.12-slim}"
REPO_NAME="${PORTAL_ECR_REPO_NAME:-claude-gw-portal}"

# Generate the portal TLS leaf on the build host (openssl here, not in the
# image - keeps the image build free of any package-repo access). The ALB
# re-encrypts and does not validate this cert; it only encrypts the ALB->portal
# hop. Regenerated each build and gitignored. umask 077 + rm so the private key
# is never briefly world-readable.
TLS_DIR="${REPO_ROOT}/docker/portal/tls"
log "Generating portal TLS cert"
mkdir -p "$TLS_DIR"
( umask 077
  rm -f "$TLS_DIR/server.key" "$TLS_DIR/server.crt"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" \
    -days 3650 -subj "/CN=claude-gw-portal" 2>/dev/null )

# Optional enterprise/TLS-inspection root CA (e.g. Zscaler): trusted by the
# image so the portal's outbound Okta OIDC calls verify behind inspected
# egress. Staged as an empty file when unset - the Dockerfile skips the empty
# file.
if [ -n "${EXTRA_CA_CERT_PATH:-}" ]; then
  log "Staging extra root CA from ${EXTRA_CA_CERT_PATH}"
  cp "$EXTRA_CA_CERT_PATH" "${REPO_ROOT}/docker/portal/extra-ca.pem"
else
  : > "${REPO_ROOT}/docker/portal/extra-ca.pem"
fi

ensure_ecr_repo "$REPO_NAME"
REGISTRY="$(ecr_login)"
IMAGE="${REGISTRY}/${REPO_NAME}:${PORTAL_IMAGE_TAG:-$PORTAL_VERSION}"

log "Building ${IMAGE} (base: ${PORTAL_BASE_IMAGE})"
docker build \
  --build-arg "PORTAL_BASE_IMAGE=${PORTAL_BASE_IMAGE}" \
  -t "$IMAGE" \
  "${REPO_ROOT}/docker/portal"

log "Pushing ${IMAGE}"
docker push "$IMAGE"

set_env_var PORTAL_IMAGE "$IMAGE"
log "Done. PORTAL_IMAGE persisted to deploy.env (deploy-download-portal.sh uses it)."
