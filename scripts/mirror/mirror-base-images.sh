#!/usr/bin/env bash
# Mirror the four container BASE images (gateway / lambda / grafana / portal)
# into your ECR, digest-pinned, and persist GATEWAY_BASE_IMAGE /
# LAMBDA_BASE_IMAGE / GRAFANA_BASE_IMAGE / PORTAL_BASE_IMAGE into deploy.env -
# the build-and-push-*.sh scripts consume them. The upstream Docker Hub /
# public.ecr.aws defaults exist for dev convenience only: the offline build
# machine cannot reach either registry (.claude/rules/offline-build.md), so
# in the target profile this script runs BEFORE any image build.
#
# Run on a machine with Docker that can reach the upstream registries
# (Docker Hub + public.ecr.aws) plus AWS creds - the same dual-reach host
# profile as mirror-collector.sh (.claude/rules/offline-build.md carves out
# exactly this pull-and-push step; nothing lands in mirror/, the ECR copy IS
# the transferred artifact).
#
# Usage: mirror-base-images.sh [gateway|lambda|grafana|portal ...]
#   No arguments mirrors all four.
#
# Overrides:
#   GATEWAY_BASE_UPSTREAM / LAMBDA_BASE_UPSTREAM / GRAFANA_BASE_UPSTREAM /
#   PORTAL_BASE_UPSTREAM   - upstream ref per image (tag form preferred; a
#                            @sha256 ref works but yields a 'latest-<digest>'
#                            local tag). Distinct from the *_BASE_IMAGE vars
#                            this script WRITES, so a re-run never re-pulls
#                            from your own ECR.
#   GRAFANA_VERSION        - Grafana base tag; keep in sync with
#                            build-and-push-grafana.sh (same default, 13.1.1).
#   BASE_ECR_REPO_PREFIX   - local repo prefix; repos are <prefix>-gateway,
#                            <prefix>-lambda, <prefix>-grafana, <prefix>-portal.
source "$(dirname "$0")/../common.sh"

# Keep in sync with build-and-push-grafana.sh (its default base image is
# grafana/grafana:${GRAFANA_VERSION}; the OSS image is grafana/grafana -
# grafana-oss on Docker Hub is frozen at 12.4).
GRAFANA_VERSION="${GRAFANA_VERSION:-13.1.1}"

GATEWAY_BASE_UPSTREAM="${GATEWAY_BASE_UPSTREAM:-public.ecr.aws/amazonlinux/amazonlinux:2023}"
LAMBDA_BASE_UPSTREAM="${LAMBDA_BASE_UPSTREAM:-public.ecr.aws/lambda/python:3.12}"
GRAFANA_BASE_UPSTREAM="${GRAFANA_BASE_UPSTREAM:-grafana/grafana:${GRAFANA_VERSION}}"
PORTAL_BASE_UPSTREAM="${PORTAL_BASE_UPSTREAM:-public.ecr.aws/docker/library/python:3.12-slim}"

# One repo per base image, following the claude-gw-* convention of the other
# repos (claude-gw-adot, claude-gw-dbadmin, ...); CMK-encrypted + IMMUTABLE
# via ensure_ecr_repo.
BASE_ECR_REPO_PREFIX="${BASE_ECR_REPO_PREFIX:-${NAME_PREFIX:-claude-gw}-base}"

usage() {
  cat >&2 <<EOF
Usage: $(basename "$0") [gateway|lambda|grafana|portal ...]
  No arguments mirrors all four base images.
EOF
}

SELECTED=()
if [ "$#" -eq 0 ]; then
  SELECTED=(gateway lambda grafana portal)
else
  for arg in "$@"; do
    case "$arg" in
      gateway|lambda|grafana|portal) SELECTED+=("$arg") ;;
      *) echo "FATAL: unknown image '${arg}'" >&2; usage; exit 1 ;;
    esac
  done
fi

# ECR encryption is fixed at creation: a repo born without the CMK stays
# AES256 forever (.claude/rules/security.md - everything at rest uses the
# customer-managed key, including ECR at creation). Fail closed rather than
# silently creating four non-CMK repos; the override is a deliberate, named
# flag per the house verification pattern.
if [ -z "${KMS_KEY_ARN:-}" ] && [ "${ALLOW_NONCMK_BASE_REPOS:-}" != "1" ]; then
  echo "FATAL: KMS_KEY_ARN is not set - the base-image ECR repos would be created" >&2
  echo "       WITHOUT the customer-managed key, and ECR encryption cannot be" >&2
  echo "       changed after creation. Run scripts/deploy-database.sh first (it" >&2
  echo "       persists KMS_KEY_ARN into deploy.env), or copy that line from the" >&2
  echo "       deploy host's deploy.env. Set ALLOW_NONCMK_BASE_REPOS=1 only to" >&2
  echo "       deliberately create AES256-encrypted repos." >&2
  exit 1
fi

