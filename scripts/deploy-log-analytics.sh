#!/usr/bin/env bash
# Deploy cloudformation/05-log-analytics.yaml (optional ALB access-log search:
# Athena workgroup + Glue database/table over the gateway ALB's log bucket).
# Prerequisites: gateway stack deployed (same NAME_PREFIX) - the ALB logs
# bucket name is read from its outputs and passed as a parameter, so 02 keeps
# no locked export. Independent of 03/04; deploy or tear down any time.
# After deploy: persists ATHENA_WORKGROUP / ATHENA_DATABASE / ATHENA_TABLE /
# ATHENA_RESULTS_BUCKET into deploy.env for
# scripts/diagnostics/athena-alb-query.sh. Query how-to: om-runbooks §14.
source "$(dirname "$0")/common.sh"

require_vars NAME_PREFIX GATEWAY_STACK_NAME

LOG_ANALYTICS_STACK_NAME="${LOG_ANALYTICS_STACK_NAME:-${NAME_PREFIX}-logs}"

ALB_LOGS_BUCKET="$(stack_output "$GATEWAY_STACK_NAME" AlbLogsBucketName)"
# `aws --output text` yields the literal "None" for a missing output, not ""
case "${ALB_LOGS_BUCKET:-}" in
  ''|None)
    echo "FATAL: no AlbLogsBucketName output on ${GATEWAY_STACK_NAME} - deploy the gateway stack first" >&2
    exit 1 ;;
esac

log "Deploying ${LOG_ANALYTICS_STACK_NAME} (Athena over s3://${ALB_LOGS_BUCKET}) in ${AWS_REGION}"
ARTIFACTS_BUCKET="$(ensure_artifacts_bucket)"
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$LOG_ANALYTICS_STACK_NAME" \
  --template-file "${REPO_ROOT}/cloudformation/05-log-analytics.yaml" \
  --s3-bucket "$ARTIFACTS_BUCKET" \
  --s3-prefix "$LOG_ANALYTICS_STACK_NAME" \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
      "NamePrefix=${NAME_PREFIX}" \
      "AlbLogsBucketName=${ALB_LOGS_BUCKET}" \
      "AlbLogsProjectionStart=${ALB_LOGS_PROJECTION_START:-2025/01/01}" \
      "AthenaResultsRetentionDays=${ATHENA_RESULTS_RETENTION_DAYS:-30}" \
      "AthenaScanCutoffBytes=${ATHENA_SCAN_CUTOFF_BYTES:-10737418240}"

log "Stack outputs"
aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$LOG_ANALYTICS_STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

# Persist for athena-alb-query.sh (and any other tooling) - no copy-paste.
ATHENA_WORKGROUP="$(stack_output "$LOG_ANALYTICS_STACK_NAME" AthenaWorkGroupName)"
ATHENA_DATABASE="$(stack_output "$LOG_ANALYTICS_STACK_NAME" GlueDatabaseName)"
ATHENA_TABLE="$(stack_output "$LOG_ANALYTICS_STACK_NAME" GlueTableName)"
ATHENA_RESULTS_BUCKET="$(stack_output "$LOG_ANALYTICS_STACK_NAME" AthenaResultsBucketName)"
set_env_var ATHENA_WORKGROUP "$ATHENA_WORKGROUP"
set_env_var ATHENA_DATABASE "$ATHENA_DATABASE"
set_env_var ATHENA_TABLE "$ATHENA_TABLE"
set_env_var ATHENA_RESULTS_BUCKET "$ATHENA_RESULTS_BUCKET"

log "Done. Try: scripts/diagnostics/athena-alb-query.sh \"SELECT elb_status_code, count(*) FROM ${ATHENA_TABLE} WHERE day >= date_format(current_date - interval '1' day, '%Y/%m/%d') GROUP BY 1 ORDER BY 2 DESC\""
