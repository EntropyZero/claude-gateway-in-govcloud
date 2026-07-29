#!/usr/bin/env bash
# Deploy cloudformation/02-gateway.yaml (ALB + ECS Fargate gateway service).
# Prerequisites: database stack deployed (same NAME_PREFIX), certificate
# imported into ACM, container image pushed (scripts/build-and-push-image.sh).
source "$(dirname "$0")/common.sh"

require_vars VPC_ID VPC_CIDR PRIVATE_SUBNET_IDS CLIENT_INGRESS_CIDR \
             CERTIFICATE_ARN GATEWAY_FQDN OKTA_ISSUER OKTA_CLIENT_ID \
             ALLOWED_EMAIL_DOMAINS DBADMIN_IMAGE

if [ -z "${IMAGE_URI:-}" ]; then
  IMAGE_URI="$(account_id).dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}:${CLAUDE_VERSION}"
fi
log "Container image: ${IMAGE_URI}"

# Telemetry now runs as a loopback ADOT collector SIDECAR inside the gateway
# task (no separate collector service, no cross-stack forward target to
# resolve). It turns on when OBSERVABILITY_AMP_ENDPOINT is set - populated by
# deploy-observability.sh from stack 03's outputs. The sidecar is
# non-essential and forwards over localhost, so a missing/incomplete
# observability stack degrades telemetry softly (metrics/logs just don't land)
# rather than crash-looping the gateway, so no pre-flight stack check is
# needed. COLLECTOR_IMAGE (from mirror-collector.sh) is the sidecar's image.
if [ -n "${OBSERVABILITY_AMP_ENDPOINT:-}" ] && [ -z "${COLLECTOR_IMAGE:-}" ]; then
  echo "FATAL: OBSERVABILITY_AMP_ENDPOINT is set (telemetry on) but COLLECTOR_IMAGE is empty." >&2
  echo "       Run scripts/mirror/mirror-collector.sh first, or unset the OBSERVABILITY_AMP_* vars" >&2
  echo "       in deploy.env to deploy without the telemetry sidecar." >&2
  exit 1
fi

# Model defaults - the SINGLE definition; the guard below and the
# --parameter-overrides both consume these, so they cannot drift apart.
# Keep in lockstep with the parameter defaults in 02-gateway.yaml.
OPUS_MODEL_ID="${OPUS_MODEL_ID:-claude-opus-4-8}"
OPUS_BEDROCK_MODEL_ID="${OPUS_BEDROCK_MODEL_ID:-us-gov.anthropic.claude-opus-4-8}"
SONNET_MODEL_ID="${SONNET_MODEL_ID:-claude-sonnet-5}"
SONNET_BEDROCK_MODEL_ID="${SONNET_BEDROCK_MODEL_ID:-us-gov.anthropic.claude-sonnet-5}"
HAIKU_MODEL_ID="${HAIKU_MODEL_ID:-claude-sonnet-4-5}"
HAIKU_BEDROCK_MODEL_ID="${HAIKU_BEDROCK_MODEL_ID:-us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0}"

# Both ID triples must be pairwise distinct. The gateway-facing IDs key the
# gateway's `models:` list and the pushed `availableModels` allowlist; the
# Bedrock IDs decide what each menu entry actually invokes - two gateway IDs
# mapped to one Bedrock profile deploy fine and serve fine, but one picker
# entry is silently MISLABELED (e.g. "claude-sonnet-5" invoking Sonnet 4.5).
# The likely trigger is a deploy.env written before the Sonnet 5 rollout:
# update BOTH SONNET_MODEL_ID and SONNET_BEDROCK_MODEL_ID per
# deploy.env.example rather than overriding the HAIKU_* vars away.
_dup_check() {  # <label> <a-name> <a-val> <b-name> <b-val> <c-name> <c-val>
  local label="$1" an="$2" a="$3" bn="$4" b="$5" cn="$6" c="$7"
  if [ "$a" = "$b" ] || [ "$a" = "$c" ] || [ "$b" = "$c" ]; then
    echo "FATAL: duplicate ${label}:" >&2
    echo "       ${an}='${a}'" >&2
    echo "       ${bn}='${b}'" >&2
    echo "       ${cn}='${c}'" >&2
    echo "       Each must be distinct - see the Models section of deploy.env.example." >&2
    echo "       A pre-Sonnet-5 deploy.env usually still sets SONNET_MODEL_ID=claude-sonnet-4-5" >&2
    echo "       and SONNET_BEDROCK_MODEL_ID=us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0 -" >&2
    echo "       update BOTH to the Sonnet 5 values." >&2
    exit 1
  fi
}
_dup_check "gateway-facing model IDs" \
  OPUS_MODEL_ID "$OPUS_MODEL_ID" \
  SONNET_MODEL_ID "$SONNET_MODEL_ID" \
  HAIKU_MODEL_ID "$HAIKU_MODEL_ID"
_dup_check "Bedrock model / inference-profile IDs" \
  OPUS_BEDROCK_MODEL_ID "$OPUS_BEDROCK_MODEL_ID" \
  SONNET_BEDROCK_MODEL_ID "$SONNET_BEDROCK_MODEL_ID" \
  HAIKU_BEDROCK_MODEL_ID "$HAIKU_BEDROCK_MODEL_ID"

