# CLAUDE.md — working guide for this repo

Orientation for future sessions on `claude-gateway-in-govcloud`. Read this,
then the doc it points you at for whatever you're doing.

## What this is

A **client-configurable, code-driven** deployment of Anthropic's self-hosted
Claude apps gateway for Claude Code, targeting **AWS GovCloud
`us-gov-west-1`**: internal ALB + ECS Fargate gateway, RDS PostgreSQL store,
Bedrock inference (Opus 4.8 / Sonnet 4.5 via `us-gov` inference profiles),
Okta OIDC, an offline Windows client rollout, and an optional usage/cost
observability stack (AMP + ADOT collector + Grafana). End users are on
Zscaler-secured Windows laptops (ZPA) in an AWS Landing Zone (Transit
Gateway, central egress).

It is a **template, not a single deployment** — every org-specific value is a
CloudFormation parameter or a `scripts/deploy.env` variable.

## Status (keep this current)

**As of 2026-07-17: first end-to-end test run IN PROGRESS.** Stacks 01/02
deploy; proven live so far: DB bootstrap + app-user auth, RDS TLS
(verify-full via the OS trust store — the driver ignores `sslrootcert=`),
offline image builds on a hardened host (legacy Docker builder, umask 077),
ALB + access logs (after evicting a landing-zone auto-remediation), and the
endpoint-SG reachability model (workloads + in-VPC admin host).
**Currently blocked on one org prerequisite: a Zscaler ALLOW +
SSL-inspection exemption for the Okta issuer FQDN on the VPC's server-side
egress** — gateway boot fails at OIDC discovery (403) until it lands. The
running fix log is at the top of `docs/security-review-2026-07.md`. Still
unexercised: gateway steady state + end-to-end login, Grafana Okta login,
secret rotation, activity archive. Not production-ready until the runbook's
validation checklist is green.

The full security review (`docs/security-review-2026-07.md`) is implemented:
batches A (deploy-breakers), B (ZPA/landing-zone prerequisites), C (FedRAMP
hardening C1–C11), D (correctness), and C12 (least-privilege app DB user +
self-rolling rotation). **Deferred by decision:** C9 (S3 Object Lock).
**Partial by design:** C2 (the gateway→collector OTLP hop stays
plaintext-but-SG-scoped; the TLS recipe is documented on the collector task).

## Repo map

| Path | What |
|---|---|
| `cloudformation/01-database.yaml` | RDS PG16, the **KMS CMK** (created here, exported), db SGs, pgaudit |
| `cloudformation/02-gateway.yaml` | ALB+TLS, ECS gateway, IAM, secrets, VPC endpoints, **db bootstrap + rotation Lambdas** |
| `cloudformation/03-observability.yaml` | AMP, ADOT collector, Grafana (Okta SSO), activity-archive chain |
| `docker/` | gateway image + entrypoint; `db-admin/` (bootstrap+rotation Lambda); `grafana/` |
| `client/` | offline release mirror + `Install-ClaudeCode.ps1` (non-admin Windows) |
| `scripts/` | `deploy.env`-driven runbook; `common.sh` holds the shared helpers |
| `docs/architecture.md` | review package: 8 SVG diagrams + secrets/SG/encryption inventories |
| `docs/diagrams/generate.py` | **source of the diagrams** — edit the script, re-run, commit both |
| `docs/security-review-2026-07.md` | finding-by-finding status; the source of truth for what's done |
| `docs/test-run-runbook.md` | the deploy runbook |
| `docs/networking-request-email.md` | cert/DNS/Zscaler request template |
| `docs/okta-request-email.md` | Okta OIDC app request template (org server, Web app, groups) |
| `tests/` + `Makefile` | test suites (`make test`); CI in `.github/workflows/tests.yml` |

## Deploy model (details in the runbook)

Order is load-bearing: **cert → 01 database → build all four images → 02
gateway → DNS/Zscaler → verify → 03 observability → Grafana secret → 02
re-run**. 01 is first because it creates and persists the CMK so the ECR
repos are born encrypted. Scripts persist their outputs back into
`deploy.env` (`set_env_var`) so there are no copy-paste steps. Teardown is the
reverse (03 → 02 → 01).

## How to work here

- **Before committing nontrivial CFN / script / Lambda changes, run a
  multi-agent self-review** (finder angles + an adversarial pass that
  web-checks AWS/Postgres/Grafana semantics). This has repeatedly caught
  deploy-breaking bugs that syntax checks and docs missed. It is the single
  highest-value habit in this repo.
- **Run `make test` and keep it green before moving on / committing** (it's a
  rule — see `.claude/rules/process.md`). Four fast suites:
  - `tests/lambda` — pytest for the db-admin rotation/bootstrap Lambda (moto
    Secrets Manager + faked pg/ECS): alternating-user flip, idempotency
    guards, error propagation. **The code with real bug history — extend it
    when you touch `docker/db-admin/app.py`.**
  - `tests/bash` — bats for `common.sh` helpers (`proxy_port`, `set_env_var`,
    `require_vars`).
  - `tests/cfn` — `cfn-lint` + a **cfn-guard** ruleset encoding the security
    rules as gates (CMK on log groups/secrets, explicit SG egress, HTTPS
    target-group health-check protocol, RDS/S3/ALB posture). A template
    change that violates a rule fails CI.
  - `tests/powershell` — Pester for `Install-ClaudeCode.ps1`'s
    `Build-ManagedSettings` (dot-sourced via the `CLAUDE_INSTALLER_DOTSOURCE`
    guard); runs on Linux pwsh.
  Toolchain is pip/npm/tarball-installable (pytest+moto+pg8000, `bats`,
  `cfn-lint`, `cfn-guard`, `pwsh`+Pester); CI runs the same commands per job
  on `ubuntu-latest`.
- Cheap extra checks outside the tested surface: `bash -n` each changed
  script, YAML-parse changed templates, `py_compile` the Lambda.
- Diagrams are hand-laid-out SVGs from `docs/diagrams/generate.py`; **rasterize
  and look at them** (cairosvg) before committing. Never use Mermaid — its
  auto-layout produced unreadable, sometimes non-rendering output here.
- Keep `docs/security-review-2026-07.md` in sync when a finding's status
  changes; keep the Status section above honest.
- Commit trailers: end messages with
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Durable context that isn't obvious from the code

- **GovCloud model availability:** Opus 4.8 (`us-gov.anthropic.claude-opus-4-8`,
  un-dated ID) and Sonnet 4.5 (`us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0`,
  dated). Sonnet 4.6 / Sonnet 5 are NOT in GovCloud. Verify model IDs against
  the Bedrock console before changing defaults.
- **User decisions (2026-07-15):** precompiled native `claude` binary only (no
  npm distribution); Grafana auth = Okta SSO; Object Lock deferred.
- **Landing zone:** hub-and-spoke with Transit Gateway (not peering); central
  egress; the workload VPC is a no-NAT spoke in the target profile.
- **Some C12 assumptions are doc-verified, not deploy-verified** — the async
  rotation's EventBridge/SecretId event shape especially. The test run is
  where these get confirmed.

## Rules

Hard rules live in `.claude/rules/*.md` (`security`, `cloudformation`,
`scripts`, `process`). Claude Code **auto-loads** that directory at session
start — no import needed — so they are always in effect. Follow them; add new
cross-cutting rules there rather than inline here.

To add a rule file, copy `.claude/rules/TEMPLATE.md.example` to a new `.md`
file. It shows the house style and the optional `paths:` frontmatter that
scopes a file to matching paths — the four active files omit it deliberately
(their rules are cross-cutting), so they load every session.
