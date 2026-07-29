# CLAUDE.md — working guide for this repo

Orientation for future sessions on `claude-gateway-in-govcloud`. Read this,
then the doc it points you at for whatever you're doing.

## What this is

A **client-configurable, code-driven** deployment of Anthropic's self-hosted
Claude apps gateway for Claude Code, targeting **AWS GovCloud
`us-gov-west-1`**: internal ALB + ECS Fargate gateway, RDS PostgreSQL store,
Bedrock inference (Opus 4.8 / Sonnet 5 / Sonnet 4.5 via `us-gov` inference
profiles), Okta OIDC, an offline Windows + Linux client rollout, an optional
usage/cost observability stack (AMP + ADOT sidecar + Grafana), and an
optional Okta-secured installer download portal. End users are on
Zscaler-secured laptops (ZPA) in an AWS Landing Zone (Transit Gateway,
central egress).

It is a **template, not a single deployment** — every org-specific value is a
CloudFormation parameter or a `scripts/deploy.env` variable.

## Status (keep this current)

**As of 2026-07-28: the pilot deployment is LIVE and stable.** All four
stacks deploy from this repo and the end-to-end path is proven in
production: Okta sign-in, the three-model menu on Bedrock, per-user/group
spend caps with fail-closed enforcement, client telemetry → AMP → the
Grafana dashboard, the activity archive, the download portal (Windows and
Linux), and secret rotation. The docs describe this steady state; the
hard-won symptom→cause→fix lessons from bring-up live in
`docs/operations/troubleshooting.md`, not in narrative history.

**Committed, NOT yet deployed (2026-07-29): opt-in client prompt/response
content capture in the activity stream.** Two independent flags:
`LOG_USER_PROMPTS=true` (02 param `LogUserPrompts`) pushes
`OTEL_LOG_USER_PROMPTS=1` via the managed catch-all policy so
`claude_code.user_prompt` events carry full prompt text;
`LOG_ASSISTANT_RESPONSES=true` (02 param `LogAssistantResponses`) pushes
`OTEL_LOG_ASSISTANT_RESPONSES=1` so `claude_code.assistant_response` events
carry the model's response text (clients ≥2.1.193; older ignore it). Both
default false and ride the existing activity pipeline; `deploy-gateway.sh`
refuses either without `FORWARD_ACTIVITY_LOGS=true`. When prompts are on and
responses off, `OTEL_LOG_ASSISTANT_RESPONSES=0` is pinned — unset, it falls
back to the prompts flag and would silently capture responses too
(adversarial-review catch, web-verified against the monitoring docs).
Enabling either raises the FIPS-199 confidentiality rating to High (fips-199
config table). Deploy: re-run `deploy-gateway.sh`; clients pick it up at
their next settings fetch. Needs live confirmation that the content actually
lands in the activity stream (env-var names and event semantics are
doc-verified against Anthropic's monitoring docs, not yet exercised against
this gateway).

Open security/technical findings (none critical or high) are tracked in
`docs/ato/poam.md` — the source of truth for remediation status — derived
from the point-in-time assessment `docs/ato/security-assessment-2026-07.md`,
which also records the accepted risks (deferred S3 Object Lock, SSE-S3 ALB
logs, the fail-closed spend-enforcement availability trade, and peers).
ATO-package artifacts not yet authored are registered in
`docs/ato/ato-package-gaps.md`.

## Repo map

