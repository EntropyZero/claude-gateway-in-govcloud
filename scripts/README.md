# scripts/ — deploy, operate, mirror, diagnose

Everything here is driven by `deploy.env` (copy `deploy.env.example`, fill it
in; scripts persist their outputs back into it via `set_env_var`, so there are
no copy-paste steps between them). The deploy/operate scripts source
`common.sh` for the shared helpers (`require_vars`, `stack_output`,
`put_secret_and_roll`, `ensure_ecr_repo`, …); the standalone egress-host
mirror tools (`mirror/mirror-claude-release.sh`, `mirror/mirror-python-deps.sh`,
`mirror/mirror-grafana-plugin.sh`, `mirror/mirror-rds-ca-bundle.sh`)
deliberately don't, so they run without a filled-in `deploy.env`. See `.claude/rules/scripts.md` for the house rules.

Layout: the **deploy/operate chain lives flat at this level** (these names
appear throughout the runbooks — they are the repo's operator API);
special-purpose tooling lives in `mirror/` and `diagnostics/`.

**Two-host model** (`.claude/rules/offline-build.md`): the build/deploy
machine has **no internet** — only AWS service endpoints. Everything external
is fetched, verified, and staged by the `mirror/` tools on a **separate
egress host**; the `mirror/` directory is then copied to the build machine,
and the `build-and-push-*.sh` scripts consume it (failing closed with
instructions when an artifact is missing — they never fetch and never invoke
a mirror script).

## Core deploy chain (in run order)

The order is load-bearing — see
[`docs/operations/greenfield-deployment.md`](../docs/operations/greenfield-deployment.md)
for the full runbook.

| Script | What it does |
|---|---|
| `import-enterprise-cert.sh` | Generate a CSR / import the signed enterprise cert into ACM for the ALB (runs without `deploy.env` for PKI workstations) |
| `deploy-database.sh` | Stack 01: RDS PostgreSQL + the KMS CMK (created first so ECR repos are born encrypted) |
| `mirror/mirror-claude-release.sh` + `mirror/mirror-rds-ca-bundle.sh` | Egress host: verify + stage the pinned Claude Code release and the RDS CA trust bundle into `mirror/`, then copy `mirror/` to the build machine |
| `mirror/mirror-base-images.sh` | Mirror the four container **base** images into ECR, digest-pinned into `deploy.env` (`*_BASE_IMAGE`) — run before the builds below; dual-reach like `mirror/mirror-collector.sh` |
| `build-and-push-image.sh` | Gateway image (stages `claude` from `mirror/<version>/`, re-verified against `CHECKSUMS.txt`, + `mirror/rds-ca-bundle.pem`) |
| `build-and-push-dbadmin.sh` | DB bootstrap/rotation Lambda image (vendored wheels + `mirror/rds-ca-bundle.pem` — no egress) |
| `build-and-push-grafana.sh` | Provisioned Grafana image (dashboard + AMP SigV4 plugin baked in from `mirror/grafana-plugins/`, re-verified against `mirror/grafana-plugin.pin`) |
| `mirror/mirror-collector.sh` | Mirror the pinned ADOT collector image into ECR, digest-pinned |
| `build-and-push-portal.sh` | Optional download-portal image |
| `deploy-gateway.sh` | Stack 02: ALB + ECS gateway (+ telemetry sidecar on the 02 re-run) |
| `verify-gateway.sh` | Post-deploy DNS/TLS/OAuth verification from the client-side network |
| `deploy-observability.sh` | Stack 03: AMP + Grafana + activity archive (emits the AMP params 02 consumes) |
| `deploy-download-portal.sh` | Stack 04 (optional): installer download portal |
| `publish-portal-release.sh` | Upload a verified release + installer to the portal artifacts bucket |

## Secret setters

All use `put_secret_and_roll` (hidden prompt → mode-600 `file://` write →
force new ECS deployment); secrets never touch argv or `deploy.env`.

| Script | Secret |
|---|---|
| `set-okta-secret.sh` | Gateway Okta OIDC client secret |
| `set-grafana-oidc-secret.sh` | Grafana Okta SSO client secret |
| `set-portal-oidc-secret.sh` | Download-portal Okta client secret |

## O&M utilities

| Script | What it does |
|---|---|
| `set-spend-limit.sh` | Create/update/clear/list per-user / per-group / org spend caps — see [`docs/operations/cost-controls.md`](../docs/operations/cost-controls.md) |
| `stack-outputs.sh` | Print the DB + gateway stack outputs |

## `mirror/` — all vendor mirroring

One place for every third-party artifact we pull in. Run these on an
egress-capable host; everything downstream is offline.

| Script | Mirrors |
|---|---|
| `mirror/mirror-claude-release.sh` | Claude Code native binaries (GPG-verified manifest, SHA-256 per platform) → `mirror/<version>/` (gitignored staging) |
| `mirror/mirror-base-images.sh` | The four container **base** images (gateway, db-admin Lambda, Grafana, portal) → your ECR under `<prefix>-base-*` repos, digest-pinned into `deploy.env` (`GATEWAY_BASE_IMAGE`, `LAMBDA_BASE_IMAGE`, `GRAFANA_BASE_IMAGE`, `PORTAL_BASE_IMAGE`, consumed by the `build-and-push-*.sh` scripts). Needs **both** upstream-registry reach (Docker Hub + `public.ecr.aws`) and AWS creds — run it where both are available |
| `mirror/mirror-collector.sh` | ADOT collector image → your ECR, digest-pinned into `deploy.env` (`COLLECTOR_IMAGE`). Same dual-reach profile as `mirror-base-images.sh`: needs **both** upstream-registry reach (`public.ecr.aws`) and AWS creds — run it where both are available |
| `mirror/mirror-grafana-plugin.sh` | Grafana AMP datasource plugin (SigV4 auth; not bundled upstream) → `mirror/grafana-plugins/` (gitignored staging); version+sha256 pinned in `mirror/grafana-plugin.pin`, which `build-and-push-grafana.sh` re-verifies against when it bakes the transferred artifact into the image |
| `mirror/mirror-rds-ca-bundle.sh` | RDS CA trust bundle (GovCloud truststore; `RDS_CA_BUNDLE_URL` overrides for commercial regions) → `mirror/rds-ca-bundle.pem`, baked into the gateway + db-admin images by their build scripts |
| `mirror/mirror-python-deps.sh` | Python wheels: regenerates the **committed** `docker/portal/vendor/` and `docker/db-admin/vendor/` sets from each image's `requirements.txt`, and (`--tools`) stages operator-tooling wheels into `vendor/tools/` (gitignored) |

Dependency-update flow: edit the pin in `docker/<image>/requirements.txt` →
`mirror/mirror-python-deps.sh <image>` → commit the wheel changes → bump the
image version → `build-and-push-*.sh` → `deploy-gateway.sh` /
`deploy-download-portal.sh`.

## `diagnostics/`

| Script | What it does |
|---|---|
| `diagnostics/diagnose-telemetry.sh` | Locate the break in the client → gateway → sidecar → AMP metrics chain (ALB logs, AMP queries, live collector config) |
| `diagnostics/amp-query.py` | SigV4-signed PromQL query direct against AMP (needs `botocore`) |
| `diagnostics/check-collector-config.py` | Parse a collector config from stdin, report per-pipeline receivers (needs `pyyaml`) |
| `diagnostics/dump-usage.sh` / `dump-usage.py` | Read-only dump of the gateway's Postgres usage/identity/caps tables (needs `pg8000`, in-VPC reach to RDS) |

Python deps for these (and for `docs/md-to-pdf.py`) are listed in
`requirements-tools.txt` — `pip install -r scripts/requirements-tools.txt`
online, or mirror them offline with `mirror/mirror-python-deps.sh --tools`.

## Files

- `common.sh` — shared helpers; sourced by the deploy/operate scripts.
  `deploy.env` must stay a sibling of `common.sh` (helpers resolve it
  relative to themselves).
- `deploy.env.example` — the committed template; `deploy.env` itself is
  gitignored. Every new parameter goes into the example with a comment.
- `requirements-tools.txt` — operator-tooling Python deps (see above).
