#!/usr/bin/env bash
# Build the provisioned Grafana image from docker/grafana/ and push to ECR.
# OFFLINE build host (.claude/rules/offline-build.md): every external input
# is pre-staged — the AMP plugin zip arrives in the transferred mirror/
# directory (scripts/mirror/mirror-grafana-plugin.sh on the egress host),
# and the base image must come from your registry mirror in the target
# profile (pass GRAFANA_BASE_IMAGE; the Docker Hub default resolves only
# where Docker Hub is reachable).
source "$(dirname "$0")/common.sh"

# 13.1.1 = the 2026-07-25 upgrade off EOL 11.5.1 (11.x left security support
# 2026-06-15). Two upstream changes this script now absorbs:
#   * The OSS image moved: grafana/grafana-oss on Docker Hub is FROZEN as of
#     12.4 (stops at 13.0.2); the OSS image is grafana/grafana (Enterprise is
#     grafana/grafana-enterprise).
#   * Grafana >=13.1 removed SigV4 from the core prometheus datasource — AMP
#     auth now needs the grafana-amazonprometheus-datasource plugin, which is
#     NOT bundled, so this script stages it into the image (pinned + sha256).
# Verified against a throwaway 13.1.1 with --network none (provisioning,
# plugin signature with public-key retrieval disabled, uid-routed SigV4
# query path, dashboard render); the Okta login round-trip and the Fargate
# task-role credential path still need the live pass.
GRAFANA_VERSION="${GRAFANA_VERSION:-13.1.1}"
GRAFANA_BASE_IMAGE="${GRAFANA_BASE_IMAGE:-grafana/grafana:${GRAFANA_VERSION}}"
REPO_NAME="${GRAFANA_ECR_REPO_NAME:-claude-gw-grafana}"
# The repo is tag-IMMUTABLE (this image bakes in the provisioned dashboard -
# it must not be silently overwritten). When you change provisioning without
# bumping Grafana, push under a new tag: GRAFANA_IMAGE_TAG=13.1.1-r2
GRAFANA_IMAGE_TAG="${GRAFANA_IMAGE_TAG:-${GRAFANA_VERSION}}"

# Amazon Managed Prometheus datasource plugin, baked into the image (the
# task has no egress to install it at boot). Consumed from the transferred
# mirror/ — NEVER fetched here (this host cannot reach grafana.com). The
# pin is grafana-plugin.pin, shared with the mirror script, and the zip is
# re-verified against it so transfer corruption/tampering fails the build.
command -v unzip >/dev/null || { echo "FATAL: unzip is required (extracts the AMP datasource plugin preserving the backend binary's exec bit)." >&2; exit 1; }
source "${SCRIPT_DIR}/mirror/grafana-plugin.pin"
PLUGIN_ZIP="${MIRROR_DIR:-${REPO_ROOT}/mirror}/grafana-plugins/${AMP_PLUGIN_ID}-${AMP_PLUGIN_VERSION}-linux-amd64.zip"
require_mirrored_file "$PLUGIN_ZIP" "scripts/mirror/mirror-grafana-plugin.sh"
verify_sha256 "$PLUGIN_ZIP" "$AMP_PLUGIN_SHA256"
log "Staging the AMP datasource plugin from ${PLUGIN_ZIP} (verified)"
PLUGIN_DIR="${REPO_ROOT}/docker/grafana/plugins"
rm -rf "$PLUGIN_DIR"; mkdir -p "$PLUGIN_DIR"
unzip -q "$PLUGIN_ZIP" -d "$PLUGIN_DIR"

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
