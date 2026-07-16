# Rules — shell scripts & deploy flow

- **Source `common.sh`; reuse its helpers — don't re-implement them.**
  - `require_vars VAR...` — fail early listing every unset variable.
  - `set_env_var KEY VALUE` — persist an output back into `deploy.env` so the
    next script picks it up (this is why there are no copy-paste steps).
  - `stack_output STACK KEY`, `account_id`.
  - `put_secret_and_roll ARN CLUSTER SERVICE PROMPT` — hidden prompt →
    `file://` mode-600 write → force new deployment. Both `set-*-secret.sh`
    scripts use it; a new secret script must too.
  - `ensure_ecr_repo NAME [lambda]` — create if missing (CMK when
    `KMS_KEY_ARN` set), enforce IMMUTABLE, back-fill on existing repos; pass
    `lambda` to grant `lambda.amazonaws.com` image pull.
  - `ecr_login` (prints registry host), `proxy_port URL` (userinfo-safe;
    empty for 443).

- **`deploy.env` is gitignored; only `deploy.env.example` is committed.** Add
  every new parameter to the example with a comment, and wire it through the
  matching `deploy-*.sh` `--parameter-overrides`.

- **Deploy order is fixed:** cert → `01-database` → build **all four** images
  (gateway, db-admin, grafana, mirrored ADOT collector) → `02-gateway` →
  DNS/Zscaler → `verify-gateway.sh` → `03-observability` →
  `set-grafana-oidc-secret.sh` → re-run `deploy-gateway.sh`. `01` is first so
  the CMK exists before ECR repos are created. Teardown is the reverse.

- **Rebuild and push images BEFORE any stack update that changes what the task
  definition expects** (TLS entrypoints, RDS CA bundle). Tags are immutable —
  bump the version/tag; a same-tag rebuild can't be pushed and an unchanged
  image URI leaves the deployed function on old code.

- **Bash safety:** scripts run under `set -euo pipefail`. Expand possibly-empty
  arrays as `${arr[@]+"${arr[@]}"}` (bare `"${arr[@]}"` aborts under `set -u`
  on bash < 4.4, incl. macOS system bash).
  ```bash
  local enc=()
  [ -n "${KMS_KEY_ARN:-}" ] && enc=(--encryption-configuration "...")
  aws ecr create-repository ... ${enc[@]+"${enc[@]}"}   # good — safe when empty
  aws ecr create-repository ... "${enc[@]}"              # bad  — "unbound variable" on bash 3.2
  ```

- **Never write key material or secrets to a world-readable path.** Use
  `umask 077` before creating them, and remove any pre-existing target first
  (umask governs new files only, not overwrites).
  ```bash
  ( umask 077; rm -f "$key"; openssl ... -keyout "$key" )   # good — 0600 even if $key pre-existed 0644
  openssl ... -keyout "$key"; chmod 600 "$key"              # bad  — brief world-readable window
  ```
