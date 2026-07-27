#!/usr/bin/env bash
# Mirror the RDS CA trust bundle into mirror/rds-ca-bundle.pem. Both the
# gateway and the db-admin Lambda connect with sslmode/context verify-full
# and trust the bundle BAKED INTO their images — but the truststore is a
# public download endpoint the offline build machine cannot reach
# (.claude/rules/offline-build.md), so the fetch happens here.
#
# EGRESS-HOST tool: run it where the truststore is reachable, then copy
# mirror/ to the build machine — build-and-push-image.sh and
# build-and-push-dbadmin.sh consume the staged bundle and never fetch.
# Standalone, like mirror-claude-release.sh: no common.sh, no deploy.env.
# Run from the repo root (or set MIRROR_DIR).
#
# No checksum pin: AWS republishes the bundle as CAs rotate (that rotation is
# exactly why om-runbooks runbook 4 exists), so the sanity check is
# structural — the payload must parse as a non-empty PEM certificate set.
#
# GovCloud has its own truststore domain; for commercial regions override:
#   RDS_CA_BUNDLE_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
#
# Prints exactly one line on stdout: the staged bundle's path (logs go to
# stderr) — callers capture it with $(...).
set -euo pipefail

RDS_CA_BUNDLE_URL="${RDS_CA_BUNDLE_URL:-https://truststore.pki.us-gov-west-1.rds.amazonaws.com/global/global-bundle.pem}"
MIRROR_DIR="${MIRROR_DIR:-./mirror}"
OUT="${MIRROR_DIR}/rds-ca-bundle.pem"

mkdir -p "$MIRROR_DIR"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
echo "Fetching RDS CA trust bundle from ${RDS_CA_BUNDLE_URL}" >&2
curl -fsSL "$RDS_CA_BUNDLE_URL" -o "$TMP"

CERT_COUNT="$(grep -c 'BEGIN CERTIFICATE' "$TMP" || true)"
if [ "$CERT_COUNT" -lt 1 ]; then
  echo "FATAL: downloaded bundle contains no PEM certificates — refusing to stage it." >&2
  exit 1
fi
mv "$TMP" "$OUT"
trap - EXIT
echo "Staged ${CERT_COUNT} CA certificates: ${OUT}" >&2
echo "$OUT"