| Path | What |
|---|---|
| `cloudformation/01-database.yaml` | RDS PG16, the **KMS CMK** (created here, exported), db SGs, pgaudit |
| `cloudformation/02-gateway.yaml` | ALB+TLS, ECS gateway (+ optional co-resident ADOT collector **sidecar** when telemetry is on), IAM, secrets, VPC endpoints, **db bootstrap + rotation Lambdas**, spend-cap admin keys, the managed client policy (`GATEWAY_MANAGED_B64`) |
| `cloudformation/03-observability.yaml` | AMP, Grafana (Okta SSO), activity-archive chain, Bedrock prompt-log destinations; **outputs the AMP params the gateway sidecar consumes** (no standalone collector service — it lives in 02's task) |
| `cloudformation/04-download-portal.yaml` | **optional** Okta-secured download portal (ECS Fargate at `/portal`, in-app OIDC + group auth, CMK S3 artifacts + audit log): installer downloads, self-usage, user guide, fingerprint, spend-cap admin. Imports 02's spend-read-key export |
| `docker/` | gateway image + entrypoint; `db-admin/` (bootstrap+rotation Lambda); `grafana/`; `portal/` (download-portal **Flask package** `portal/` + `wsgi.py`, gunicorn, vendored wheels) |
| `client/` | `Install-ClaudeCode.ps1` (non-admin Windows install) + `install-claude-code.sh` (no-root Linux install) |
| `scripts/` | `deploy.env`-driven deploy/operate chain at the root (see `scripts/README.md`); `common.sh` holds the shared helpers |
| `scripts/mirror/` | **all vendor mirroring**: Claude Code releases, ADOT collector image, base images, the Grafana AMP datasource plugin, the RDS CA bundle, Python wheel vendor dirs |
| `scripts/diagnostics/` | telemetry/usage diagnostics (`diagnose-telemetry.sh`, `amp-query.py`, `dump-usage.sh`, …) |
| `docs/README.md` | docs index: what lives where (`ato/`, `operations/`, `requests/`, `generated/`) and the PDF-partner model |
| `docs/ato/architecture.md` | ATO package: 8 SVG diagrams + secrets/SG/encryption inventories |
| `docs/ato/conops.md` | ATO Concept of Operations: users/roles, operational scenarios, modes, accepted risks (references architecture, doesn't duplicate) |
| `docs/ato/network-access-controls.md` | who-can-talk-to-what deep dive (SGs, endpoints, ingress/egress) |
| `docs/ato/security-assessment-2026-07.md` | the point-in-time security assessment: methodology, findings, verified strengths, **accepted risks**, refuted claims |
| `docs/ato/poam.md` | POA&M — **the source of truth for open findings**; update it when a change remediates one |
| `docs/ato/control-implementation.md` | NIST 800-53 control → where implemented (template) / where documented (doc §) matrix |
| `docs/ato/fips-199-categorization.md` | information types, C-I-A impacts, system categorization (org placeholders where only the org can decide) |
| `docs/ato/ato-package-gaps.md` | register + stubs for ATO artifacts not yet authored, incl. org-level dependencies |
| `docs/ato/diagrams/generate.py` | **source of the diagrams** — edit the script, re-run, commit both |
| `docs/operations/greenfield-deployment.md` | **the reusable deploy runbook**: empty VPC → client authenticated, incl. the org-prerequisite request phase and the Phase 11 end-to-end validation checklist |
| `docs/operations/om-runbooks.md` | steady-state O&M runbooks (cert/secret rotation, CA refresh, updates, backup/restore, alarms, teardown) |
| `docs/operations/troubleshooting.md` | symptom-indexed troubleshooting: what you see → cause → fix, by subsystem |
| `docs/operations/cost-controls.md` | cost-control runbook: spend caps, dashboard walkthrough, fail-closed incident response |
| `docs/operations/monitoring-and-retention.md` | product-owner summary: alarms, SNS contract, log/metric destinations + retention |
| `docs/operations/client-config.md` | **client guide**: Part I = developer **user manual** (prereqs, install, sign-in walkthrough, troubleshooting — kept free of verification tags by user decision), Part II = admin enforcement model (gateway `/managed/settings` push, **GPO/MDM forced-login** path). Part I is also extracted to `docs/generated/user-manual.pdf` (the end-user handout) |
| `docs/requests/` | copy-paste request templates: networking (cert/DNS/Zscaler), Okta OIDC app, AD/GPO managed setting |
| `docs/generated/` | PDF partners of every source doc, mirroring the source tree, plus `user-manual.pdf` (fixed path — `publish-portal-release.sh` uploads it). Regenerate with `make docs-pdf` in the same change |
| `tests/` + `Makefile` | test suites (`make test`); CI in `.github/workflows/tests.yml` |

## Deploy model (details in the runbook)

Order is load-bearing: **cert → 01 database → build all four images →
02 gateway → DNS/Zscaler → verify → 03 observability → Grafana secret →
02 re-run**. 01 is first because it creates and persists the CMK so the ECR
repos are born encrypted. Scripts persist their outputs back into
`deploy.env` (`set_env_var`) so there are no copy-paste steps. **03 emits
the AMP remote-write endpoint, workspace ARN, and activity-log-group name**
(auto-persisted to `deploy.env`); the **02 re-run** picks them up and
attaches the ADOT collector as a **localhost sidecar** in the gateway task —
there is no separate collector service. 02 never imports from 03; the params
flow via `deploy.env`, so the two-pass order is unchanged. The **optional
download portal (04)** is a fifth image + stack that slots in any time after
02 (independent of 03): `build-and-push-portal.sh →
deploy-download-portal.sh → publish-portal-release.sh →
set-portal-oidc-secret.sh`; it reuses the ALB / FQDN / cert / Zscaler entry
(path-based at `/portal`) and **requires a 02 that carries the
`${NamePrefix}-spend-read-key-arn` export** — when upgrading an older
deployment, re-run `deploy-gateway.sh` before `deploy-download-portal.sh`.
Teardown is the reverse (04 and 03 → 02 → 01).

Version/update flows (publishing portal releases before raising the minimum
client version, image-tag bumps before stack updates, Grafana/base-image
bumps via the mirror layer) are in `docs/operations/om-runbooks.md`.

## How to work here

- **Before committing nontrivial CFN / script / Lambda changes, run a
  multi-agent self-review** (finder angles + an adversarial pass that
  web-checks AWS/Postgres/Grafana semantics). This has repeatedly caught
  deploy-breaking bugs that syntax checks and docs missed. It is the single
  highest-value habit in this repo.
- **Run `make test` and keep it green before moving on / committing** (it's a
  rule — see `.claude/rules/process.md`). Five fast suites:
  - `tests/lambda` — pytest for the db-admin rotation/bootstrap Lambda (moto
    Secrets Manager + faked pg/ECS): alternating-user flip, idempotency
    guards, error propagation. **The code with real bug history — extend it
    when you touch `docker/db-admin/app.py`.**
  - `tests/portal` — pytest for the download-portal app (the
    `docker/portal/portal/` Flask package, driven via `create_app()` +
    Flask's `test_client()`): OIDC/JWT verification, cookie/PKCE, group
    authz, dropdown validation, install.cmd/ZIP generation,
    usage/admin/fingerprint/guide pages, and full HTTP flows. Extend it when
    you touch the portal app.
  - `tests/bash` — bats for `common.sh` helpers (`proxy_port`, `set_env_var`,
    `require_vars`), cert import, and the Linux installer's sourceable
    functions (`install-linux.bats`).
  - `tests/cfn` — `cfn-lint` + a **cfn-guard** ruleset encoding the security
    rules as gates (CMK on log groups/secrets, explicit SG egress, HTTPS
    target-group health-check protocol, RDS/S3/ALB posture). A template
    change that violates a rule fails CI.
  - `tests/powershell` — Pester for `Install-ClaudeCode.ps1`'s
    `Build-UserEnv` + `Write-UserSettings` (dot-sourced via the
    `CLAUDE_INSTALLER_DOTSOURCE` guard); runs on Linux pwsh.
  Toolchain is pip/npm/tarball-installable (pytest+moto+pg8000, `bats`,
  `cfn-lint`, `cfn-guard`, `pwsh`+Pester); CI runs the same commands per job
  on `ubuntu-latest`.
- Cheap extra checks outside the tested surface: `bash -n` each changed
  script, YAML-parse changed templates, `py_compile` the Lambda.
- Diagrams are hand-laid-out SVGs from `docs/ato/diagrams/generate.py`;
  **rasterize and look at them** (cairosvg) before committing. Never use
  Mermaid — its auto-layout produced unreadable, sometimes non-rendering
  output here.
- Keep `docs/ato/poam.md` in sync when a finding's status changes; keep the
  Status section above honest. Every source-doc edit regenerates its PDF
  partner (`make docs-pdf`) in the same change.
- Commit trailers: end messages with
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Durable context that isn't obvious from the code

- **GovCloud model availability:** Opus 4.8 (`us-gov.anthropic.claude-opus-4-8`,
  un-dated ID), Sonnet 5 (`us-gov.anthropic.claude-sonnet-5`, un-dated), and
  Sonnet 4.5 (`us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0`, dated) in
  the small/fast "haiku" slot — GovCloud has no Haiku-family model, and
  Sonnet 4.6 was never offered there. Verify model IDs against the Bedrock
  console (`aws bedrock list-inference-profiles --region us-gov-west-1`)
  before changing defaults.
- **User decisions:** precompiled native `claude` binary only (no npm
  distribution); Grafana auth = Okta SSO; S3 Object Lock deferred;
  first-party JS allowed in the portal (inline still banned); `Bash`
  curl/wget and subagents deliberately not denied in the managed policy.
- **Landing zone:** hub-and-spoke with Transit Gateway (not peering); central
  egress; the workload VPC is a no-NAT spoke in the target profile.
- **Managed-policy ordering is load-bearing:** gateway policy selection is
  first-match-wins and a `match:`-less policy matches everyone, so the
  catch-all allowlist policy must stay **last** or group-scoped policies
  become dead config. `availableModels`/`enforceAvailableModels` belong
  **inside the policy's `cli:` object** (they are Claude Code settings
  keys); unknown `cli:` keys are boot-fatal, but only validated once the
  Postgres store connects — a probe against a dead DB misses them.
- When something live misbehaves, check
  `docs/operations/troubleshooting.md` first — the bring-up failure modes
  (telemetry temporality, loopback SSRF guard, Zscaler/Okta egress, KMS
  detachment, PromQL pitfalls, …) are all catalogued there.

## Rules

Hard rules live in `.claude/rules/*.md` (`security`, `cloudformation`,
`scripts`, `process`, `offline-build`). Claude Code **auto-loads** that
directory at session start — no import needed — so they are always in
effect. Follow them; add new cross-cutting rules there rather than inline
here.

To add a rule file, copy `.claude/rules/TEMPLATE.md.example` to a new `.md`
file. It shows the house style and the optional `paths:` frontmatter that
scopes a file to matching paths — the five active files omit it deliberately
(their rules are cross-cutting), so they load every session.
