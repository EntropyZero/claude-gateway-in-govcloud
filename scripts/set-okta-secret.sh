#!/usr/bin/env bash
# Set the real Okta OIDC client secret (the stack creates a placeholder) and
# roll the ECS service so tasks pick it up. Prompts for the secret so it never
# lands in shell history or process listings.
source "$(dirname "$0")/common.sh"

SECRET_ARN="$(stack_output "$GATEWAY_STACK_NAME" OktaClientSecretArn)"
[ -n "$SECRET_ARN" ] && [ "$SECRET_ARN" != "None" ] || {
  echo "FATAL: could not read OktaClientSecretArn from stack ${GATEWAY_STACK_NAME} - is it deployed?" >&2
  exit 1
}

CLUSTER="$(stack_output "$GATEWAY_STACK_NAME" ClusterName)"
SERVICE="$(stack_output "$GATEWAY_STACK_NAME" ServiceName)"

put_secret_and_roll "$SECRET_ARN" "$CLUSTER" "$SERVICE" "Okta client secret"
