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

**Added 2026-07-18: the optional installer download portal (stack 04).**
Code-complete with a full test suite (`tests/portal`) green; NOT deploy-verified
— the Okta OIDC round-trip and the streamed download at real size need the live
test run (and the same in-flight Zscaler/Okta egress exemption the gateway
needs). Its new surface + controls are in `docs/security-review-2026-07.md` §E.

The full security review (`docs/security-review-2026-07.md`) is implemented:
batches A (deploy-breakers), B (ZPA/landing-zone prerequisites), C (FedRAMP
hardening C1–C11), D (correctness), and C12 (least-privilege app DB user +
self-rolling rotation). **Deferred by decision:** C9 (S3 Object Lock).
**Resolved by hop elimination — pending live verification (2026-07-22):** C2.
The live run disproved the documented "plaintext-but-SG-scoped" gateway→collector
posture — the gateway **refuses** a non-HTTPS `telemetry.forward_to` unless the
host is localhost, so that network hop could never boot — so the accepted risk
is **withdrawn as unimplementable**. The ADOT collector now runs as a
**localhost sidecar** inside the gateway task (loopback within one Fargate
network namespace), which satisfies SC-8 by absence of network transmission — a
stronger posture than TLS, with no new PKI. The sidecar **fails closed by
default** (`TelemetryFailClosed=true`, AU-5): it is Essential + health-checked
and the gateway waits on it HEALTHY, so the gateway will not serve traffic while
telemetry/audit processing is down; a **missing-telemetry alarm** (03,
`AWS/Usage ResourceCount`/`IngestionRate` on the workspace) is the end-to-end
backstop that container health cannot provide.

**Fixed 2026-07-24 (committed, NOT yet deployed): the loopback telemetry
sidecar could never work.** Live symptom: `forward to http://localhost:4318
failed: ECONNREFUSED_SSRF: blocked (cloud metadata / link-local): localhost ->
127.0.0.1`. The gateway refuses a non-HTTPS `forward_to` unless the host is
loopback — and then its SSRF guard blocks loopback — so C2's sidecar resolution
was missing a half. Fix: `CLAUDE_GATEWAY_ALLOW_LOOPBACK=1` on the gateway
container (gated on `HaveTelemetry`). It re-permits only loopback/unspecified;
EC2 IMDS and the other metadata addresses stay blocked — probe-verified.
Config validation cannot catch this (the static check sees the hostname
`localhost`, not the resolved `127.0.0.1`), which is why it surfaced only in
production.

**Added 2026-07-24 (committed, NOT yet deployed): per-user / per-group spend
caps; `MANAGED_CLI_GROUPS` retired.** 02 now configures the gateway's `admin:`
block (the master switch for spend enforcement) + two CMK-encrypted generated
admin keys, and sets `enforcement.fail_closed_on_error: true` — **an availability
trade: a spend-store outage halts all inference fleet-wide** (recovery path in
`om-runbooks.md` §10). Caps are **data, not config**: rows in `spend_limits` set
by `scripts/set-spend-limit.sh` (per user / per `rbac_group` / org-wide), so no
cap rows = no enforcement. `MANAGED_CLI_GROUPS` is gone — its update lockdown now
reaches every user via the catch-all policy — but the `groups` scope is now
requested **unconditionally**, because per-group caps resolve against the Okta
groups claim. Verified end to end against the mirrored gateway + a throwaway
Postgres (both cap scopes created and listed; read key refused for writes).

**Fixed 2026-07-24 (committed, NOT yet deployed): the client model picker
offered models the gateway does not serve.** End-to-end login now works; the
first real session showed Claude Code's own built-in `/model` menu, so every
pick failed as unauthorized. `models:` only governs what the **gateway serves** —
constraining the client's picker requires pushing `availableModels` via
`/managed/settings`, which the template never rendered (it lived only in a
comment). Worse, that comment put `availableModels` at the **policy** level,
which the gateway rejects as an unrecognized key — a boot failure had anyone
implemented it. Correct placement (binary-verified against the mirrored 2.1.211
gateway) is **inside the policy's `cli:` object**, since
`availableModels`/`enforceAvailableModels` are Claude Code `settings.json` keys.
(`cli` is *not* an unvalidated passthrough: unknown keys there are fatal — but
only checked once the Postgres store connects, so probes against a dead DB miss
it entirely.)
`GATEWAY_MANAGED_B64` is now always rendered, with an unconditional (no `match:`)
allowlist policy that needs no Okta groups claim. **Policy order is load-bearing:**
selection is first-match-wins and a `match:`-less policy matches everyone, so the
catch-all allowlist must be **last** or the group-scoped update lockdown becomes
dead config (caught pre-commit by multi-agent review; runtime-verified against a
throwaway Postgres). **Next step: confirm the deployed gateway image contains the
`GATEWAY_MANAGED_B64` stanza from `docker/entrypoint.sh` (added in `1f856ad`) -
an older image ignores the env var SILENTLY - rebuilding with a bumped tag if
not; then re-run `deploy-gateway.sh` and confirm `/model` in a live session.**

