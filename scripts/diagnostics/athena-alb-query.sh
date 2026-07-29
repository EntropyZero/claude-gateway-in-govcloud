#!/usr/bin/env bash
# Run one Athena SQL query against the ALB access-log table (stack 05,
# cloudformation/05-log-analytics.yaml) and stream the result CSV to stdout.
#
# Usage: athena-alb-query.sh "SELECT ... FROM alb_access_logs WHERE day >= '2026/07/01' ..."
#
# The query runs in stack 05's workgroup (which enforces the CMK-encrypted
# results location and the per-query scan cutoff) with its Glue database as
# the default, so the table name needs no qualification. ALWAYS filter on the
# day partition key ('yyyy/MM/dd' strings) - it is what bounds the scan (and
# the bill). Example queries: docs/operations/om-runbooks.md §14.
#
# Caller needs: athena:StartQueryExecution/GetQueryExecution (and
# StopQueryExecution for the timeout path), glue read on
# the database/table, s3 read on the logs bucket, s3 read AND write on the
# results bucket (PutObject/GetBucketLocation/AbortMultipartUpload), and
# kms:GenerateDataKey + kms:Decrypt on the CMK (Athena writes the SSE-KMS
# result object with the CALLER's credentials, then this script reads it
# back). GovCloud note: query strings (and table/database names and
# partition values) are Athena METADATA and must not contain
# export-controlled data.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common.sh
. "${HERE}/../common.sh"

if [ $# -ne 1 ] || [ -z "$1" ]; then
  echo "usage: $(basename "$0") \"<athena sql>\"" >&2
  exit 2
fi
SQL="$1"
REGION="${AWS_REGION:-us-gov-west-1}"

# deploy-log-analytics.sh persists these; fall back to the stack's outputs so
# the script also works from a checkout whose deploy.env predates stack 05.
if [ -z "${ATHENA_WORKGROUP:-}" ] || [ -z "${ATHENA_DATABASE:-}" ]; then
  require_vars NAME_PREFIX
  LOG_ANALYTICS_STACK_NAME="${LOG_ANALYTICS_STACK_NAME:-${NAME_PREFIX}-logs}"
  # `|| true`: a nonexistent stack makes stack_output exit nonzero - land in
  # the curated FATAL below instead of dying on the raw CLI error.
  ATHENA_WORKGROUP="${ATHENA_WORKGROUP:-$(stack_output "$LOG_ANALYTICS_STACK_NAME" AthenaWorkGroupName 2>/dev/null || true)}"
  ATHENA_DATABASE="${ATHENA_DATABASE:-$(stack_output "$LOG_ANALYTICS_STACK_NAME" GlueDatabaseName 2>/dev/null || true)}"
fi
case "${ATHENA_WORKGROUP:-}:${ATHENA_DATABASE:-}" in
  *None*|:*|*:)
    echo "FATAL: no Athena workgroup/database - deploy stack 05 first (scripts/deploy-log-analytics.sh)" >&2
    exit 1 ;;
esac

QID="$(aws athena start-query-execution --region "$REGION" \
  --work-group "$ATHENA_WORKGROUP" \
  --query-execution-context "Database=${ATHENA_DATABASE}" \
  --query-string "$SQL" \
  --query QueryExecutionId --output text)"
echo "[athena] query ${QID} (workgroup ${ATHENA_WORKGROUP}, db ${ATHENA_DATABASE})" >&2

# Day-scoped queries return in seconds; the cap is for a fat-fingered full
# scan racing the workgroup's bytes cutoff. 2s x 300 = 10 min.
STATE=QUEUED
for _ in $(seq 1 300); do
  STATE="$(aws athena get-query-execution --region "$REGION" \
    --query-execution-id "$QID" \
    --query 'QueryExecution.Status.State' --output text)"
  case "$STATE" in SUCCEEDED|FAILED|CANCELLED) break ;; esac
  sleep 2
done

case "$STATE" in
  SUCCEEDED) ;;
  FAILED|CANCELLED)
    REASON="$(aws athena get-query-execution --region "$REGION" \
      --query-execution-id "$QID" \
      --query 'QueryExecution.Status.StateChangeReason' --output text)"
    echo "FATAL: query ${QID} ${STATE}: ${REASON}" >&2
    exit 1 ;;
  *)
    # Still QUEUED/RUNNING after the poll window: cancel so an abandoned
    # query doesn't keep scanning (and billing) unattended.
    echo "FATAL: query ${QID} still ${STATE} after 10 min - cancelling it" >&2
    aws athena stop-query-execution --region "$REGION" \
      --query-execution-id "$QID" || true
    exit 1 ;;
esac

SCANNED="$(aws athena get-query-execution --region "$REGION" \
  --query-execution-id "$QID" \
  --query 'QueryExecution.Statistics.DataScannedInBytes' --output text)"
RESULT_URI="$(aws athena get-query-execution --region "$REGION" \
  --query-execution-id "$QID" \
  --query 'QueryExecution.ResultConfiguration.OutputLocation' --output text)"
echo "[athena] SUCCEEDED, ${SCANNED} bytes scanned; result: ${RESULT_URI}" >&2

# For SELECT queries the result object is a complete CSV (header row
# included) - streaming it beats paginating get-query-results and needs no
# jq. (DDL like SHOW/DESCRIBE lands as .txt instead; still streams fine.)
exec aws s3 cp --region "$REGION" "$RESULT_URI" -
