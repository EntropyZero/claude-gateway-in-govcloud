#!/usr/bin/env bash
# Mirror a pinned Claude Code release for offline distribution.
#
# Run on a machine WITH egress to downloads.claude.ai. Downloads the release
# manifest and the requested platform binaries, verifies each binary's SHA-256
# against the manifest, and (when a signature + public key are available)
# GPG-verifies the manifest itself. Stage the output directory on the internal
# file share; nothing downstream (laptops, the container build) needs egress.
#
# Usage:
#   ./mirror-claude-release.sh <version> [platform ...]
#   ./mirror-claude-release.sh 2.1.207                      # linux-x64 + win32-x64
#   ./mirror-claude-release.sh 2.1.207 win32-x64
#
# Output layout (MIRROR_DIR, default ./mirror):
#   mirror/<version>/manifest.json
#   mirror/<version>/claude            (linux-x64 -> docker/claude for the image build)
#   mirror/<version>/claude.exe        (win32-x64 -> Install-ClaudeCode.ps1 -BinaryPath)
#   mirror/<version>/CHECKSUMS.txt     (per-platform sha256, from the manifest)
#
# Env overrides:
#   BASE_URL          release bucket (default https://downloads.claude.ai/claude-code-releases)
#   MIRROR_DIR        output directory (default ./mirror)
#   ANTHROPIC_GPG_KEY path to Anthropic's release-signing public key; enables
#                     GPG verification of the manifest signature
set -euo pipefail

BASE_URL="${BASE_URL:-https://downloads.claude.ai/claude-code-releases}"
MIRROR_DIR="${MIRROR_DIR:-./mirror}"

VERSION="${1:?usage: mirror-claude-release.sh <version> [platform ...]}"
shift
PLATFORMS=("$@")
[ "${#PLATFORMS[@]}" -gt 0 ] || PLATFORMS=(linux-x64 win32-x64)

command -v jq >/dev/null || { echo "FATAL: jq is required" >&2; exit 1; }

OUT="${MIRROR_DIR}/${VERSION}"
mkdir -p "$OUT"

echo "==> Fetching manifest for ${VERSION}"
curl -fsSL "${BASE_URL}/${VERSION}/manifest.json" -o "${OUT}/manifest.json"

# --- GPG verification of the signed manifest --------------------------------
# Fail closed: without GPG verification the whole pipeline trusts the
# manifest on TLS alone. Override only deliberately, with
# ALLOW_UNVERIFIED_MANIFEST=1.
if curl -fsSL "${BASE_URL}/${VERSION}/manifest.json.sig" -o "${OUT}/manifest.json.sig" 2>/dev/null; then
  if [ -n "${ANTHROPIC_GPG_KEY:-}" ]; then
    echo "==> GPG-verifying manifest signature"
    GNUPGHOME="$(mktemp -d)"; export GNUPGHOME
    trap 'rm -rf "$GNUPGHOME"' EXIT
    gpg --quiet --import "$ANTHROPIC_GPG_KEY"
    gpg --verify "${OUT}/manifest.json.sig" "${OUT}/manifest.json"
    echo "    manifest signature OK"
  elif [ "${ALLOW_UNVERIFIED_MANIFEST:-}" = "1" ]; then
    echo "WARN: ANTHROPIC_GPG_KEY unset - manifest NOT verified (ALLOW_UNVERIFIED_MANIFEST=1)" >&2
  else
    echo "FATAL: manifest.json.sig exists but ANTHROPIC_GPG_KEY is unset." >&2
    echo "       Supply Anthropic's release-signing public key, or set" >&2
    echo "       ALLOW_UNVERIFIED_MANIFEST=1 to accept TLS-only trust." >&2
    exit 3
  fi
elif [ "${ALLOW_UNVERIFIED_MANIFEST:-}" = "1" ]; then
  echo "WARN: no manifest signature published - proceeding unverified (ALLOW_UNVERIFIED_MANIFEST=1)" >&2
  rm -f "${OUT}/manifest.json.sig"
else
  rm -f "${OUT}/manifest.json.sig"
  echo "FATAL: no manifest signature published at ${BASE_URL}/${VERSION}/manifest.json.sig" >&2
  echo "       Set ALLOW_UNVERIFIED_MANIFEST=1 to accept TLS-only trust." >&2
  exit 3
fi

# --- Binaries ----------------------------------------------------------------
: > "${OUT}/CHECKSUMS.txt"
for platform in "${PLATFORMS[@]}"; do
  expected="$(jq -re --arg p "$platform" '.platforms[$p].checksum' "${OUT}/manifest.json")" || {
    echo "FATAL: platform '${platform}' not in manifest. Available:" >&2
    jq -r '.platforms | keys[]' "${OUT}/manifest.json" | sed 's/^/  /' >&2
    exit 1
  }

  case "$platform" in
    win32-*) local_name="claude.exe" ;;
    *)       local_name="claude" ;;
  esac

  echo "==> Downloading ${platform}"
  # Windows builds may be published as claude.exe - try both names.
  curl -fsSL "${BASE_URL}/${VERSION}/${platform}/claude" -o "${OUT}/${local_name}" \
    || curl -fsSL "${BASE_URL}/${VERSION}/${platform}/claude.exe" -o "${OUT}/${local_name}"

  actual="$(sha256sum "${OUT}/${local_name}" | awk '{print $1}')"
  if [ "$actual" != "$expected" ]; then
    echo "FATAL: SHA-256 mismatch for ${platform}" >&2
    echo "  manifest: ${expected}" >&2
    echo "  actual:   ${actual}" >&2
    rm -f "${OUT}/${local_name}"
    exit 2
  fi
  echo "    checksum OK (${actual})"
  echo "${actual}  ${local_name}  (${platform})" >> "${OUT}/CHECKSUMS.txt"
  [ "$platform" = linux-x64 ] && chmod 0755 "${OUT}/claude"
done

cat <<EOF

Mirrored to ${OUT}/
Next:
  - Container build: cp ${OUT}/claude <repo>/docker/claude && scripts/build-and-push-image.sh
  - Windows rollout: stage ${OUT}/claude.exe + CHECKSUMS.txt on the file share;
    pass the win32-x64 checksum to Install-ClaudeCode.ps1 -Sha256
EOF
