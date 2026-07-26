#!/usr/bin/env bash
# Mirror the Grafana Amazon Managed Prometheus datasource plugin — the SigV4
# auth path for AMP since Grafana >=13.1 removed it from the core prometheus
# datasource. The plugin is NOT bundled upstream and the Grafana task has no
# egress to install it at boot, so it is baked into the image;
# build-and-push-grafana.sh invokes this script (idempotent) and unzips the
# staged artifact into the build context.
#
# Standalone egress-host tool, like mirror-claude-release.sh: no common.sh,
# no deploy.env. Run from the repo root (or set MIRROR_DIR).
#
# Pin lives HERE (single source). To bump: pick the version, then
#   curl -s https://grafana.com/api/plugins/grafana-amazonprometheus-datasource/versions/<ver> \
#     | jq -r '.packages["linux-amd64"].sha256'
# and update BOTH values together.
#
# Prints exactly one line on stdout: the staged zip's path (logs go to
# stderr) — callers capture it with $(...).
set -euo pipefail

AMP_PLUGIN_ID="grafana-amazonprometheus-datasource"
AMP_PLUGIN_VERSION="${AMP_PLUGIN_VERSION:-3.1.0}"
# linux-amd64 (Fargate X86_64)
AMP_PLUGIN_SHA256="${AMP_PLUGIN_SHA256:-0374c5d7680ed86b904709a86f78a07f41fb263a9098df5c42d2371b6ea5a829}"

MIRROR_DIR="${MIRROR_DIR:-./mirror}"
OUT_DIR="${MIRROR_DIR}/grafana-plugins"
OUT="${OUT_DIR}/${AMP_PLUGIN_ID}-${AMP_PLUGIN_VERSION}-linux-amd64.zip"

if [ -f "$OUT" ] && echo "${AMP_PLUGIN_SHA256}  ${OUT}" | sha256sum -c - >/dev/null 2>&1; then
  echo "Already mirrored + verified: ${OUT}" >&2
  echo "$OUT"
  exit 0
fi

mkdir -p "$OUT_DIR"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
echo "Fetching ${AMP_PLUGIN_ID} ${AMP_PLUGIN_VERSION} (linux-amd64) from grafana.com" >&2
curl -fsSL "https://grafana.com/api/plugins/${AMP_PLUGIN_ID}/versions/${AMP_PLUGIN_VERSION}/download?os=linux&arch=amd64" -o "$TMP"
echo "${AMP_PLUGIN_SHA256}  ${TMP}" | sha256sum -c - >/dev/null \
  || { echo "FATAL: ${AMP_PLUGIN_ID} ${AMP_PLUGIN_VERSION} sha256 mismatch — refusing to stage an unverified plugin." >&2; exit 1; }
mv "$TMP" "$OUT"
trap - EXIT
echo "Verified + staged: ${OUT}" >&2
echo "$OUT"
