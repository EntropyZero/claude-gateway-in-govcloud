#!/usr/bin/env bash
# Build the DB admin Lambda image (app-user bootstrap + secret rotation)
# from docker/db-admin/ and push to ECR. Run on a machine with Docker and
# egress (pip pulls pg8000; the RDS CA bundle is fetched here too).
# For controlled networks, mirror the Lambda Python base image first and
# pass LAMBDA_BASE_IMAGE pointing at it (pin by digest).
source "$(dirname "$0")/common.sh"

# The repo is tag-IMMUTABLE: bump DBADMIN_VERSION for every app.py change
# (a same-tag rebuild cannot be pushed, and an unchanged DbAdminLambdaImage
# parameter would leave the deployed functions on the old code anyway).
DBADMIN_VERSION="${DBADMIN_VERSION:-1.0.0}"
LAMBDA_BASE_IMAGE="${LAMBDA_BASE_IMAGE:-public.ecr.aws/lambda/python:3.12}"
REPO_NAME="${DBADMIN_ECR_REPO_NAME:-claude-gw-dbadmin}"

# Same trust bundle the gateway image uses (sslmode/context verify-full).
RDS_CA_BUNDLE_URL="${RDS_CA_BUNDLE_URL:-https://truststore.pki.us-gov-west-1.rds.amazonaws.com/global/global-bundle.pem}"
log "Fetching RDS CA trust bundle"
curl -fsSL "$RDS_CA_BUNDLE_URL" -o "${REPO_ROOT}/docker/db-admin/rds-ca-bundle.pem"

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
