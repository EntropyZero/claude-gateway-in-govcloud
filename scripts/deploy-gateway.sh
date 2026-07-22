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
  echo "       Run scripts/mirror-collector.sh first, or unset the OBSERVABILITY_AMP_* vars" >&2
  echo "       in deploy.env to deploy without the telemetry sidecar." >&2
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
      "ManagedCliGroups=${MANAGED_CLI_GROUPS:-}" \
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
      "OpusModelId=${OPUS_MODEL_ID:-claude-opus-4-8}" \
      "OpusBedrockModelId=${OPUS_BEDROCK_MODEL_ID:-us-gov.anthropic.claude-opus-4-8}" \
      "SonnetModelId=${SONNET_MODEL_ID:-claude-sonnet-4-5}" \
      "SonnetBedrockModelId=${SONNET_BEDROCK_MODEL_ID:-us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0}"

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
