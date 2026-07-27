#!/usr/bin/env bash
# Build the DB admin Lambda image (app-user bootstrap + secret rotation)
# from docker/db-admin/ and push to ECR. OFFLINE build host
# (.claude/rules/offline-build.md): pip installs from the committed vendor/
# wheels (refresh via scripts/mirror/mirror-python-deps.sh db-admin, on the
# egress host), the RDS CA bundle comes pre-staged in the transferred
# mirror/ directory, and in the target profile the Lambda Python base image
# must come from your registry mirror (pass LAMBDA_BASE_IMAGE, pinned by
# digest).
source "$(dirname "$0")/common.sh"

# The repo is tag-IMMUTABLE: bump DBADMIN_VERSION for every app.py change
# (a same-tag rebuild cannot be pushed, and an unchanged DbAdminLambdaImage
# parameter would leave the deployed functions on the old code anyway).
DBADMIN_VERSION="${DBADMIN_VERSION:-1.0.0}"
LAMBDA_BASE_IMAGE="${LAMBDA_BASE_IMAGE:-public.ecr.aws/lambda/python:3.12}"
REPO_NAME="${DBADMIN_ECR_REPO_NAME:-claude-gw-dbadmin}"

# Same trust bundle the gateway image uses (sslmode/context verify-full),
# consumed from the transferred mirror/ — never fetched here (the truststore
# is a public download endpoint this host cannot reach).
RDS_CA_BUNDLE="${MIRROR_DIR:-${REPO_ROOT}/mirror}/rds-ca-bundle.pem"
require_mirrored_file "$RDS_CA_BUNDLE" "scripts/mirror/mirror-rds-ca-bundle.sh"
log "Staging RDS CA trust bundle from ${RDS_CA_BUNDLE}"
cp "$RDS_CA_BUNDLE" "${REPO_ROOT}/docker/db-admin/rds-ca-bundle.pem"

ensure_ecr_repo "$REPO_NAME" lambda
REGISTRY="$(ecr_login)"
IMAGE="${REGISTRY}/${REPO_NAME}:${DBADMIN_VERSION}"

log "Building ${IMAGE} (base: ${LAMBDA_BASE_IMAGE})"
docker build \
  --build-arg "LAMBDA_BASE_IMAGE=${LAMBDA_BASE_IMAGE}" \
  -t "$IMAGE" \
  "${REPO_ROOT}/docker/db-admin"

log "Pushing ${IMAGE}"
docker push "$IMAGE"

set_env_var DBADMIN_IMAGE "$IMAGE"
log "Done. DBADMIN_IMAGE persisted to deploy.env (deploy-gateway.sh uses it)."