# Minimum client version, pushed to every client via the managed settings
# (requiredMinimumVersion): an older client exits at startup telling the
# user to update. Defaults to CLAUDE_VERSION - the version the gateway
# image itself runs - so the client floor follows every gateway upgrade
# automatically. MIN_CLIENT_VERSION=none disables the check; anything else
# must be X.Y.Z (the template's AllowedPattern would reject it anyway, but
# failing here names the deploy.env variable instead of a CFN parameter).
MIN_CLIENT_VERSION="${MIN_CLIENT_VERSION:-${CLAUDE_VERSION:-}}"
[ "$MIN_CLIENT_VERSION" = "none" ] && MIN_CLIENT_VERSION=""
if [ -n "$MIN_CLIENT_VERSION" ] && ! [[ "$MIN_CLIENT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "FATAL: MIN_CLIENT_VERSION='${MIN_CLIENT_VERSION}' is not a semantic version (X.Y.Z) or 'none'." >&2
  echo "       (When MIN_CLIENT_VERSION is unset it defaults from CLAUDE_VERSION - check that too.)" >&2
  exit 1
fi

# Prompt/response-content capture (OTEL_LOG_USER_PROMPTS=1 /
# OTEL_LOG_ASSISTANT_RESPONSES=1 pushed to every client) rides the
# activity-log pipeline - without FORWARD_ACTIVITY_LOGS=true the clients
# would attach the content to telemetry events the gateway never forwards:
# the operator believes capture is on and nothing is landing. Refuse the
# combination instead of deploying it.
if { [ "${LOG_USER_PROMPTS:-false}" = "true" ] || [ "${LOG_ASSISTANT_RESPONSES:-false}" = "true" ]; } \
    && [ "${FORWARD_ACTIVITY_LOGS:-false}" != "true" ]; then
  echo "FATAL: LOG_USER_PROMPTS=true / LOG_ASSISTANT_RESPONSES=true require FORWARD_ACTIVITY_LOGS=true." >&2
  echo "       The captured content travels inside the activity-log stream; with" >&2
  echo "       forwarding off it is silently dropped at the gateway." >&2
  exit 1
fi

ARTIFACTS_BUCKET="$(ensure_artifacts_bucket)"

# On failure, KEEP successfully-created resources (the stack lands in
# CREATE_FAILED / UPDATE_FAILED) instead of rolling everything back: fix the
# problem and re-run this script - the deploy CONTINUES from where it failed.
# This kills the fix -> full-rollback -> full-recreate cycle (the in-VPC
# db-admin Lambda alone adds ~30 min of ENI teardown to every rollback).
# Set CFN_DISABLE_ROLLBACK=false for classic auto-rollback (hands-off
# production updates).
ROLLBACK_ARGS=(--disable-rollback)
[ "${CFN_DISABLE_ROLLBACK:-true}" = "false" ] && ROLLBACK_ARGS=()

log "Deploying ${GATEWAY_STACK_NAME} (ALB + ECS Fargate) in ${AWS_REGION}"
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$GATEWAY_STACK_NAME" \
  --template-file "${REPO_ROOT}/cloudformation/02-gateway.yaml" \
  --s3-bucket "$ARTIFACTS_BUCKET" \
  --s3-prefix "$GATEWAY_STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  ${ROLLBACK_ARGS[@]+"${ROLLBACK_ARGS[@]}"} \
  --parameter-overrides \
      "NamePrefix=${NAME_PREFIX}" \
      "VpcId=${VPC_ID}" \
      "VpcCidr=${VPC_CIDR}" \
      "PrivateSubnetIds=${PRIVATE_SUBNET_IDS}" \
      "ClientIngressCidr=${CLIENT_INGRESS_CIDR}" \
      "CertificateArn=${CERTIFICATE_ARN}" \
      "GatewayFqdn=${GATEWAY_FQDN}" \
      "ContainerImage=${IMAGE_URI}" \
      "DbAdminLambdaImage=${DBADMIN_IMAGE}" \
      "AppSecretRotationDays=${APP_SECRET_ROTATION_DAYS:-90}" \
      "DesiredCount=${DESIRED_COUNT:-2}" \
      "TaskCpu=${TASK_CPU:-1024}" \
      "TaskMemory=${TASK_MEMORY:-2048}" \
      "OktaIssuer=${OKTA_ISSUER}" \
      "OktaClientId=${OKTA_CLIENT_ID}" \
      "AllowedEmailDomains=${ALLOWED_EMAIL_DOMAINS}" \
      "SessionTtlHours=${SESSION_TTL_HOURS:-1}" \
      "AlbIdleTimeoutSeconds=${ALB_IDLE_TIMEOUT_SECONDS:-900}" \
      "DeregistrationDelaySeconds=${DEREGISTRATION_DELAY_SECONDS:-300}" \
      "CreateBedrockEndpoint=${CREATE_BEDROCK_ENDPOINT:-true}" \
      "BedrockPrivateDns=${BEDROCK_PRIVATE_DNS:-true}" \
      "CreateSupportingEndpoints=${CREATE_SUPPORTING_ENDPOINTS:-false}" \
      "AdminClientSecurityGroupId=${ADMIN_CLIENT_SG_ID:-}" \
      "CreateEcrApiEndpoint=${CREATE_ECR_API_ENDPOINT:-true}" \
      "CreateEcrDkrEndpoint=${CREATE_ECR_DKR_ENDPOINT:-true}" \
      "CreateLogsEndpoint=${CREATE_LOGS_ENDPOINT:-true}" \
      "CreateSecretsManagerEndpoint=${CREATE_SECRETSMANAGER_ENDPOINT:-true}" \
      "CreateEcsEndpoint=${CREATE_ECS_ENDPOINT:-true}" \
      "PrivateRouteTableIds=${PRIVATE_ROUTE_TABLE_IDS:-}" \
      "HttpsProxyUrl=${HTTPS_PROXY_URL:-}" \
      "HttpsProxyPort=$(proxy_port "${HTTPS_PROXY_URL:-}")" \
      "CertExpiryAlarmDays=${CERT_EXPIRY_ALARM_DAYS:-30}" \
      "AlarmSnsTopicArn=${ALARM_SNS_TOPIC_ARN:-}" \
      "AlbLogRetentionDays=${ALB_LOG_RETENTION_DAYS:-90}" \
      "ObservabilityAmpRemoteWriteEndpoint=${OBSERVABILITY_AMP_ENDPOINT:-}" \
      "ObservabilityAmpWorkspaceArn=${OBSERVABILITY_AMP_WORKSPACE_ARN:-}" \
      "ObservabilityActivityLogGroup=${OBSERVABILITY_ACTIVITY_LOG_GROUP:-}" \
      "CollectorImage=${COLLECTOR_IMAGE:-}" \
      "TelemetryFailClosed=${TELEMETRY_FAIL_CLOSED:-true}" \
      "ForwardActivityLogs=${FORWARD_ACTIVITY_LOGS:-false}" \
      "LogUserPrompts=${LOG_USER_PROMPTS:-false}" \
      "LogAssistantResponses=${LOG_ASSISTANT_RESPONSES:-false}" \
      "OpusModelId=${OPUS_MODEL_ID}" \
      "OpusBedrockModelId=${OPUS_BEDROCK_MODEL_ID}" \
      "SonnetModelId=${SONNET_MODEL_ID}" \
      "SonnetBedrockModelId=${SONNET_BEDROCK_MODEL_ID}" \
      "HaikuModelId=${HAIKU_MODEL_ID}" \
      "HaikuBedrockModelId=${HAIKU_BEDROCK_MODEL_ID}" \
      "MinClientVersion=${MIN_CLIENT_VERSION}" \
      "SpendGroupLimitMode=${SPEND_GROUP_LIMIT_MODE:-min}" \
      "SpendBlockedMessage=${SPEND_BLOCKED_MESSAGE:-Contact the Claude Code platform team to request an increase.}" \
      "SpendAdminGroups=${SPEND_ADMIN_GROUPS:-}"

# Stack policy: refuse any future update that would REPLACE or DELETE the
# ALB. Its default DNS name is the corporate CNAME target - recreation
# means re-submitting DNS to the client and re-publishing the fingerprint.
# Layered with deletion_protection.enabled and the fixed ALB name (a
# create-before-delete replacement collides with itself). Remove the
# policy deliberately if an ALB replacement is ever truly intended:
#   aws cloudformation set-stack-policy --stack-name <stack> \
#     --stack-policy-body '{"Statement":[{"Effect":"Allow","Action":"Update:*","Principal":"*","Resource":"*"}]}'
log "Locking the ALB against replacement/deletion (stack policy)"
aws cloudformation set-stack-policy \
  --region "$AWS_REGION" \
  --stack-name "$GATEWAY_STACK_NAME" \
  --stack-policy-body '{
    "Statement": [
      {"Effect": "Allow", "Action": "Update:*", "Principal": "*", "Resource": "*"},
      {"Effect": "Deny", "Action": ["Update:Replace", "Update:Delete"],
       "Principal": "*", "Resource": "LogicalResourceId/LoadBalancer"}
    ]
  }'

# ALB deletion protection + access logs live in the TEMPLATE
# (LoadBalancerAttributes) - declarative and drift-checked. The transient
# post-deploy variant that used to live here existed to dodge a landing-zone
# auto-remediation that was rewriting the ALB's access-log config (since
# exempted). If create-time log validation ever fails AccessDenied on a
# fresh account, check for such automation FIRST - the bucket policy grants
# both ELB delivery principals and is not the likely culprit.

log "Stack outputs"
aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$GATEWAY_STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

cat <<EOF

Next steps (see README.md):
  1. scripts/set-okta-secret.sh                     # real OIDC client secret
  2. Corporate DNS: ${GATEWAY_FQDN} CNAME <AlbDnsName output above>
  3. Zscaler bypass for ${GATEWAY_FQDN} (ZIA exemption or ZPA app segment)
  4. scripts/verify-gateway.sh                      # end-to-end checks
EOF