# mirror_one NAME UPSTREAM-REF LOCAL-REPO ENV-VAR
#
# LOCAL-TAG SCHEME: <upstream tag>-<12-hex short digest of the PULLED image>
# (e.g. 2023-a1b2c3d4e5f6). Upstream tags float (amazonlinux:2023 is
# re-published in place) while our repos are tag-IMMUTABLE, so mirroring
# under the bare upstream tag would collide on the first re-mirror after
# upstream moves. A digest suffix - rather than a date stamp - makes the tag
# a pure function of content: re-running against unchanged upstream derives
# the SAME tag, which already exists, so the push is skipped (idempotent);
# new upstream content derives a NEW tag, so there is no immutable-tag
# collision. A date stamp has neither property: a same-day re-mirror of NEW
# content collides, and a next-day re-run of IDENTICAL content mints a
# pointless duplicate.
mirror_one() {
  local name="$1" upstream="$2" repo="$3" var="$4"
  local tag_part repo_name arch upstream_digest short tag dest digest

  # Every consumer is x86_64: the 02/03/04 task definitions all pin
  # RuntimePlatform CpuArchitecture: X86_64 and the db-admin Lambda is
  # x86_64. Without the pin, docker pull selects the mirror host's native
  # arch (an arm64 laptop mirrors an arm64 base), and because the local tag
  # below derives from the arch-INDEPENDENT manifest-list digest, a later
  # re-run from the right host would skip the push - making the wrong-arch
  # mirror permanent. Pin the pull and assert what actually landed.
  log "[${name}] Pulling ${upstream} (linux/amd64)"
  docker pull --platform linux/amd64 "$upstream"
  arch="$(docker image inspect "$upstream" --format '{{.Os}}/{{.Architecture}}')"
  if [ "$arch" != "linux/amd64" ]; then
    echo "FATAL: [${name}] pulled ${arch} for ${upstream}; the Fargate tasks and" >&2
    echo "       the db-admin Lambda are linux/amd64. Does the upstream publish" >&2
    echo "       an amd64 variant?" >&2
    exit 1
  fi

  # Split the ref: repo name + tag ('latest' when neither tag nor digest).
  read -r repo_name tag_part <<<"$(split_image_ref "$upstream")"

  # Digest of what we just pulled, from the RepoDigests entry for this repo
  # (an image can carry digests from several registries). Docker normalizes
  # a docker.io/ prefix - and the library/ namespace of official images -
  # away in RepoDigests, so match all familiar forms.
  upstream_digest="$(docker image inspect "$upstream" \
      --format '{{range .RepoDigests}}{{println .}}{{end}}' \
    | awk -F'@' -v r="$repo_name" -v r2="${repo_name#docker.io/}" \
        -v r3="$(x="${repo_name#docker.io/}"; printf '%s' "${x#library/}")" \
        '$1 == r || $1 == r2 || $1 == r3 {print $2; exit}')"
  if [ -z "$upstream_digest" ]; then
    echo "FATAL: [${name}] cannot resolve the pulled digest of ${upstream}" >&2
    echo "       (no RepoDigests entry for ${repo_name} - was the pull re-tagged locally?)" >&2
    exit 1
  fi
  short="${upstream_digest#sha256:}"
  tag="${tag_part}-${short:0:12}"

  ensure_ecr_repo "$repo"
  dest="${REGISTRY}/${repo}:${tag}"

  if aws ecr describe-images --region "$AWS_REGION" \
       --repository-name "$repo" --image-ids imageTag="$tag" \
       >/dev/null 2>&1; then
    log "[${name}] ${repo}:${tag} already mirrored (upstream unchanged) - skipping push"
  else
    log "[${name}] Mirroring ${upstream} -> ${dest}"
    docker tag "$upstream" "$dest"
    docker push "$dest"
  fi

  # Pin to the digest ECR actually stores. It can legitimately differ from
  # the upstream digest (a multi-arch pull resolves a manifest LIST; the
  # push re-uploads the single-platform manifest), so always read it back.
  digest="$(aws ecr describe-images --region "$AWS_REGION" \
    --repository-name "$repo" --image-ids imageTag="$tag" \
    --query 'imageDetails[0].imageDigest' --output text)"
  set_env_var "$var" "${REGISTRY}/${repo}@${digest}"
}

REGISTRY="$(ecr_login)"

for name in ${SELECTED[@]+"${SELECTED[@]}"}; do
  case "$name" in
    gateway) mirror_one gateway "$GATEWAY_BASE_UPSTREAM" "${BASE_ECR_REPO_PREFIX}-gateway" GATEWAY_BASE_IMAGE ;;
    lambda)  mirror_one lambda  "$LAMBDA_BASE_UPSTREAM"  "${BASE_ECR_REPO_PREFIX}-lambda"  LAMBDA_BASE_IMAGE ;;
    grafana) mirror_one grafana "$GRAFANA_BASE_UPSTREAM" "${BASE_ECR_REPO_PREFIX}-grafana" GRAFANA_BASE_IMAGE ;;
    portal)  mirror_one portal  "$PORTAL_BASE_UPSTREAM"  "${BASE_ECR_REPO_PREFIX}-portal"  PORTAL_BASE_IMAGE ;;
  esac
done

log "Done. Digest-pinned *_BASE_IMAGE vars persisted to deploy.env (the build-and-push-*.sh scripts use them)."
log "If this host is not the build/deploy machine, copy the persisted *_BASE_IMAGE lines"
log "from scripts/deploy.env into that machine's deploy.env - set_env_var writes only locally."
