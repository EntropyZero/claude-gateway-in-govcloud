#!/usr/bin/env bash
# Deploy cloudformation/03-observability.yaml (AMP + OTLP collector + Grafana).
# Prerequisites: gateway stack deployed (same NAME_PREFIX), Grafana image
# pushed (scripts/build-and-push-grafana.sh), collector image reachable
# (mirror it into ECR for controlled networks).
# After deploy: set OBSERVABILITY_OTLP_URL in deploy.env to the
# OtlpForwardUrl output and re-run deploy-gateway.sh to start forwarding.
source "$(dirname "$0")/common.sh"

require_vars VPC_ID PRIVATE_SUBNET_IDS GATEWAY_FQDN GRAFANA_IMAGE

OBS_STACK_NAME="${OBS_STACK_NAME:-${NAME_PREFIX}-obs}"

log "Deploying ${OBS_STACK_NAME} (AMP + collector + Grafana) in ${AWS_REGION}"
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$OBS_STACK_NAME" \
  --template-file "${REPO_ROOT}/cloudformation/03-observability.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
      "NamePrefix=${NAME_PREFIX}" \
      "VpcId=${VPC_ID}" \
      "PrivateSubnetIds=${PRIVATE_SUBNET_IDS}" \
      "GatewayFqdn=${GATEWAY_FQDN}" \
      "GrafanaImage=${GRAFANA_IMAGE}" \
      "CollectorImage=${COLLECTOR_IMAGE:-public.ecr.aws/aws-observability/aws-otel-collector:latest}" \
      "CreateAmpEndpoint=${CREATE_AMP_ENDPOINT:-false}" \
      "ActivityLogWindowDays=${ACTIVITY_LOG_WINDOW_DAYS:-14}" \
      "ActivityArchiveRetentionDays=${ACTIVITY_ARCHIVE_RETENTION_DAYS:-731}"

log "Stack outputs"
aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$OBS_STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

cat <<EOF

Next steps:
  1. Set OBSERVABILITY_OTLP_URL in deploy.env to the OtlpForwardUrl output
     and re-run scripts/deploy-gateway.sh (ECS rolls the service; the
     gateway then pushes telemetry env vars to every connected client).
  2. Grafana: https://${GATEWAY_FQDN}/grafana  (user 'admin'; password:
     aws secretsmanager get-secret-value --secret-id ${NAME_PREFIX}/grafana-admin-password)
  3. Workstation grouping labels: re-run Install-ClaudeCode.ps1 with
     -CostCenter/-Team (or push OTEL_RESOURCE_ATTRIBUTES via MDM).
EOF
