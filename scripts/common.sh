#!/usr/bin/env bash
# Shared helpers for the deploy scripts. Source, don't execute:
#   source "$(dirname "$0")/common.sh"
# Loads scripts/deploy.env and provides require_vars / stack_output.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${DEPLOY_ENV_FILE:-${SCRIPT_DIR}/deploy.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "FATAL: ${ENV_FILE} not found." >&2
  echo "       cp scripts/deploy.env.example scripts/deploy.env  and fill it in." >&2
  exit 1
fi
# shellcheck source=deploy.env.example
source "$ENV_FILE"

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
