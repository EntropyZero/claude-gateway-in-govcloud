#!/usr/bin/env bash
# Locate the break when client usage metrics are not reaching AMP / Grafana.
#
# The chain is:
#   client --(1)--> gateway /v1/metrics --(2)--> sidecar OTLP :4318 --(3)--> AMP
# and clients only export at all if they first fetched policy:
#   client --(0)--> gateway /managed/settings   (carries the OTLP env vars)
#
# Step 0 and 1 are visible in the ALB access logs, which is the only evidence
# that does not depend on interrogating a laptop. `/status` on the client shows
# only that enterprise settings exist, not which keys, so it cannot answer this.
#
# Usage: diagnose-telemetry.sh
# Covers today + yesterday (UTC) of ALB access logs - ALB partitions by day, so
# that is the natural granularity. Re-run after a client restart to compare.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${HERE}/common.sh"

require_vars GATEWAY_STACK_NAME

BUCKET="$(stack_output "$GATEWAY_STACK_NAME" AlbLogsBucketName)"
ACCT="$(account_id)"
REGION="${AWS_REGION:-us-gov-west-1}"
# `aws --output text` yields the literal "None" for a missing output, not ""
case "${BUCKET:-}" in
  ''|None) echo "FATAL: no AlbLogsBucketName output on $GATEWAY_STACK_NAME" >&2; exit 1 ;;
esac

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
echo "[diag] ALB access logs: s3://$BUCKET  (today+yesterday UTC, region $REGION)"

# ALB writes .../elasticloadbalancing/<region>/YYYY/MM/DD/ - pull today and
# yesterday so an early-morning run still covers the window.
for d in 0 1; do
  if date -u -d "-${d} day" +%Y/%m/%d >/dev/null 2>&1; then
    DAY="$(date -u -d "-${d} day" +%Y/%m/%d)"        # GNU
  else
    DAY="$(date -u -v-${d}d +%Y/%m/%d)"              # BSD/macOS
  fi
  aws s3 sync "s3://${BUCKET}/AWSLogs/${ACCT}/elasticloadbalancing/${REGION}/${DAY}/" \
    "$WORK" --quiet 2>/dev/null || true
done

shopt -s nullglob
FILES=("$WORK"/*.gz)
if [ ${#FILES[@]} -eq 0 ]; then
  echo "[diag] No access-log objects found. Either the ALB is idle, access logging"
  echo "       is off, or a landing-zone auto-remediation rewrote the log config"
  echo "       (known trap - see om-runbooks). Nothing further can be concluded."
  exit 0
fi
echo "[diag] ${#FILES[@]} log object(s)"

count_path() { zcat "${FILES[@]}" 2>/dev/null | grep -c -- "$1" || true; }
codes()      { zcat "${FILES[@]}" 2>/dev/null | grep -- "$1" | awk '{print "elb="$9" target="$10}' \
                 | sort | uniq -c | sort -rn | head -5; }

SETTINGS="$(count_path '/managed/settings')"
METRICS="$(count_path '/v1/metrics')"

echo
echo "  (0) client -> /managed/settings : ${SETTINGS} request(s)"
[ "$SETTINGS" -gt 0 ] && codes '/managed/settings' | sed 's/^/        /'
echo "  (1) client -> /v1/metrics       : ${METRICS} request(s)"
[ "$METRICS" -gt 0 ] && codes '/v1/metrics' | sed 's/^/        /'
echo

# ---- verdict -------------------------------------------------------------
if [ "$SETTINGS" -eq 0 ]; then
  cat <<'EOF'
[verdict] Clients are NOT fetching policy.
  They never receive CLAUDE_CODE_ENABLE_TELEMETRY / OTEL_EXPORTER_OTLP_ENDPOINT,
  so they will never export metrics. This is an ENROLLMENT problem, not a
  gateway or collector problem - no template change will fix it.
  Likely: sessions established before the managed block was deployed, still
  running on cached settings. Restart the CLI on one machine and re-run this.
  Do NOT reach for /logout as a first step - see the /logout recovery runbook
  (om-runbooks) before doing that to a working client.
  Going forward, forceRemoteSettingsRefresh in the GPO object makes the CLI
  refuse to start rather than run without freshly-fetched policy.
EOF
elif [ "$METRICS" -eq 0 ]; then
  cat <<'EOF'
[verdict] Clients fetch policy but do NOT export metrics.
  The push is reaching them; the OTLP env is not taking effect. Check the
  gateway startup log for:
      telemetry.forward_to is configured but listen.public_url is not
  (that means clients are told nothing) and confirm the startup line
      telemetry relay: N destination(s), signals enabled: metrics,logs
  actually lists `metrics`. If public_url is set and metrics are enabled,
  suspect the client-side settings merge next.
EOF
else
  cat <<'EOF'
[verdict] Clients ARE exporting metrics to the gateway.
  The break is downstream of the ALB. Since the sidecar's own otelcol_* series
  do reach AMP, the remote_write/SigV4/KMS path is healthy - so check, in order:
    1. Grafana EXPLORE (no dashboard variables): {__name__=~"claude_code.*"}
       If data appears here but the dashboard is empty, the dashboard's
       $team / $cost_center / $okta_group variables are filtering it out.
    2. Sidecar container logs for export errors or dropped batches.
EOF
fi
