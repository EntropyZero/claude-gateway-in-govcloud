#!/usr/bin/env bash
# Set the real Okta OIDC client secret for the download portal (the stack
# creates a placeholder) and roll the portal service so tasks pick it up.
# Prompts for the secret so it never lands in shell history or process
# listings. If the portal reuses the gateway's Okta app, this is the same
# secret value as scripts/set-okta-secret.sh sets.
source "$(dirname "$0")/common.sh"

PORTAL_STACK_NAME="${PORTAL_STACK_NAME:-${NAME_PREFIX}-portal}"

SECRET_ARN="$(stack_output "$PORTAL_STACK_NAME" PortalOidcSecretArn)"
[ -n "$SECRET_ARN" ] && [ "$SECRET_ARN" != "None" ] || {
  echo "FATAL: could not read PortalOidcSecretArn from stack ${PORTAL_STACK_NAME} - is it deployed?" >&2
  exit 1
}

CLUSTER="$(stack_output "$GATEWAY_STACK_NAME" ClusterName)"
SERVICE="$(stack_output "$PORTAL_STACK_NAME" PortalServiceName)"

put_secret_and_roll "$SECRET_ARN" "$CLUSTER" "$SERVICE" "Okta client secret for the download portal"
