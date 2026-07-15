#!/usr/bin/env bash
# Shared helpers for the deploy scripts. Source, don't execute:
#   source "$(dirname "$0")/common.sh"
# Loads scripts/deploy.env and provides require_vars / stack_output.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${DEPLOY_ENV_FILE:-${SCRIPT_DIR}/deploy.env}"

# Set COMMON_SH_OPTIONAL_ENV=1 before sourcing to use the helpers (e.g.
# set_env_var) without requiring a filled-in deploy.env - the certificate
# script runs this way on a PKI workstation.
if [ ! -f "$ENV_FILE" ]; then
  if [ "${COMMON_SH_OPTIONAL_ENV:-}" != "1" ]; then
    echo "FATAL: ${ENV_FILE} not found." >&2
    echo "       cp scripts/deploy.env.example scripts/deploy.env  and fill it in." >&2
    exit 1
  fi
else
  # shellcheck source=deploy.env.example
  source "$ENV_FILE"
fi

export AWS_REGION="${AWS_REGION:-us-gov-west-1}"

# require_vars VAR1 VAR2 ... - abort listing every unset/empty variable.
require_vars() {
  local missing=()
  for v in "$@"; do
    [ -n "${!v:-}" ] || missing+=("$v")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "FATAL: set these in ${ENV_FILE}: ${missing[*]}" >&2
    exit 1
  fi
}

# stack_output STACK-NAME OUTPUT-KEY
stack_output() {
  aws cloudformation describe-stacks \
    --region "$AWS_REGION" \
    --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" \
    --output text
}

# account_id - caller's AWS account (for deriving ECR URIs)
account_id() {
  aws sts get-caller-identity --query Account --output text
}

log() { printf '\033[36m==> %s\033[0m\n' "$*"; }

# set_env_var KEY VALUE - persist an output back into deploy.env so the next
# script picks it up with no manual copy/paste. Replaces an existing
# `export KEY=...` line in place (preserving a trailing # comment) or appends
# one. awk (not sed) so ARNs/URLs with slashes need no escaping. No-op with a
# warning if deploy.env doesn't exist.
set_env_var() {
  local key="$1" val="$2"
  if [ ! -f "$ENV_FILE" ]; then
    echo "WARN: ${ENV_FILE} missing; not persisting ${key}=${val}" >&2
    return 0
  fi
  if grep -qE "^[[:space:]]*export[[:space:]]+${key}=" "$ENV_FILE"; then
    local tmp; tmp="$(mktemp)"
    awk -v key="$key" -v val="$val" '
      $0 ~ "^[[:space:]]*export[[:space:]]+" key "=" {
        c = ""; h = index($0, "#"); if (h) c = "   " substr($0, h)
        print "export " key "=\"" val "\"" c; next
      }
      { print }
    ' "$ENV_FILE" > "$tmp" && cat "$tmp" > "$ENV_FILE" && rm -f "$tmp"
  else
    printf 'export %s="%s"\n' "$key" "$val" >> "$ENV_FILE"
  fi
  log "persisted ${key} -> ${ENV_FILE##*/}"
}
