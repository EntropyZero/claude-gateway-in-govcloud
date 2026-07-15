#!/usr/bin/env bash
# Build the provisioned Grafana image from docker/grafana/ and push to ECR.
# For controlled networks, mirror the base image into your registry first
# and pass GRAFANA_BASE_IMAGE pointing at it.
source "$(dirname "$0")/common.sh"

GRAFANA_VERSION="${GRAFANA_VERSION:-11.5.1}"
GRAFANA_BASE_IMAGE="${GRAFANA_BASE_IMAGE:-grafana/grafana-oss:${GRAFANA_VERSION}}"
REPO_NAME="${GRAFANA_ECR_REPO_NAME:-claude-gw-grafana}"
# The repo is tag-IMMUTABLE (this image bakes in the provisioned dashboard -
# it must not be silently overwritten). When you change provisioning without
# bumping Grafana, push under a new tag: GRAFANA_IMAGE_TAG=11.5.1-r2
GRAFANA_IMAGE_TAG="${GRAFANA_IMAGE_TAG:-${GRAFANA_VERSION}}"

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
