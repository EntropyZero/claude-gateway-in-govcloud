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

ACCOUNT="$(account_id)"
REGISTRY="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${ECR_REPO_NAME}:${CLAUDE_VERSION}"

log "Ensuring ECR repository ${ECR_REPO_NAME} exists"
aws ecr describe-repositories --region "$AWS_REGION" \
    --repository-names "$ECR_REPO_NAME" >/dev/null 2>&1 || \
  aws ecr create-repository --region "$AWS_REGION" \
    --repository-name "$ECR_REPO_NAME" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability IMMUTABLE >/dev/null

log "Logging in to ${REGISTRY}"
aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "$REGISTRY"

log "Building ${IMAGE}"
docker build \
  --build-arg "CLAUDE_VERSION=${CLAUDE_VERSION}" \
  -t "$IMAGE" \
  "${REPO_ROOT}/docker"

log "Pushing ${IMAGE}"
docker push "$IMAGE"

set_env_var IMAGE_URI "$IMAGE"
log "Done. IMAGE_URI persisted to deploy.env (deploy-gateway.sh uses it)."
