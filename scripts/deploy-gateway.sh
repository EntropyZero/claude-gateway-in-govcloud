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

# Telemetry forwarding needs the observability stack's collector to exist
# first - otherwise the gateway can't resolve the forward target, crash-loops,
# and rolls back this whole deploy. Guard against a stale/premature
# OBSERVABILITY_OTLP_URL in deploy.env (e.g. a fresh account reusing config).
# Only a definitive "stack missing / never came up" clears the URL; any other
# describe-stacks failure (permissions, throttling, expired credentials) is
# fatal, so a transient API error can't silently strip telemetry forwarding
# from the whole fleet.
if [ -n "${OBSERVABILITY_OTLP_URL:-}" ]; then
  OBS_STACK_NAME="${OBS_STACK_NAME:-${NAME_PREFIX}-obs}"
  OBS_STATUS="$(aws cloudformation describe-stacks --region "$AWS_REGION" \
    --stack-name "$OBS_STACK_NAME" \
    --query 'Stacks[0].StackStatus' --output text 2>&1)" || true
  case "$OBS_STATUS" in
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE|IMPORT_COMPLETE|UPDATE_IN_PROGRESS|UPDATE_COMPLETE_CLEANUP_IN_PROGRESS)
      ;;  # collector stack is (or stays) functional - keep forwarding
    *"does not exist"*|ROLLBACK_COMPLETE|ROLLBACK_IN_PROGRESS|CREATE_FAILED|DELETE_COMPLETE|DELETE_IN_PROGRESS)
      log "WARN: OBSERVABILITY_OTLP_URL is set but stack ${OBS_STACK_NAME} is missing or never came up (${OBS_STATUS})."
      log "      Deploying WITHOUT telemetry forwarding; run deploy-observability.sh, then re-run this script."
      OBSERVABILITY_OTLP_URL=""
      ;;
    *)
      echo "FATAL: cannot confirm observability stack ${OBS_STACK_NAME} (${OBS_STATUS})." >&2
      echo "       Fix credentials/permissions, or unset OBSERVABILITY_OTLP_URL in deploy.env" >&2
      echo "       to deliberately deploy without telemetry forwarding." >&2
      exit 1
      ;;
  esac
fi

log "Deploying ${GATEWAY_STACK_NAME} (ALB + ECS Fargate) in ${AWS_REGION}"
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$GATEWAY_STACK_NAME" \
  --template-file "${REPO_ROOT}/cloudformation/02-gateway.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
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
      "PrivateRouteTableIds=${PRIVATE_ROUTE_TABLE_IDS:-}" \
      "HttpsProxyUrl=${HTTPS_PROXY_URL:-}" \
      "HttpsProxyPort=$(proxy_port "${HTTPS_PROXY_URL:-}")" \
      "CertExpiryAlarmDays=${CERT_EXPIRY_ALARM_DAYS:-30}" \
      "AlarmSnsTopicArn=${ALARM_SNS_TOPIC_ARN:-}" \
      "AlbLogRetentionDays=${ALB_LOG_RETENTION_DAYS:-90}" \
      "ObservabilityOtlpUrl=${OBSERVABILITY_OTLP_URL:-}" \
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
