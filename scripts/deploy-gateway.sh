#!/usr/bin/env bash
# Deploy cloudformation/02-gateway.yaml (ALB + ECS Fargate gateway service).
# Prerequisites: database stack deployed (same NAME_PREFIX), certificate
# imported into ACM, container image pushed (scripts/build-and-push-image.sh).
source "$(dirname "$0")/common.sh"

require_vars VPC_ID VPC_CIDR PRIVATE_SUBNET_IDS CLIENT_INGRESS_CIDR \
             CERTIFICATE_ARN GATEWAY_FQDN OKTA_ISSUER OKTA_CLIENT_ID \
             ALLOWED_EMAIL_DOMAINS

if [ -z "${IMAGE_URI:-}" ]; then
  IMAGE_URI="$(account_id).dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}:${CLAUDE_VERSION}"
fi
log "Container image: ${IMAGE_URI}"

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
      "DesiredCount=${DESIRED_COUNT:-2}" \
      "OktaIssuer=${OKTA_ISSUER}" \
      "OktaClientId=${OKTA_CLIENT_ID}" \
      "AllowedEmailDomains=${ALLOWED_EMAIL_DOMAINS}" \
      "SessionTtlHours=${SESSION_TTL_HOURS:-1}" \
      "CreateBedrockEndpoint=${CREATE_BEDROCK_ENDPOINT:-true}" \
      "CreateSupportingEndpoints=${CREATE_SUPPORTING_ENDPOINTS:-false}" \
      "PrivateRouteTableIds=${PRIVATE_ROUTE_TABLE_IDS:-}" \
      "HttpsProxyUrl=${HTTPS_PROXY_URL:-}" \
      "CertExpiryAlarmDays=${CERT_EXPIRY_ALARM_DAYS:-30}" \
      "AlarmSnsTopicArn=${ALARM_SNS_TOPIC_ARN:-}" \
      "AlbLogRetentionDays=${ALB_LOG_RETENTION_DAYS:-90}" \
      "ObservabilityOtlpUrl=${OBSERVABILITY_OTLP_URL:-}" \
      "ForwardActivityLogs=${FORWARD_ACTIVITY_LOGS:-false}" \
      "OpusModelId=${OPUS_MODEL_ID:-claude-opus-4-8}" \
      "OpusBedrockModelId=${OPUS_BEDROCK_MODEL_ID:-us-gov.anthropic.claude-opus-4-8}" \
      "SonnetModelId=${SONNET_MODEL_ID:-claude-sonnet-4-5}" \
      "SonnetBedrockModelId=${SONNET_BEDROCK_MODEL_ID:-us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0}"

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
