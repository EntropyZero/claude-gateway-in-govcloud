#!/usr/bin/env bash
# Publish a verified Claude Code release + the installer to the portal's
# artifacts bucket, so the portal can assemble per-download ZIPs. Reuses the
# output of client/mirror-claude-release.sh (which downloaded and VERIFIED the
# binary against the GPG-signed manifest) - this script never fetches from the
# internet itself.
#
# Usage:
#   scripts/publish-portal-release.sh <version>
#   MIRROR_DIR=/mnt/share/mirror scripts/publish-portal-release.sh 2.1.207
#
# Uploads to the CMK-encrypted bucket (bucket default encryption applies the
# CMK; the caller needs kms:GenerateDataKey on it):
#   releases/<version>/claude.exe
#   releases/<version>/manifest.json
#   releases/<version>/CHECKSUMS.txt      (if present)
#   Install-ClaudeCode.ps1                (from client/)
#   extra-ca.pem                          (when EXTRA_CA_CERT_PATH is set)
source "$(dirname "$0")/common.sh"

VERSION="${1:?usage: publish-portal-release.sh <version>}"
MIRROR_DIR="${MIRROR_DIR:-${REPO_ROOT}/mirror}"
SRC="${MIRROR_DIR}/${VERSION}"

PORTAL_STACK_NAME="${PORTAL_STACK_NAME:-${NAME_PREFIX}-portal}"
BUCKET="${PORTAL_ARTIFACTS_BUCKET:-$(stack_output "$PORTAL_STACK_NAME" ArtifactsBucketName)}"
[ -n "$BUCKET" ] && [ "$BUCKET" != "None" ] || {
  echo "FATAL: could not resolve the portal artifacts bucket. Deploy 04-download-portal" >&2
  echo "       first (scripts/deploy-download-portal.sh), or set PORTAL_ARTIFACTS_BUCKET." >&2
  exit 1
}

# The win32-x64 binary + manifest are required; CHECKSUMS.txt is a convenience.
[ -f "${SRC}/claude.exe" ] || { echo "FATAL: ${SRC}/claude.exe not found (run client/mirror-claude-release.sh ${VERSION} win32-x64)." >&2; exit 1; }
[ -f "${SRC}/manifest.json" ] || { echo "FATAL: ${SRC}/manifest.json not found." >&2; exit 1; }

# Re-verify the exe against the manifest before publishing (defence in depth -
# the mirror already did, but the share could have been tampered with since).
EXPECTED="$(jq -re '.platforms["win32-x64"].checksum' "${SRC}/manifest.json")"
ACTUAL="$(sha256sum "${SRC}/claude.exe" | awk '{print $1}')"
if [ "$EXPECTED" != "$ACTUAL" ]; then
  echo "FATAL: claude.exe SHA-256 does not match the manifest - refusing to publish." >&2
  echo "  manifest: ${EXPECTED}" >&2
  echo "  actual:   ${ACTUAL}" >&2
  exit 2
fi
log "claude.exe checksum verified (${ACTUAL})"

INSTALLER="${REPO_ROOT}/client/Install-ClaudeCode.ps1"
[ -f "$INSTALLER" ] || { echo "FATAL: ${INSTALLER} not found." >&2; exit 1; }

log "Publishing ${VERSION} to s3://${BUCKET}"
aws s3 cp "${SRC}/claude.exe"     "s3://${BUCKET}/releases/${VERSION}/claude.exe"     --region "$AWS_REGION"
aws s3 cp "${SRC}/manifest.json"  "s3://${BUCKET}/releases/${VERSION}/manifest.json"  --region "$AWS_REGION"
[ -f "${SRC}/CHECKSUMS.txt" ] && \
  aws s3 cp "${SRC}/CHECKSUMS.txt" "s3://${BUCKET}/releases/${VERSION}/CHECKSUMS.txt" --region "$AWS_REGION"
aws s3 cp "$INSTALLER"            "s3://${BUCKET}/Install-ClaudeCode.ps1"             --region "$AWS_REGION"

if [ -n "${EXTRA_CA_CERT_PATH:-}" ]; then
  log "Uploading extra CA (${EXTRA_CA_CERT_PATH}); set PORTAL_BUNDLE_EXTRA_CA=true and redeploy 04 to include it in ZIPs"
  aws s3 cp "$EXTRA_CA_CERT_PATH" "s3://${BUCKET}/extra-ca.pem" --region "$AWS_REGION"
fi

cat <<EOF

Published ${VERSION}. If this is the version the portal serves, ensure the
stack's ReleaseVersion matches (PORTAL_RELEASE_VERSION / CLAUDE_VERSION) and
redeploy 04 if you changed it.
EOF
