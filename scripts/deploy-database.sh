#!/usr/bin/env bash
# Deploy cloudformation/01-database.yaml (RDS PostgreSQL backing store).
# Idempotent: `aws cloudformation deploy` creates or updates the stack.
source "$(dirname "$0")/common.sh"

require_vars VPC_ID PRIVATE_SUBNET_IDS

log "Deploying ${DB_STACK_NAME} (RDS PostgreSQL) in ${AWS_REGION}"
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$DB_STACK_NAME" \
  --template-file "${REPO_ROOT}/cloudformation/01-database.yaml" \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
      "NamePrefix=${NAME_PREFIX}" \
      "VpcId=${VPC_ID}" \
      "PrivateSubnetIds=${PRIVATE_SUBNET_IDS}" \
      "PgauditLogClasses=${PGAUDIT_LOG_CLASSES:-ddl,role,write}" \
      "KmsKeyArn=${KMS_KEY_ARN:-}"

log "Stack outputs"
aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$DB_STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

# Persist the resolved CMK so ensure_ecr_repo creates CMK-encrypted repos
# (ECR encryption is fixed at creation - deploy this stack BEFORE the first
# image push, per the README quick-start order).
if [ -z "${KMS_KEY_ARN:-}" ]; then
  RESOLVED_KEY="$(stack_output "$DB_STACK_NAME" KmsKeyArnResolved)"
  [ -n "$RESOLVED_KEY" ] && [ "$RESOLVED_KEY" != "None" ] && set_env_var KMS_KEY_ARN "$RESOLVED_KEY"
fi
