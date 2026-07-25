#!/usr/bin/env bash
# Build the download-portal image from docker/portal/ and push to ECR.
# For controlled networks, mirror the Python base image into your registry
# first and pass PORTAL_BASE_IMAGE pointing at it (pin by digest). The base
# image is the ONLY egress requirement: boto3 is installed from vendored
# wheels (docker/portal/vendor/) and the TLS leaf is generated here, so the
# image build itself needs no PyPI or other package-repo access.
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

# Optional extra trust anchors, staged into the image's system store (the
# Dockerfile skips an empty file):
#   EXTRA_CA_CERT_PATH  - enterprise/TLS-inspection root (Zscaler etc.) for the
#                         outbound Okta OIDC calls behind inspected egress.
#   GATEWAY_CA_BUNDLE   - the internal-PKI chain of the gateway ALB cert. The
#                         spend-cap admin page calls the gateway at
#                         https://<GatewayFqdn> server-side (device flow +
#                         spend API); Python verifies against the system store,
#                         which does not carry an internal PKI. Same
#                         add-both-roots reasoning as set-spend-limit.sh.
# Files are newline-joined so adjacent PEM markers can never fuse.
: > "${REPO_ROOT}/docker/portal/extra-ca.pem"
CA_SOURCES=()
[ -n "${EXTRA_CA_CERT_PATH:-}" ] && CA_SOURCES+=("$EXTRA_CA_CERT_PATH")
if [ -n "${GATEWAY_CA_BUNDLE:-}" ] && [ "${GATEWAY_CA_BUNDLE}" != "${EXTRA_CA_CERT_PATH:-}" ]; then
  CA_SOURCES+=("$GATEWAY_CA_BUNDLE")
fi
for ca in ${CA_SOURCES[@]+"${CA_SOURCES[@]}"}; do
  log "Staging trust anchor from ${ca}"
  cat "$ca" >> "${REPO_ROOT}/docker/portal/extra-ca.pem"
  printf '\n' >> "${REPO_ROOT}/docker/portal/extra-ca.pem"
done

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
