#!/usr/bin/env bash
# Mirror the Grafana Amazon Managed Prometheus datasource plugin — the SigV4
# auth path for AMP since Grafana >=13.1 removed it from the core prometheus
# datasource. The plugin is NOT bundled upstream and the Grafana task has no
# egress to install it at boot, so it is baked into the image.
#
# EGRESS-HOST tool (.claude/rules/offline-build.md): run it where grafana.com
# is reachable, then copy mirror/ to the offline build machine —
# build-and-push-grafana.sh only consumes the staged artifact and never
# invokes this script. Standalone, like mirror-claude-release.sh: no
# common.sh, no deploy.env. Run from the repo root (or set MIRROR_DIR).
#
# Pin (version + sha256) lives in grafana-plugin.pin next to this script —
# the single source shared with the build-side verification.
#
# Prints exactly one line on stdout: the staged zip's path (logs go to
# stderr) — callers capture it with $(...).
set -euo pipefail

source "$(dirname "$0")/grafana-plugin.pin"

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
