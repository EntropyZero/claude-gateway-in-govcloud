#!/usr/bin/env bash
# Build the provisioned Grafana image from docker/grafana/ and push to ECR.
# For controlled networks, mirror the base image into your registry first
# and pass GRAFANA_BASE_IMAGE pointing at it.
source "$(dirname "$0")/common.sh"

GRAFANA_VERSION="${GRAFANA_VERSION:-11.5.1}"
GRAFANA_BASE_IMAGE="${GRAFANA_BASE_IMAGE:-grafana/grafana-oss:${GRAFANA_VERSION}}"
REPO_NAME="${GRAFANA_ECR_REPO_NAME:-claude-gw-grafana}"

ACCOUNT="$(account_id)"
REGISTRY="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPO_NAME}:${GRAFANA_VERSION}"

log "Ensuring ECR repository ${REPO_NAME} exists"
aws ecr describe-repositories --region "$AWS_REGION" \
    --repository-names "$REPO_NAME" >/dev/null 2>&1 || \
  aws ecr create-repository --region "$AWS_REGION" \
    --repository-name "$REPO_NAME" \
    --image-scanning-configuration scanOnPush=true >/dev/null

log "Logging in to ${REGISTRY}"
aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "$REGISTRY"

log "Building ${IMAGE} (base: ${GRAFANA_BASE_IMAGE})"
docker build \
  --build-arg "GRAFANA_BASE_IMAGE=${GRAFANA_BASE_IMAGE}" \
  -t "$IMAGE" \
  "${REPO_ROOT}/docker/grafana"

log "Pushing ${IMAGE}"
docker push "$IMAGE"

log "Done. Set GRAFANA_IMAGE=${IMAGE} in deploy.env"