**Fixed + LIVE-PROVEN 2026-07-23 (deployed): two AMP telemetry bugs.**
(1) *CMK-encrypted AMP needs caller-side KMS.* Grafana ("Unable to retrieve
metric names") and the sidecar (missing-telemetry ALARM) both got a server-side
403. Querying a CMK-encrypted AMP workspace needs the *caller* to hold
`kms:Decrypt`; remote-write needs `kms:GenerateDataKey` — the `aps.<region>`
service grant covers only AMP's internal use, not the data-plane API. Fix
(deployed): 03's `GrafanaTaskRole` gains `kms:Decrypt` (gated on `WantAmpCmk`),
02's `telemetry-sidecar` role gains `kms:GenerateDataKey`, both scoped
`kms:ViaService=aps.${AWS::Region}.amazonaws.com`. (2) *Alarm false-fired on
idle fleet.* Client usage metrics are push-only/bursty, so an idle fleet
produced no ingestion → `missing-telemetry` (TreatMissingData: breaching) would
fire every quiet period. Fix (deployed): the sidecar's `prometheus` receiver
now scrapes the collector's own `otelcol_*` self-metrics (loopback :8888) every
30 s into the remote_write pipeline — a **continuous heartbeat** that also
exercises the full SigV4+KMS+AMP write path (genuine AU-5 liveness). **Proven:**
AMP now holds 20 `otelcol_*` series; alarm is OK. Still pending: fail-closed
stop-on-broken-config, shutdown flush, alarm OK→ALARM→OK cycle (now cheap to
test — stop the sidecar). Full proof in the 2026-07-23 fix-log entries of
`docs/security-review-2026-07.md`.

## Repo map

| Path | What |
|---|---|
| `cloudformation/01-database.yaml` | RDS PG16, the **KMS CMK** (created here, exported), db SGs, pgaudit |
| `cloudformation/02-gateway.yaml` | ALB+TLS, ECS gateway (+ optional co-resident ADOT collector **sidecar** when telemetry is on), IAM, secrets, VPC endpoints, **db bootstrap + rotation Lambdas** |
| `cloudformation/03-observability.yaml` | AMP, Grafana (Okta SSO), activity-archive chain; **outputs the AMP params the gateway sidecar consumes** (no standalone collector service — that moved into 02's task) |
| `cloudformation/04-download-portal.yaml` | **optional** Okta-secured installer download portal (ECS Fargate at `/portal`, in-app OIDC + group auth, CMK S3 artifacts + audit log) |
| `docker/` | gateway image + entrypoint; `db-admin/` (bootstrap+rotation Lambda); `grafana/`; `portal/` (download-portal app) |
| `client/` | offline release mirror + `Install-ClaudeCode.ps1` (non-admin Windows) |
| `scripts/` | `deploy.env`-driven runbook; `common.sh` holds the shared helpers |
| `docs/architecture.md` | review package: 8 SVG diagrams + secrets/SG/encryption inventories |
| `docs/conops.md` | ATO Concept of Operations: users/roles, operational scenarios, modes, accepted risks (references architecture, doesn't duplicate) |
| `docs/diagrams/generate.py` | **source of the diagrams** — edit the script, re-run, commit both |
| `docs/security-review-2026-07.md` | finding-by-finding status; the source of truth for what's done |
| `docs/test-run-runbook.md` | the deploy runbook |
| `docs/om-runbooks.md` | steady-state O&M runbooks (cert/secret rotation, CA refresh, updates, backup/restore, alarms, teardown) |
| `docs/client-config.md` | **client config & enforcement model**: no-admin user-scope installer, one-time `/login` flow, gateway `/managed/settings` push (model allowlist + update lockdown), and the **GPO/MDM forced-login** path (ops how-to; not in the PDF set) |
| `docs/networking-request-email.md` | cert/DNS/Zscaler request template |
| `docs/okta-request-email.md` | Okta OIDC app request template (org server, Web app, groups) |
| `docs/ad-request-email.md` | AD/GPO request template — the machine-policy managed setting that enables gateway login |
| `tests/` + `Makefile` | test suites (`make test`); CI in `.github/workflows/tests.yml` |

## Deploy model (details in the runbook)

Order is load-bearing: **cert → 01 database → build all four images → 02
gateway → DNS/Zscaler → verify → 03 observability → Grafana secret → 02
re-run**. 01 is first because it creates and persists the CMK so the ECR
repos are born encrypted. Scripts persist their outputs back into
`deploy.env` (`set_env_var`) so there are no copy-paste steps. **03 now emits
the AMP remote-write endpoint, workspace ARN, and activity-log-group name**
(auto-persisted to `deploy.env`); the **02 re-run** picks them up and attaches
the ADOT collector as a **localhost sidecar** in the gateway task — there is no
separate collector service. 02 never imports from 03; the params flow via
`deploy.env`, so the two-pass order is unchanged. The **optional
download portal (04)** is a fifth image + stack that slots in any time after
02 (independent of 03): `build-and-push-portal.sh → deploy-download-portal.sh
→ publish-portal-release.sh → set-portal-oidc-secret.sh`; it reuses the ALB /
FQDN / cert / Zscaler entry (path-based at `/portal`). Teardown is the reverse
(04 and 03 → 02 → 01).

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
  - `tests/portal` — pytest for the download-portal app (`docker/portal/app.py`):
    OIDC/JWT verification (RS256 against a `cryptography`-minted test JWKS),
    cookie/PKCE, group authz, dropdown validation, install.cmd/ZIP generation,
    and full HTTP-handler flows. Extend it when you touch the portal app.
  - `tests/bash` — bats for `common.sh` helpers (`proxy_port`, `set_env_var`,
    `require_vars`).
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
