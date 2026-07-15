#!/usr/bin/env bash
# Print the outputs of both stacks (DNS CNAME target, secret ARNs, etc.).
source "$(dirname "$0")/common.sh"

for stack in "$DB_STACK_NAME" "$GATEWAY_STACK_NAME"; do
  log "$stack"
  aws cloudformation describe-stacks --region "$AWS_REGION" \
    --stack-name "$stack" \
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
    --output table 2>/dev/null || echo "    (not deployed)"
done
