#!/usr/bin/env bash
# Refresh the vendored Python wheel sets from their requirements.txt pins —
# the one entry point for all Python dependency mirroring. Run on a host with
# PyPI egress; commit the resulting vendor/ changes, bump the affected image
# version (PORTAL_VERSION / DBADMIN_VERSION), rebuild, deploy.
#
#   scripts/mirror/mirror-python-deps.sh            # refresh every image vendor dir
#   scripts/mirror/mirror-python-deps.sh portal     # just docker/portal/vendor
#   scripts/mirror/mirror-python-deps.sh db-admin   # just docker/db-admin/vendor
#   scripts/mirror/mirror-python-deps.sh --tools    # stage operator-tooling wheels
#                                                   # into vendor/tools/ (gitignored)
#
# Image vendor dirs hold universal (pure-Python) wheels only and are COMMITTED
# so image builds need zero PyPI access (pip install --no-index). The dir is
# wiped before download so stale versions cannot linger next to new ones.
# vendor/tools/ is ephemeral staging for offline operator hosts (weasyprint &
# friends ship platform wheels, so mirror on a matching platform) — it is NOT
# committed; transfer it like a release mirror.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"

refresh_image_vendor() { # <component dir under docker/>
  local dir="${REPO_ROOT}/docker/$1" req vend
  req="${dir}/requirements.txt"; vend="${dir}/vendor"
  [ -f "$req" ] || { echo "FATAL: ${req} not found" >&2; exit 1; }
  echo "==> ${vend#"${REPO_ROOT}"/} from ${req#"${REPO_ROOT}"/}"
  rm -rf "$vend"; mkdir -p "$vend"
  # --only-binary=:all: → wheels only; a package that publishes no universal
  # wheel fails here (loudly) instead of landing an sdist the offline
  # Dockerfile pip cannot build. --python-version pins resolution to the
  # image interpreter (both images run 3.12) so a newer host python cannot
  # vendor a set the image's pip would reject.
  python3 -m pip download --only-binary=:all: --python-version 3.12 \
    -r "$req" -d "$vend" --quiet
  ls -l "$vend"
}

stage_tools_vendor() {
  local vend="${REPO_ROOT}/vendor/tools"
  echo "==> ${vend#"${REPO_ROOT}"/} from scripts/requirements-tools.txt (NOT committed)"
  rm -rf "$vend"; mkdir -p "$vend"
  python3 -m pip download -r "${REPO_ROOT}/scripts/requirements-tools.txt" -d "$vend" --quiet
  ls -l "$vend"
  echo "Install offline with:"
  echo "  pip install --no-index --find-links vendor/tools -r scripts/requirements-tools.txt"
}

case "${1:-all}" in
  all)       refresh_image_vendor portal; refresh_image_vendor db-admin ;;
  portal)    refresh_image_vendor portal ;;
  db-admin)  refresh_image_vendor db-admin ;;
  --tools)   stage_tools_vendor ;;
  *) echo "usage: $0 [all|portal|db-admin|--tools]" >&2; exit 64 ;;
esac
