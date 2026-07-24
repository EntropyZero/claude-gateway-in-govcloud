#!/usr/bin/env bash
# Deploy cloudformation/03-observability.yaml (AMP + activity archive + Grafana).
# Prerequisites: gateway stack deployed (same NAME_PREFIX) and the Grafana
# image pushed (scripts/build-and-push-grafana.sh). The OTLP collector is NO
# LONGER deployed here - it runs as a loopback sidecar in the gateway task, so
# the ADOT image (COLLECTOR_IMAGE, from mirror-collector.sh) is consumed by the
# GATEWAY stack, not this one.
# After deploy: this script persists the AMP endpoint / workspace ARN /
# activity-log group into deploy.env; re-run deploy-gateway.sh to attach the
# telemetry sidecar and start forwarding.
source "$(dirname "$0")/common.sh"

require_vars VPC_ID PRIVATE_SUBNET_IDS GATEWAY_FQDN GRAFANA_IMAGE \
             OKTA_ISSUER GRAFANA_OKTA_CLIENT_ID GRAFANA_ADMIN_GROUP

OBS_STACK_NAME="${OBS_STACK_NAME:-${NAME_PREFIX}-obs}"

log "Deploying ${OBS_STACK_NAME} (AMP + collector + Grafana) in ${AWS_REGION}"
ARTIFACTS_BUCKET="$(ensure_artifacts_bucket)"
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$OBS_STACK_NAME" \
  --template-file "${REPO_ROOT}/cloudformation/03-observability.yaml" \
  --s3-bucket "$ARTIFACTS_BUCKET" \
  --s3-prefix "$OBS_STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
      "NamePrefix=${NAME_PREFIX}" \
      "VpcId=${VPC_ID}" \
      "PrivateSubnetIds=${PRIVATE_SUBNET_IDS}" \
      "GatewayFqdn=${GATEWAY_FQDN}" \
      "GrafanaImage=${GRAFANA_IMAGE}" \
      "OktaIssuer=${OKTA_ISSUER}" \
      "OktaAuthServerType=${OKTA_AUTH_SERVER_TYPE:-org}" \
      "GrafanaOktaClientId=${GRAFANA_OKTA_CLIENT_ID}" \
      "GrafanaAdminGroup=${GRAFANA_ADMIN_GROUP}" \
      "GrafanaEditorGroup=${GRAFANA_EDITOR_GROUP:-}" \
      "GrafanaViewerGroup=${GRAFANA_VIEWER_GROUP:-}" \
      "GrafanaDisableLoginForm=${GRAFANA_DISABLE_LOGIN_FORM:-true}" \
      "HttpsProxyUrl=${HTTPS_PROXY_URL:-}" \
      "HttpsProxyPort=$(proxy_port "${HTTPS_PROXY_URL:-}")" \
      "AlarmSnsTopicArn=${ALARM_SNS_TOPIC_ARN:-}" \
      "MissingTelemetryAlarmMinutes=${MISSING_TELEMETRY_ALARM_MINUTES:-15}" \
      "ActivityLogsAlarmMinutes=${ACTIVITY_LOGS_ALARM_MINUTES:-0}" \
      "CreateAmpEndpoint=${CREATE_AMP_ENDPOINT:-false}" \
      "AdminClientSecurityGroupId=${ADMIN_CLIENT_SG_ID:-}" \
      "CreateSupportingEndpoints=${CREATE_SUPPORTING_ENDPOINTS:-false}" \
      "EncryptAmpWithCmk=${ENCRYPT_AMP_WITH_CMK:-true}" \
      "ActivityLogWindowDays=${ACTIVITY_LOG_WINDOW_DAYS:-14}" \
      "ActivityArchiveRetentionDays=${ACTIVITY_ARCHIVE_RETENTION_DAYS:-731}"

log "Stack outputs"
aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$OBS_STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

# Persist the sidecar's destinations back into deploy.env so deploy-gateway.sh
# picks them up with no copy-paste. The gateway stack's telemetry sidecar
# remote-writes to the AMP workspace and writes the activity stream to the log
# group; its task role is scoped to the workspace ARN and log group.
AMP_ENDPOINT="$(stack_output "$OBS_STACK_NAME" WorkspacePrometheusEndpoint)"
AMP_ARN="$(stack_output "$OBS_STACK_NAME" WorkspaceArn)"
ACTIVITY_LOG_GROUP="$(stack_output "$OBS_STACK_NAME" ActivityLogGroupName)"
[ -n "$AMP_ENDPOINT" ] && [ "$AMP_ENDPOINT" != "None" ] && set_env_var OBSERVABILITY_AMP_ENDPOINT "$AMP_ENDPOINT"
[ -n "$AMP_ARN" ] && [ "$AMP_ARN" != "None" ] && set_env_var OBSERVABILITY_AMP_WORKSPACE_ARN "$AMP_ARN"
[ -n "$ACTIVITY_LOG_GROUP" ] && [ "$ACTIVITY_LOG_GROUP" != "None" ] && set_env_var OBSERVABILITY_ACTIVITY_LOG_GROUP "$ACTIVITY_LOG_GROUP"

cat <<EOF

Next steps:
  1. The telemetry sidecar's destinations (OBSERVABILITY_AMP_ENDPOINT,
     OBSERVABILITY_AMP_WORKSPACE_ARN, OBSERVABILITY_ACTIVITY_LOG_GROUP) are now
     set in deploy.env. Re-run scripts/deploy-gateway.sh so the gateway task
     picks up the co-resident ADOT collector sidecar and starts forwarding
     telemetry (ECS rolls the service; the gateway then pushes the OTLP enable
     env vars to every connected client). COLLECTOR_IMAGE must be set
     (scripts/mirror-collector.sh) - the sidecar runs that image.
  2. Okta app: register the redirect URI from the GrafanaOidcRedirectUri
     output, then set the client secret: scripts/set-grafana-oidc-secret.sh
  3. Grafana: https://${GATEWAY_FQDN}/grafana - sign in with Okta; the role
     comes from Okta group membership (${GRAFANA_ADMIN_GROUP} = Admin).
     Break-glass 'admin' login stays disabled unless
     GRAFANA_DISABLE_LOGIN_FORM=false.
  4. Workstation grouping labels: re-run Install-ClaudeCode.ps1 with
     -CostCenter/-Team (or push OTEL_RESOURCE_ATTRIBUTES via MDM).
EOF
