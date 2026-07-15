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

read -r -s -p "Okta client secret (input hidden): " OKTA_CLIENT_SECRET; echo
[ -n "$OKTA_CLIENT_SECRET" ] || { echo "FATAL: empty secret" >&2; exit 1; }

log "Updating ${SECRET_ARN}"
aws secretsmanager put-secret-value \
  --region "$AWS_REGION" \
  --secret-id "$SECRET_ARN" \
  --secret-string "$OKTA_CLIENT_SECRET" >/dev/null
unset OKTA_CLIENT_SECRET

CLUSTER="$(stack_output "$GATEWAY_STACK_NAME" ClusterName)"
SERVICE="$(stack_output "$GATEWAY_STACK_NAME" ServiceName)"

log "Forcing new deployment of ${SERVICE} on ${CLUSTER}"
aws ecs update-service \
  --region "$AWS_REGION" \
  --cluster "$CLUSTER" \
  --service "$SERVICE" \
  --force-new-deployment \
  --query 'service.deployments[0].{status:status,rolloutState:rolloutState}' \
  --output table

log "Watch rollout: aws ecs wait services-stable --region $AWS_REGION --cluster $CLUSTER --services $SERVICE"
