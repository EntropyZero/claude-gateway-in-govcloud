#!/usr/bin/env bash
# Set the real Okta OIDC client secret for Grafana SSO (the observability
# stack creates a placeholder) and roll the Grafana service so the task picks
# it up. Prompts for the secret so it never lands in shell history or process
# listings. If Grafana reuses the gateway's Okta app, this is the same secret
# value as scripts/set-okta-secret.sh sets.
source "$(dirname "$0")/common.sh"

OBS_STACK_NAME="${OBS_STACK_NAME:-${NAME_PREFIX}-obs}"

SECRET_ARN="$(stack_output "$OBS_STACK_NAME" GrafanaOidcSecretArn)"
[ -n "$SECRET_ARN" ] && [ "$SECRET_ARN" != "None" ] || {
  echo "FATAL: could not read GrafanaOidcSecretArn from stack ${OBS_STACK_NAME} - is it deployed?" >&2
  exit 1
}

CLUSTER="$(stack_output "$GATEWAY_STACK_NAME" ClusterName)"
SERVICE="$(stack_output "$OBS_STACK_NAME" GrafanaServiceName)"

put_secret_and_roll "$SECRET_ARN" "$CLUSTER" "$SERVICE" "Okta client secret for Grafana"
