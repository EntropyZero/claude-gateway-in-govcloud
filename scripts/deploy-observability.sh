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

# Bedrock prompt logging is an ACCOUNT+REGION-level Bedrock setting, not a
# stack resource (no native CloudFormation type exists). The DESTINATIONS
# (CMK log group + CMK bucket + delivery role) are ALWAYS in the stack -
# inert and near-free until the account configuration points at them - so
# flipping this flag never creates/deletes them (a conditional, fixed-name,
# Retain log group would collide on re-enable, and removing the delivery
# role/bucket-grant while the account config still points at them would
# silently stop delivery). Tri-state, driving ONLY the account setting:
#   ""      (default) - never touch the account configuration
#   "true"  - after the stack deploys, point Bedrock invocation logging at
#             the stack's destinations (captures FULL prompts+responses of
#             EVERY Bedrock call in this account+region)
#   "false" - remove the account configuration (delivery stops; destinations
#             and their data remain)
BEDROCK_PROMPT_LOGGING="${BEDROCK_PROMPT_LOGGING:-}"
case "$BEDROCK_PROMPT_LOGGING" in
  ''|true|false) ;;
  *) echo "FATAL: BEDROCK_PROMPT_LOGGING must be '', 'true' or 'false' (got '$BEDROCK_PROMPT_LOGGING')" >&2; exit 1 ;;
esac

if [ "$BEDROCK_PROMPT_LOGGING" = "false" ]; then
  # get-then-delete: only touch the account config if one exists, so a
  # standing "false" in deploy.env doesn't make every unrelated 03 re-run
  # issue an account-wide delete (or fail for operators without the
  # bedrock:Delete* permission). The get itself must SUCCEED (set -e, stderr
  # visible): swallowing an AccessDenied here would report "already disabled"
  # while account-wide prompt capture keeps running.
  CURRENT_LOGGING="$(aws bedrock get-model-invocation-logging-configuration \
    --region "$AWS_REGION" --query 'loggingConfig' --output text)"
  if [ -n "$CURRENT_LOGGING" ] && [ "$CURRENT_LOGGING" != "None" ]; then
    log "Disabling Bedrock model-invocation logging (ACCOUNT+REGION-wide)"
    aws bedrock delete-model-invocation-logging-configuration --region "$AWS_REGION"
  else
    log "Bedrock model-invocation logging already disabled; nothing to do"
  fi
fi

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
      "ActivityArchiveRetentionDays=${ACTIVITY_ARCHIVE_RETENTION_DAYS:-731}" \
      "BedrockPromptLogWindowDays=${BEDROCK_PROMPT_LOG_WINDOW_DAYS:-14}" \
      "BedrockPromptArchiveRetentionDays=${BEDROCK_PROMPT_ARCHIVE_RETENTION_DAYS:-731}"

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

# ---- Bedrock prompt logging: apply the account-level configuration --------
# Runs AFTER the stack so the destinations exist. The caller needs
# bedrock:PutModelInvocationLoggingConfiguration + iam:PassRole on the
# delivery role. text+image are enabled (Claude Code sends both);
# embeddings/video are not used by this deployment. Bodies >100 KB never
# reach CloudWatch - they land only in the S3 bucket - hence both s3Config
# and largeDataDeliveryS3Config point at the stack's bucket.
if [ "$BEDROCK_PROMPT_LOGGING" = "true" ]; then
  PROMPT_LOG_GROUP="$(stack_output "$OBS_STACK_NAME" BedrockPromptLogGroupName)"
  PROMPT_BUCKET="$(stack_output "$OBS_STACK_NAME" BedrockPromptLogsBucketName)"
  PROMPT_ROLE_ARN="$(stack_output "$OBS_STACK_NAME" BedrockPromptLoggingRoleArn)"
  for v in PROMPT_LOG_GROUP PROMPT_BUCKET PROMPT_ROLE_ARN; do
    if [ -z "${!v}" ] || [ "${!v}" = "None" ]; then
      echo "FATAL: stack output for $v missing - is the deployed 03 template older than the prompt-logging change?" >&2
      exit 1
    fi
  done
  log "Enabling Bedrock model-invocation logging (ACCOUNT+REGION-wide: every"
  log "Bedrock call in ${AWS_REGION} logs FULL prompts+responses to these destinations)"
  LOGGING_CONFIG="$(mktemp)"
  trap 'rm -f "$LOGGING_CONFIG"' EXIT
  cat > "$LOGGING_CONFIG" <<JSON
{
  "cloudWatchConfig": {
    "logGroupName": "${PROMPT_LOG_GROUP}",
    "roleArn": "${PROMPT_ROLE_ARN}",
    "largeDataDeliveryS3Config": {
      "bucketName": "${PROMPT_BUCKET}"
    }
  },
  "s3Config": {
    "bucketName": "${PROMPT_BUCKET}"
  },
  "textDataDeliveryEnabled": true,
  "imageDataDeliveryEnabled": true,
  "embeddingDataDeliveryEnabled": false
}
JSON
# videoDataDeliveryEnabled is deliberately OMITTED (its default is false):
# older CLI service models reject the member client-side (boto3 #4381 class),
# and this deployment doesn't use video either way.
  aws bedrock put-model-invocation-logging-configuration \
    --region "$AWS_REGION" \
    --logging-config "file://${LOGGING_CONFIG}"
  log "Applied. Current account configuration:"
  aws bedrock get-model-invocation-logging-configuration --region "$AWS_REGION"
fi

cat <<EOF

Next steps:
  1. The telemetry sidecar's destinations (OBSERVABILITY_AMP_ENDPOINT,
     OBSERVABILITY_AMP_WORKSPACE_ARN, OBSERVABILITY_ACTIVITY_LOG_GROUP) are now
     set in deploy.env. Re-run scripts/deploy-gateway.sh so the gateway task
     picks up the co-resident ADOT collector sidecar and starts forwarding
     telemetry (ECS rolls the service; the gateway then pushes the OTLP enable
     env vars to every connected client). COLLECTOR_IMAGE must be set
     (scripts/mirror/mirror-collector.sh) - the sidecar runs that image.
  2. Okta app: register the redirect URI from the GrafanaOidcRedirectUri
     output, then set the client secret: scripts/set-grafana-oidc-secret.sh
  3. Grafana: https://${GATEWAY_FQDN}/grafana - sign in with Okta; the role
     comes from Okta group membership (${GRAFANA_ADMIN_GROUP} = Admin).
     Break-glass 'admin' login stays disabled unless
     GRAFANA_DISABLE_LOGIN_FORM=false.
  4. Workstation grouping labels: re-run Install-ClaudeCode.ps1 with
     -CostCenter/-Team (or push OTEL_RESOURCE_ATTRIBUTES via MDM).
  5. Bedrock prompt logging (BEDROCK_PROMPT_LOGGING): currently
     '${BEDROCK_PROMPT_LOGGING:-<unset - account setting untouched>}'.
     Enabling requires the 01 re-run first (CMK gains the Bedrock delivery
     grant). Verify after a live session:
       aws logs tail /claude/${NAME_PREFIX}/bedrock-prompts --region ${AWS_REGION}
EOF
