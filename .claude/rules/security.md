# Rules — security & secrets

- **Never hardcode org-specific values.** Gateway FQDN, Okta issuer, email
  domains, model IDs, network CIDRs, account IDs — all are CloudFormation
  parameters or `deploy.env` variables. This repo is a client template; a
  hardcoded org value is a bug. Generic placeholder defaults (e.g.
  `grafana-admins`, `claude-gateway.example.com`) are fine.

- **Secret values never go on a command line.** `--secret-string <value>` is
  visible via `ps` / `/proc/<pid>/cmdline`. Use the `put_secret_and_roll`
  helper in `common.sh`, or `--secret-string file://<mode-600 tmpfile>`.
  Read secrets via a hidden prompt; never echo, never persist to `deploy.env`.
  ```bash
  f=$(mktemp); chmod 600 "$f"; printf '%s' "$val" >"$f"
  aws secretsmanager put-secret-value --secret-id "$arn" --secret-string "file://$f"  # good
  aws secretsmanager put-secret-value --secret-id "$arn" --secret-string "$val"       # bad — leaks via ps
  ```

- **Verification fails closed.** The release mirror refuses to proceed without
  GPG verification unless `ALLOW_UNVERIFIED_MANIFEST=1` is set explicitly.
  Keep this pattern for any new integrity check — an override must be a
  deliberate, named env flag, never the default.

- **Least privilege is the default, not an add-on.**
  - The gateway connects to Postgres as `gateway_app` / `gateway_app_clone`
    (assume a NOLOGIN owner role at login), **never** the RDS master user.
    The master secret is break-glass only; no task injects it.
  - IAM and VPC-endpoint policies scope to exact resources: the two configured
    Bedrock model IDs (not `anthropic.*`), this account's ARNs, this workspace.
  - ECS execution roles read only their own secrets (+ `kms:Decrypt` on the CMK).

- **Everything at rest uses the customer-managed KMS key** (created by the DB
  stack or bring-your-own), including secrets, log groups, the activity
  archive, AMP, and ECR at creation. The one exception is the ALB access-logs
  bucket (SSE-S3) — ELB log delivery does not support KMS. Don't "fix" it.

- **Treat the activity-log stream as highly sensitive** (bash commands, tool
  inputs, file paths per user). It is opt-in, IAM-only, CMK-encrypted, and
  flagged for SIEM. Never widen its access surface.
