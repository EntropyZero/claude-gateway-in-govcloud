#!/usr/bin/env bash
# Deploy cloudformation/04-download-portal.yaml (Okta-secured installer
# download portal - ECS Fargate behind the existing ALB at /portal).
# Prerequisites: gateway stack deployed (same NAME_PREFIX), portal image built
# (scripts/build-and-push-portal.sh). Independent of the observability stack.
# After deploy: publish a release (scripts/publish-portal-release.sh), register
# the Okta redirect URI, and set the client secret
# (scripts/set-portal-oidc-secret.sh).
source "$(dirname "$0")/common.sh"

require_vars VPC_ID PRIVATE_SUBNET_IDS GATEWAY_FQDN PORTAL_IMAGE \
             OKTA_ISSUER PORTAL_OKTA_CLIENT_ID ACCESS_GROUP \
             PORTAL_TEAMS PORTAL_COST_CENTERS CLAUDE_VERSION

PORTAL_STACK_NAME="${PORTAL_STACK_NAME:-${NAME_PREFIX}-portal}"

log "Deploying ${PORTAL_STACK_NAME} (download portal) in ${AWS_REGION}"
ARTIFACTS_BUCKET="$(ensure_artifacts_bucket)"
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$PORTAL_STACK_NAME" \
  --template-file "${REPO_ROOT}/cloudformation/04-download-portal.yaml" \
  --s3-bucket "$ARTIFACTS_BUCKET" \
  --s3-prefix "$PORTAL_STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
      "NamePrefix=${NAME_PREFIX}" \
      "VpcId=${VPC_ID}" \
      "PrivateSubnetIds=${PRIVATE_SUBNET_IDS}" \
      "GatewayFqdn=${GATEWAY_FQDN}" \
      "PortalImage=${PORTAL_IMAGE}" \
      "PortalDesiredCount=${PORTAL_DESIRED_COUNT:-2}" \
      "OktaIssuer=${OKTA_ISSUER}" \
      "PortalOktaClientId=${PORTAL_OKTA_CLIENT_ID}" \
      "AccessGroup=${ACCESS_GROUP}" \
      "PortalTeams=${PORTAL_TEAMS}" \
      "PortalCostCenters=${PORTAL_COST_CENTERS}" \
      "ReleaseVersion=${PORTAL_RELEASE_VERSION:-${CLAUDE_VERSION}}" \
      "BundleExtraCa=${PORTAL_BUNDLE_EXTRA_CA:-false}" \
      "DisableUpdates=${PORTAL_DISABLE_UPDATES:-true}" \
      "SessionTtlHours=${PORTAL_SESSION_TTL_HOURS:-8}" \
      "PortalAuditRetentionDays=${PORTAL_AUDIT_RETENTION_DAYS:-365}" \
      "PortalLogRetentionDays=${PORTAL_LOG_RETENTION_DAYS:-90}" \
      "ListenerRulePriority=${PORTAL_LISTENER_RULE_PRIORITY:-20}" \
      "HttpsProxyUrl=${HTTPS_PROXY_URL:-}" \
      "HttpsProxyPort=$(proxy_port "${HTTPS_PROXY_URL:-}")" \
      "CreateSupportingEndpoints=${CREATE_SUPPORTING_ENDPOINTS:-false}"

log "Stack outputs"
aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$PORTAL_STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

ARTIFACTS_OUT="$(stack_output "$PORTAL_STACK_NAME" ArtifactsBucketName)"
[ -n "$ARTIFACTS_OUT" ] && [ "$ARTIFACTS_OUT" != "None" ] && \
  set_env_var PORTAL_ARTIFACTS_BUCKET "$ARTIFACTS_OUT"

cat <<EOF

Next steps:
  1. Publish the release to the artifacts bucket (uploads the verified mirror
     output + the installer):
       scripts/publish-portal-release.sh ${PORTAL_RELEASE_VERSION:-${CLAUDE_VERSION}}
  2. Okta app: register the redirect URI from the PortalOidcRedirectUri output,
     add the portal to the AccessGroup (${ACCESS_GROUP}), then set the client
     secret:
       scripts/set-portal-oidc-secret.sh
  3. Portal: https://${GATEWAY_FQDN}/portal - sign in with Okta.
EOF
