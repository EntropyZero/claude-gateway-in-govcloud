#!/usr/bin/env bash
# Dump what the gateway has persisted to Postgres - usage metering (`spend`)
# and identity/Okta groups (`principal_emails`) - to see whether it is
# capturing anything per user. Thin wrapper: sources deploy.env for
# NAME_PREFIX / AWS_REGION, then runs dump-usage.py (pg8000 + RDS CA,
# read-only, same connection path as the gateway).
#
# NOTE: the gateway does NOT store per-request token counts in Postgres - it
# stores aggregate cents-per-principal-per-period. Raw token metrics live in
# AMP; use diagnose-telemetry.sh for those. See dump-usage.py's header.
#
# Must run from a host with network reach to RDS (in-VPC or bastion) and IAM
# to read <NAME_PREFIX>/db-app-user.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "${HERE}/common.sh"
require_vars NAME_PREFIX

exec python3 "${HERE}/dump-usage.py" "$@"
