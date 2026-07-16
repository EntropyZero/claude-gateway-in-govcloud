#!/usr/bin/env bash
# Mirror a pinned ADOT (AWS Distro for OpenTelemetry) collector image into
# your ECR and persist COLLECTOR_IMAGE (digest-pinned) into deploy.env - the
# same pattern as the other build scripts. The collector has no Dockerfile in
# this repo; it's an upstream image we mirror, pin by digest, and CMK-encrypt
# at rest (via KMS_KEY_ARN, set by deploy-database.sh).
#
# Run on a machine with Docker that can reach the upstream registry
# (public.ecr.aws) plus AWS creds. Pick the release with ADOT_VERSION.
source "$(dirname "$0")/common.sh"

ADOT_VERSION="${ADOT_VERSION:-v0.43.0}"
UPSTREAM="${COLLECTOR_UPSTREAM_IMAGE:-public.ecr.aws/aws-observability/aws-otel-collector:${ADOT_VERSION}}"
REPO_NAME="${ADOT_ECR_REPO_NAME:-claude-gw-adot}"

ensure_ecr_repo "$REPO_NAME"
REGISTRY="$(ecr_login)"
DEST="${REGISTRY}/${REPO_NAME}:${ADOT_VERSION}"

log "Mirroring ${UPSTREAM} -> ${DEST}"
docker pull "$UPSTREAM"
docker tag "$UPSTREAM" "$DEST"
docker push "$DEST"

# Resolve the pushed digest and pin COLLECTOR_IMAGE to it.
DIGEST="$(aws ecr describe-images --region "$AWS_REGION" \
  --repository-name "$REPO_NAME" --image-ids imageTag="$ADOT_VERSION" \
  --query 'imageDetails[0].imageDigest' --output text)"
set_env_var COLLECTOR_IMAGE "${REGISTRY}/${REPO_NAME}@${DIGEST}"
log "Done. COLLECTOR_IMAGE persisted to deploy.env (deploy-observability.sh uses it)."
