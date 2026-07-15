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

ensure_ecr_repo "$ECR_REPO_NAME"
REGISTRY="$(ecr_login)"
IMAGE="${REGISTRY}/${ECR_REPO_NAME}:${CLAUDE_VERSION}"

log "Building ${IMAGE}"
docker build \
  --build-arg "CLAUDE_VERSION=${CLAUDE_VERSION}" \
  -t "$IMAGE" \
  "${REPO_ROOT}/docker"

log "Pushing ${IMAGE}"
docker push "$IMAGE"

set_env_var IMAGE_URI "$IMAGE"
log "Done. IMAGE_URI persisted to deploy.env (deploy-gateway.sh uses it)."
