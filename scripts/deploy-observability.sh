#!/usr/bin/env bash
# Deploy cloudformation/03-observability.yaml (AMP + OTLP collector + Grafana).
# Prerequisites: gateway stack deployed (same NAME_PREFIX), Grafana image
# pushed (scripts/build-and-push-grafana.sh), and the ADOT collector image
# mirrored into your ECR (COLLECTOR_IMAGE - pin by digest; there is no
# public default on purpose).
# After deploy: set OBSERVABILITY_OTLP_URL in deploy.env to the
# OtlpForwardUrl output and re-run deploy-gateway.sh to start forwarding.
source "$(dirname "$0")/common.sh"

require_vars VPC_ID PRIVATE_SUBNET_IDS GATEWAY_FQDN GRAFANA_IMAGE COLLECTOR_IMAGE \
             OKTA_ISSUER GRAFANA_OKTA_CLIENT_ID GRAFANA_ADMIN_GROUP

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
      "CollectorImage=${COLLECTOR_IMAGE}" \
      "CollectorDesiredCount=${COLLECTOR_DESIRED_COUNT:-2}" \
      "OktaIssuer=${OKTA_ISSUER}" \
      "OktaAuthServerType=${OKTA_AUTH_SERVER_TYPE:-custom}" \
      "GrafanaOktaClientId=${GRAFANA_OKTA_CLIENT_ID}" \
      "GrafanaAdminGroup=${GRAFANA_ADMIN_GROUP}" \
      "GrafanaEditorGroup=${GRAFANA_EDITOR_GROUP:-}" \
      "GrafanaViewerGroup=${GRAFANA_VIEWER_GROUP:-}" \
      "GrafanaDisableLoginForm=${GRAFANA_DISABLE_LOGIN_FORM:-true}" \
      "HttpsProxyUrl=${HTTPS_PROXY_URL:-}" \
      "HttpsProxyPort=$(proxy_port "${HTTPS_PROXY_URL:-}")" \
      "CreateAmpEndpoint=${CREATE_AMP_ENDPOINT:-false}" \
      "EncryptAmpWithCmk=${ENCRYPT_AMP_WITH_CMK:-true}" \
      "ActivityLogWindowDays=${ACTIVITY_LOG_WINDOW_DAYS:-14}" \
      "ActivityArchiveRetentionDays=${ACTIVITY_ARCHIVE_RETENTION_DAYS:-731}"

log "Stack outputs"
aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$OBS_STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

OTLP_URL="$(stack_output "$OBS_STACK_NAME" OtlpForwardUrl)"
[ -n "$OTLP_URL" ] && [ "$OTLP_URL" != "None" ] && set_env_var OBSERVABILITY_OTLP_URL "$OTLP_URL"

cat <<EOF

Next steps:
  1. OBSERVABILITY_OTLP_URL is now set in deploy.env. Re-run
     scripts/deploy-gateway.sh so the gateway starts forwarding telemetry
     (ECS rolls the service; the gateway then pushes the OTLP env vars to
     every connected client).
  2. Okta app: register the redirect URI from the GrafanaOidcRedirectUri
     output, then set the client secret: scripts/set-grafana-oidc-secret.sh
  3. Grafana: https://${GATEWAY_FQDN}/grafana - sign in with Okta; the role
     comes from Okta group membership (${GRAFANA_ADMIN_GROUP} = Admin).
     Break-glass 'admin' login stays disabled unless
     GRAFANA_DISABLE_LOGIN_FORM=false.
  4. Workstation grouping labels: re-run Install-ClaudeCode.ps1 with
     -CostCenter/-Team (or push OTEL_RESOURCE_ATTRIBUTES via MDM).
EOF
