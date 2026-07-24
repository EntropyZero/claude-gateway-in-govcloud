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

# dollars_to_cents DOLLARS - echo whole cents for a plain dollar figure.
# EXACT string arithmetic, never floating point: awk/bc round-trips put "0.05"
# on 6 cents via (a*100)+0.5 then %.0f. Rejects anything that is not
# digits-with-at-most-one-dot, and refuses >2 decimal places rather than
# silently rounding money. Used by set-spend-limit.sh (the gateway's spend
# API takes a whole-number decimal STRING of cents).
dollars_to_cents() {
  local amount="${1:-}" dollars frac cents
  case "$amount" in
    ''|*[!0-9.]*|*.*.*) echo "dollars_to_cents: '$amount' is not a plain dollar figure" >&2; return 2 ;;
  esac
  dollars="${amount%%.*}"
  if [ "$amount" = "${amount#*.}" ]; then frac=""; else frac="${amount#*.}"; fi
  [ -n "$dollars" ] || dollars=0
  case "${#frac}" in
    0) frac="00" ;;
    1) frac="${frac}0" ;;
    2) ;;
    *) echo "dollars_to_cents: '$amount' has more than 2 decimal places" >&2; return 2 ;;
  esac
  cents="$(printf '%d%02d' "$dollars" "$((10#$frac))")"
  cents="${cents#"${cents%%[!0]*}"}"
  [ -n "$cents" ] || cents=0
  printf '%s' "$cents"
}

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

# proxy_port URL - print the port of a proxy URL (explicit port, else the
# scheme default). Task-SG egress is scoped to 443 + this port; 443 prints
# nothing (already covered by the standing HTTPS rule).
proxy_port() {
  local url="$1" port
  [ -n "$url" ] || return 0
  # Optional userinfo (user:pass@) before the host must not eat the port.
  port="$(printf '%s' "$url" | sed -nE 's#^[a-zA-Z]+://([^/@]*@)?[^:/]+:([0-9]+).*#\2#p')"
  if [ -z "$port" ]; then
    case "$url" in
      https://*) port=443 ;;
      http://*)  port=80 ;;
    esac
  fi
  [ "$port" = "443" ] || printf '%s' "$port"
}

# put_secret_and_roll SECRET-ARN CLUSTER SERVICE PROMPT-LABEL
# Prompt (hidden) for a secret value, write it to Secrets Manager via a
# mode-600 temp file (never argv - visible via ps//proc otherwise), then
# force a new deployment of the ECS service so tasks pick it up.
put_secret_and_roll() {
  local secret_arn="$1" cluster="$2" service="$3" prompt="$4" value secret_file
  read -r -s -p "${prompt} (input hidden): " value; echo
  [ -n "$value" ] || { echo "FATAL: empty secret" >&2; return 1; }

  secret_file="$(mktemp)"
  trap 'rm -f "$secret_file"' RETURN
  chmod 600 "$secret_file"
  printf '%s' "$value" > "$secret_file"
  unset value

  log "Updating ${secret_arn}"
  aws secretsmanager put-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$secret_arn" \
    --secret-string "file://${secret_file}" >/dev/null
  rm -f "$secret_file"

  log "Forcing new deployment of ${service} on ${cluster}"
  aws ecs update-service \
    --region "$AWS_REGION" \
    --cluster "$cluster" \
    --service "$service" \
    --force-new-deployment \
    --query 'service.deployments[0].{status:status,rolloutState:rolloutState}' \
    --output table

  log "Watch rollout: aws ecs wait services-stable --region $AWS_REGION --cluster $cluster --services $service"
}

# ensure_ecr_repo REPO-NAME [lambda] - create the repository if missing
# (scan on push, CMK-encrypted when KMS_KEY_ARN is set - encryption is
# fixed at creation and cannot be changed on an existing repo), and
# enforce IMMUTABLE tags either way, so repos created by older script
# versions get back-filled instead of silently staying mutable.
# Pass 'lambda' to grant lambda.amazonaws.com image pull on the repo -
# container-image Lambdas need it via the repo policy, and relying on
# Lambda's "add it automatically" behavior breaks under scoped operator
# roles that lack ecr:SetRepositoryPolicy.
ensure_ecr_repo() {
  local repo="$1" allow_lambda="${2:-}" enc=()
  [ -n "${KMS_KEY_ARN:-}" ] && enc=(--encryption-configuration "encryptionType=KMS,kmsKey=${KMS_KEY_ARN}")
  log "Ensuring ECR repository ${repo} exists (immutable tags)"
  aws ecr describe-repositories --region "$AWS_REGION" \
      --repository-names "$repo" >/dev/null 2>&1 || \
    aws ecr create-repository --region "$AWS_REGION" \
      --repository-name "$repo" \
      --image-scanning-configuration scanOnPush=true \
      --image-tag-mutability IMMUTABLE \
      ${enc[@]+"${enc[@]}"} >/dev/null
  aws ecr put-image-tag-mutability --region "$AWS_REGION" \
    --repository-name "$repo" \
    --image-tag-mutability IMMUTABLE >/dev/null
  if [ "$allow_lambda" = "lambda" ]; then
    aws ecr set-repository-policy --region "$AWS_REGION" \
      --repository-name "$repo" \
      --policy-text "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"LambdaImagePull\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"lambda.amazonaws.com\"},\"Action\":[\"ecr:BatchGetImage\",\"ecr:GetDownloadUrlForLayer\"],\"Condition\":{\"StringEquals\":{\"aws:SourceAccount\":\"$(account_id)\"}}}]}" >/dev/null
  fi
}

# ensure_artifacts_bucket - name (on stdout) of a regional bucket for
# CloudFormation template uploads. Templates over 51,200 bytes must deploy
# via S3 (aws cloudformation deploy --s3-bucket), which uploads the template
# and references it by URL. Creates the bucket (SSE-S3, public access
# blocked) if missing. Override the name with DEPLOY_ARTIFACTS_BUCKET.
ensure_artifacts_bucket() {
  local bucket="${DEPLOY_ARTIFACTS_BUCKET:-${NAME_PREFIX}-cfn-artifacts-$(account_id)-${AWS_REGION}}"
  if ! aws s3api head-bucket --bucket "$bucket" --region "$AWS_REGION" >/dev/null 2>&1; then
    log "Creating CloudFormation artifacts bucket ${bucket}" >&2
    aws s3api create-bucket --bucket "$bucket" --region "$AWS_REGION" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
    aws s3api put-public-access-block --bucket "$bucket" \
      --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null
    aws s3api put-bucket-encryption --bucket "$bucket" \
      --server-side-encryption-configuration \
      '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
  fi
  printf '%s' "$bucket"
}

# ecr_login - docker login to this account's ECR registry; prints the
# registry hostname on stdout.
ecr_login() {
  local registry
  registry="$(account_id).dkr.ecr.${AWS_REGION}.amazonaws.com"
  log "Logging in to ${registry}" >&2
  aws ecr get-login-password --region "$AWS_REGION" | \
    docker login --username AWS --password-stdin "$registry" >&2
  printf '%s' "$registry"
}

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

# retry_n ATTEMPTS DELAY_SECONDS CMD [ARGS...] - run CMD up to ATTEMPTS times,
# sleeping DELAY between failures. Returns 0 on the first success, 1 after the
# final failure. For eventual-consistency waits (e.g. ELB's access-log
# test-write racing S3 bucket-policy propagation).
retry_n() {
  local attempts="$1" delay="$2"; shift 2
  local i
  for ((i = 1; i <= attempts; i++)); do
    if "$@"; then return 0; fi
    if [ "$i" -lt "$attempts" ]; then
      log "  attempt ${i}/${attempts} failed; retrying in ${delay}s"
      sleep "$delay"
    fi
  done
  return 1
}
