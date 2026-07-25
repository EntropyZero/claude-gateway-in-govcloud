#!/usr/bin/env bash
# Build the provisioned Grafana image from docker/grafana/ and push to ECR.
# For controlled networks, mirror the base image into your registry first
# and pass GRAFANA_BASE_IMAGE pointing at it.
source "$(dirname "$0")/common.sh"

# SECURITY NOTE (2026-07-25 base-image audit): the 11.x branch left security
# support 2026-06-15 and 11.5.1 predates the 11.5.3/11.5.5 CVE fixes; latest
# OSS is 13.x. An upgrade is NOT a quiet pin bump: v12.3 moved provisioning to
# a full-replace permissions model and v13 changes RBAC/datasource-UID
# handling, both touching docker/grafana/provisioning/, and the Okta SSO login
# is still unexercised live — verify the upgrade against a throwaway Grafana
# (provisioned AMP datasource, usage dashboard, Okta login) before rolling.
GRAFANA_VERSION="${GRAFANA_VERSION:-11.5.1}"
GRAFANA_BASE_IMAGE="${GRAFANA_BASE_IMAGE:-grafana/grafana-oss:${GRAFANA_VERSION}}"
REPO_NAME="${GRAFANA_ECR_REPO_NAME:-claude-gw-grafana}"
# The repo is tag-IMMUTABLE (this image bakes in the provisioned dashboard -
# it must not be silently overwritten). When you change provisioning without
# bumping Grafana, push under a new tag: GRAFANA_IMAGE_TAG=11.5.1-r2
GRAFANA_IMAGE_TAG="${GRAFANA_IMAGE_TAG:-${GRAFANA_VERSION}}"

# Generate the Grafana TLS leaf on the build host (openssl here, not in the
# Alpine image - keeps the image build free of any package-repo access). The
# ALB re-encrypts and does not validate this cert; it only encrypts the
# ALB->Grafana hop. Regenerated each build and gitignored.
TLS_DIR="${REPO_ROOT}/docker/grafana/tls"
log "Generating Grafana TLS cert"
mkdir -p "$TLS_DIR"
( umask 077
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" \
    -days 3650 -subj "/CN=claude-gw-grafana" 2>/dev/null )

# Optional enterprise/TLS-inspection root CA (e.g. Zscaler): trusted by the
# image so the Okta OAuth exchange verifies behind inspected egress. Staged
# as an empty file when unset - the Dockerfile skips the empty file.
if [ -n "${EXTRA_CA_CERT_PATH:-}" ]; then
  log "Staging extra root CA from ${EXTRA_CA_CERT_PATH}"
  cp "$EXTRA_CA_CERT_PATH" "${REPO_ROOT}/docker/grafana/extra-ca.pem"
else
  : > "${REPO_ROOT}/docker/grafana/extra-ca.pem"
fi

ensure_ecr_repo "$REPO_NAME"
REGISTRY="$(ecr_login)"
IMAGE="${REGISTRY}/${REPO_NAME}:${GRAFANA_IMAGE_TAG}"

log "Building ${IMAGE} (base: ${GRAFANA_BASE_IMAGE})"
docker build \
  --build-arg "GRAFANA_BASE_IMAGE=${GRAFANA_BASE_IMAGE}" \
  -t "$IMAGE" \
  "${REPO_ROOT}/docker/grafana"

log "Pushing ${IMAGE}"
docker push "$IMAGE"

set_env_var GRAFANA_IMAGE "$IMAGE"
log "Done. GRAFANA_IMAGE persisted to deploy.env (deploy-observability.sh uses it)."
