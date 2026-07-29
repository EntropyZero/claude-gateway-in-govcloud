# Control implementation matrix (NIST SP 800-53 rev5)

An **index**, not a narrative. Every row points at the template resource that
implements a control and the document section that explains it — the
implementation summary is one or two lines, deliberately, because the real
explanation already exists elsewhere in this package and duplicating it would
create two things to keep in sync.

Scope note: this deployment is a **client-configurable template**, not a
fielded system. Rows describe what the templates and scripts do; they do not
assert an organizational control baseline, a tailoring decision, or an
inheritance agreement — those are the deploying organization's to make. Where
a control is only partly addressed in-repo, the row says **Partial** and names
where the residual is tracked. When code and this table disagree, the code
wins and this file needs a PR.

Contents:

1. [How to read this matrix](#1-how-to-read-this-matrix)
2. [Inheritance and shared responsibility](#2-inheritance-and-shared-responsibility)
3. [AC — Access Control](#3-ac--access-control)
4. [AU — Audit and Accountability](#4-au--audit-and-accountability)
5. [CA — Assessment, Authorization, and Monitoring](#5-ca--assessment-authorization-and-monitoring)
6. [CM — Configuration Management](#6-cm--configuration-management)
7. [CP — Contingency Planning](#7-cp--contingency-planning)
8. [IA — Identification and Authentication](#8-ia--identification-and-authentication)
9. [SC — System and Communications Protection](#9-sc--system-and-communications-protection)
10. [SI — System and Information Integrity](#10-si--system-and-information-integrity)
11. [SA / SR — Acquisition and Supply Chain](#11-sa--sr--acquisition-and-supply-chain)
12. [Families not addressed by this repository](#12-families-not-addressed-by-this-repository)
13. [Deviations and accepted risks, collected](#13-deviations-and-accepted-risks-collected)

---

## 1. How to read this matrix

Each row has four columns plus a status:

- **Control** — the SP 800-53 rev5 identifier. Control *enhancements* are
  cited only where the repo demonstrably implements the enhancement.
- **Status** — one of:
  - **Implemented** — the templates/scripts implement it and the claim was
    verified against the file cited.
  - **Partial** — implemented in part; the residual is a documentation gap
    (`ato-package-gaps.md`) or an open finding (`poam.md`).
  - **Inherited** — provided by AWS GovCloud, the landing zone, Okta, or
    Zscaler; this system consumes it (see §2).
  - **Deviation** — a deliberate, recorded departure. Rationale lives in the
    accepted-risk section (§2) of `security-assessment-2026-07.md`.
- **Implementation summary** — one or two lines, no more.
- **Where implemented** — file and resource/parameter.
- **Where documented** — doc and section.

Paths are repo-relative. `<prefix>` is the deployment's `NAME_PREFIX`.
`poam.md` and `security-assessment-2026-07.md` are the companion documents in
this directory; `ato-package-gaps.md` is the documentation-gap register.

---

## 2. Inheritance and shared responsibility

Rows marked **Inherited** depend on one of these providers. The repo does not
hold the agreements or evidence for them; a full external-dependency register
is an open documentation gap (`ato-package-gaps.md`, GAP-10).

| Provider | Boundary side | What this system relies on it for |
|---|---|---|
| AWS GovCloud (US) `us-gov-west-1` | Below the boundary (IaaS/PaaS) | Physical/environmental, hypervisor, region-level compliance posture, KMS HSMs, Bedrock model hosting, service durability |
| AWS Landing Zone (org-operated) | Adjacent | Transit Gateway routing, central egress inspection, account guardrails/SCPs, log-archive account (if used) |
| Okta (org tenant) | External SaaS | User identity lifecycle, authentication strength (incl. MFA policy), group membership, session policy at the IdP |
| Zscaler (ZPA/ZIA) | Adjacent | Client-side reachability to the internal ALB, client-side web egress policy, the server-side egress ALLOW + inspection exemption for the Okta issuer |
| This system | Inside | Everything in §3–§11 below |

Boundary and trust-zone description: `conops.md` §1.4; `architecture.md` §1.
Organizational prerequisites: `conops.md` §8.2.

---

## 3. AC — Access Control

| Control | Status | Implementation summary | Where implemented | Where documented |
|---|---|---|---|---|
| AC-2 | Partial | Human identities are Okta-managed; the system defines only service/DB identities — a NOLOGIN owner role plus the alternating `gateway_app`/`gateway_app_clone` login pair, and an RDS master reserved for break-glass. Joiner/mover/leaver is Okta's, i.e. an org dependency. | `cloudformation/01-database.yaml` (`Database`, `DBParameterGroup`); `docker/db-admin/app.py` (bootstrap); `cloudformation/02-gateway.yaml` (`DbBootstrapFunction`) | `architecture.md` §4; `conops.md` §3, §8.2 |
| AC-2(3) | Partial | Deprovisioning is bounded by token/session TTL, not by an immediate revoke: portal access persists up to `SessionTtlHours` (04, default 8 h, max 24 h), spend-admin capability up to the gateway token lifetime. No "revoke this user now" procedure exists in-repo. | `cloudformation/04-download-portal.yaml` (`SessionTtlHours`); `cloudformation/02-gateway.yaml` (`session:` block, `SessionTtlHours`) | `conops.md` §6.2; accepted risk A9 in `security-assessment-2026-07.md` §2; gaps: `ato-package-gaps.md` GAP-4, GAP-12 |
| AC-3 | Implemented | Three independent enforcement points: the gateway admits an authenticated Okta user in an approved email domain; the portal requires membership of `AccessGroup`; Grafana maps Okta groups to roles strictly and denies unmapped users. | `cloudformation/02-gateway.yaml` (`allowed_email_domains` ← `AllowedEmailDomains`); `cloudformation/04-download-portal.yaml` (`AccessGroup`); `cloudformation/03-observability.yaml` (`GrafanaAdminGroup`/`GrafanaEditorGroup`/`GrafanaViewerGroup`, `GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_STRICT`) | `conops.md` §3.1, §3.5, §3.6; `architecture.md` §3 |
| AC-3 | Deviation | Okta **group** claims are not gateway access control — the `groups` scope is requested for per-group spend caps only. Gateway authorization reads as "authenticated Okta user in an approved email domain"; per-group policy is an available, unexercised extension. | `cloudformation/02-gateway.yaml` (`oidc:` scopes, `spend_limits` `rbac_group` scope) | `conops.md` §3.1; accepted risk A15 in `security-assessment-2026-07.md` |
| AC-4 | Implemented | Flow is constrained at four layers: internal-only ALB, explicit security-group rule *pairs* with default egress suppressed, VPC interface-endpoint resource policies, and IAM. The complete rule graph is tabulated, not summarized. | `cloudformation/02-gateway.yaml` + `03` + `04` (all `AWS::EC2::SecurityGroup*`, `AWS::EC2::VPCEndpoint` `PolicyDocument`s) | `network-access-controls.md` §1–§3; `architecture.md` §7 |
| AC-5 | Partial | Separation exists in practice — break-glass RDS master vs. running app user, spend admin vs. platform operator, ECS execution role vs. task role — but there is no consolidated separation-of-duties statement or privileged-role inventory. | `cloudformation/02-gateway.yaml` (execution/task role split, repeated in 03/04) | `conops.md` §3; gap: `ato-package-gaps.md` GAP-12 |
| AC-6 | Implemented | Least privilege is structural: no task injects the RDS master secret; each execution role's `GetSecretValue` list matches exactly the secrets its own task definition injects; Bedrock IAM and endpoint policies enumerate the three configured model IDs and their derived foundation-model ARNs, never a wildcard family. | `cloudformation/01-database.yaml`; `cloudformation/02-gateway.yaml` (`*ExecutionRole`, `*TaskRole`, `BedrockRuntimeEndpoint` policy) | `architecture.md` §4, §6; `network-access-controls.md` §3 |
| AC-6(1) | Deviation | The portal task holds the **read-only** spend key (`SPEND_READ_KEY`, imported from 02's `${NamePrefix}-spend-read-key-arn` export) so `/portal/me` can show a user their own caps and spend. The write key never reaches 04; the all-users admin table uses each admin's own device-flow bearer instead. | `cloudformation/02-gateway.yaml` (`SpendAdminReadKeySecret`, output `SpendReadKeySecretArn`); `cloudformation/04-download-portal.yaml` (container secret `SPEND_READ_KEY`) | `cost-controls.md` §3.4; accepted risk A10 in `security-assessment-2026-07.md` |
| AC-12 | Implemented | Sessions expire: the gateway session TTL is `SessionTtlHours` (02); the portal's signed cookie TTL is `SessionTtlHours` (04); the gateway admin bearer is short-lived and re-checked per call. | `cloudformation/02-gateway.yaml` (`session: ttl_hours`); `cloudformation/04-download-portal.yaml` (`SESSION_TTL_HOURS`) | `conops.md` §6.2; `client-config.md` §5.9 |
| AC-17 | Implemented | Remote access is the only access: the ALB is `Scheme: internal`, reachable through a Zscaler ZPA app segment, and pre-auth reachability is bounded by `ClientIngressCidr`. | `cloudformation/02-gateway.yaml` (`LoadBalancer`, `AlbSecurityGroup`) | `conops.md` §4; `architecture.md` §1, §7 |
| AC-17 | Deviation | `ClientIngressCidr` still defaults to `10.0.0.0/8` in the template. It must be set per deployment to the ZPA App Connector source range — the same ALB SG now fronts the gateway, `/grafana` **and** `/portal` (including the spend-cap admin page). | `cloudformation/02-gateway.yaml` (`ClientIngressCidr`) | `conops.md` §7; accepted risk A6 in `security-assessment-2026-07.md` |
| AC-20 | Implemented | Client-side external system use is constrained by the managed catch-all policy: `WebFetch`, `WebSearch` and `mcp__*` are denied as bare tool names, so they are removed from the model's context and cannot be re-allowed at user scope. | `cloudformation/02-gateway.yaml` (`GATEWAY_MANAGED_B64`, `cli.permissions.deny`) | `client-config.md` §6; accepted risk A8 (`Bash` and `Agent` deliberately not denied) |

---

## 4. AU — Audit and Accountability

The four audit surfaces and their retention are tabulated once, in
`monitoring-and-retention.md` §3. Rows here point at it rather than restating
numbers.

| Control | Status | Implementation summary | Where implemented | Where documented |
|---|---|---|---|---|
| AU-2 | Implemented | Five distinct record types are generated: pgaudit statement audit, ALB access logs, portal download/denial/admin audit, the gateway's `admin_audit` spend-cap trail, and the opt-in AI activity stream. Bedrock prompt logging is a sixth, opt-in and account-wide. | `01` (`DBParameterGroup` `pgaudit.log`); `02` (`AlbLogsBucket`, gateway `admin:` block); `03` (`ActivityLogGroup`, bedrock-prompts destinations); `04` (`/claude/<prefix>/portal-audit`) | `monitoring-and-retention.md` §3; `conops.md` §5.4 |
| AU-3 | Implemented | Portal audit records carry timestamp, verified Okta identity (`user_email`, `user_groups`), selections, release version, binary SHA-256, ALB-attested source IP and outcome. `admin_audit` records the acting principal as `oidc:<sub>`, with the email joined from a portal-maintained sub→email map and from the gateway's own `principal_emails` table. | `docker/portal/portal/audit.py`, `identity.py`; gateway `admin_audit` (binary schema) | `conops.md` §5.4; `cost-controls.md` §7 |
| AU-3(1) | Deviation | pgaudit runs with `pgaudit.log_parameter=0` — bind values may carry user content and are deliberately excluded, trading record completeness for data minimization. | `cloudformation/01-database.yaml` (`DBParameterGroup`) | `monitoring-and-retention.md` §3; `architecture.md` §5 |
| AU-4 | Inherited | Storage is CloudWatch Logs and S3; neither imposes an operator-managed capacity ceiling. Retention, not capacity, is the managed dimension. | — | `monitoring-and-retention.md` §3–§4 |
| AU-5 | Implemented | Telemetry/audit processing failure stops service by default: the ADOT sidecar is `Essential` + health-checked and the gateway container waits on it HEALTHY, so the gateway does not serve traffic while telemetry/audit processing is down (`TelemetryFailClosed=true`). The end-to-end backstop is the `<prefix>-missing-telemetry` alarm. | `cloudformation/02-gateway.yaml` (sidecar container definition, `dependsOn`, `TelemetryFailClosed`); `cloudformation/03-observability.yaml` (`MissingTelemetryAlarm`) | `conops.md` §6.2; `monitoring-and-retention.md` §1; `om-runbooks.md` §9 |
| AU-5 | Partial | Three audit-failure paths are **not** covered: portal audit writes fail open and unalarmed (AUD-4); the Firehose→S3 leg carrying the 731-day activity copy is unmonitored (AUD-3); the fail-closed spend store has no RDS alarm (AUD-1). Stacks 01 and 04 define zero alarms. | `cloudformation/01-database.yaml`, `cloudformation/04-download-portal.yaml` (no `AWS::CloudWatch::Alarm` resources) | `poam.md` (registered as prior AUD-1, AUD-3, AUD-4) |
| AU-6 | Partial | The sensitive streams are flagged for SIEM subscription and diagnostics tooling exists (`scripts/diagnostics/`, including ad-hoc SQL search of the ALB access logs via the optional Athena stack 05), but no review cadence, reviewer role, or SLA is defined anywhere in the repo. | `scripts/diagnostics/`; `cloudformation/05-log-analytics.yaml` | `om-runbooks.md` §14; gap: `ato-package-gaps.md` GAP-8 |
| AU-8 | Inherited | Record timestamps come from CloudWatch Logs, RDS, and the container runtime; time synchronization is an AWS platform service. Not configured by these templates. | — | — |
| AU-9 | Implemented | Audit stores are CMK-encrypted, IAM-only, public-access-blocked and TLS-only; **all nine** log groups across the four templates carry `DeletionPolicy: Retain`, so a stack teardown destroys no audit record. Five of the nine additionally carry a load-bearing `retention-policy: retain-on-teardown` tag, present so that an update touching only the deletion policy is a real property diff rather than a dropped no-op — do not remove it. | `01`–`04` (all `AWS::Logs::LogGroup`, `ActivityArchiveBucket`, prompt-logs bucket) | `architecture.md` §9; `monitoring-and-retention.md` §3 |
| AU-9 | Deviation | **S3 Object Lock is deferred by decision** — no WORM retention on the activity archive, the Bedrock prompt-log bucket, or the ALB access-log bucket. A sufficiently privileged principal can delete objects or shorten retention. | — (deliberately absent) | `conops.md` §7; accepted risk A1 in `security-assessment-2026-07.md`; `om-runbooks.md` §8 |
| AU-9 | Deviation | The ALB access-logs bucket is SSE-S3, not the CMK — ELB log delivery does not support KMS. Compensated by public-access block, IAM-only reads and 90-day lifecycle expiry. Do not "fix" it. | `cloudformation/02-gateway.yaml` (`AlbLogsBucket`) | `architecture.md` §9, §10 note 2; accepted risk A5 |
| AU-11 | Implemented | Retention is set per surface and tabulated with its parameter name and shipped default — 731 d pgaudit and activity archive, 365 d portal audit and gateway `admin_audit`, 90 d ALB logs, 150 d AMP (service default, not template-set). | `01` (`PgauditLogRetentionDays`); `02` (`AlbLogRetentionDays`); `03` (`ActivityLogWindowDays`, `ActivityArchiveRetentionDays`); `04` (`PortalAuditRetentionDays`) | `monitoring-and-retention.md` §3, §5 |
| AU-12 | Implemented | Generation is on by default for pgaudit, ALB, portal and spend-cap audit; the AI activity stream is opt-in (`FORWARD_ACTIVITY_LOGS`) and Bedrock prompt logging is opt-in and tri-state (`BEDROCK_PROMPT_LOGGING`). | `cloudformation/03-observability.yaml`; `scripts/deploy-observability.sh` | `conops.md` §5.4; `om-runbooks.md` §11 |

---

## 5. CA — Assessment, Authorization, and Monitoring

| Control | Status | Implementation summary | Where implemented | Where documented |
|---|---|---|---|---|
| CA-2 | Implemented | A point-in-time security assessment of the whole posture exists, with methodology, verification ledger and severity calibration. | — | `security-assessment-2026-07.md` |
| CA-5 | Implemented | Open findings are tracked as a POA&M with severity, owner, milestone and status. | — | `poam.md` |
| CA-7 | Partial | Automated monitoring exists (four CloudWatch alarms, Container Insights, diagnostics scripts) but there is no continuous-monitoring *strategy*: no alarm ownership, no log-review cadence, no reassessment trigger, no access-review cycle. | `cloudformation/02` + `03` alarms; `scripts/diagnostics/` | `monitoring-and-retention.md` §1; gap: `ato-package-gaps.md` GAP-8 |
| CA-3 / CA-9 | Partial | External interconnections (Okta, Zscaler, Bedrock/GovCloud) are described operationally but there is no interconnection/shared-responsibility register naming agreements, owners and failure modes. | — | `conops.md` §8.2; gap: `ato-package-gaps.md` GAP-10 |

---

## 6. CM — Configuration Management

| Control | Status | Implementation summary | Where implemented | Where documented |
|---|---|---|---|---|
| CM-2 | Implemented | The baseline is code: five CloudFormation templates, a `deploy.env` parameter set (only `deploy.env.example` is committed), digest-pinned base images persisted as `*_BASE_IMAGE`, and a pinned Grafana plugin. | `cloudformation/01`–`05`; `scripts/deploy.env.example`; `scripts/mirror/mirror-base-images.sh`; `scripts/mirror/grafana-plugin.pin` | `greenfield-deployment.md`; `om-runbooks.md` §6 |
| CM-3 | Partial | Change control is enforced by repo rules and CI, not by an org process document: `.claude/rules/process.md` mandates a multi-agent adversarial self-review of nontrivial CFN/script/Lambda diffs and a green `make test` before commit; CI runs the same suites. No emergency-change or authorization procedure is written down. | `.claude/rules/process.md`; `.github/workflows/tests.yml`; `Makefile` | gap: `ato-package-gaps.md` GAP-11 |
| CM-5 | Implemented | Change is restricted mechanically: ECR repositories are created `IMMUTABLE` (and back-filled to immutable if they pre-exist), and CloudFormation **stack policies** applied by the deploy scripts deny `Update:Replace`/`Update:Delete` on the ALB and the RDS instance, backed by deletion protection and fixed physical names. | `scripts/common.sh` (`ensure_ecr_repo`); `scripts/deploy-database.sh` and `scripts/deploy-gateway.sh` (`set-stack-policy`) | `architecture.md` §8; `.claude/rules/cloudformation.md` |
| CM-6 | Implemented | Runtime configuration is rendered from template parameters into `/etc/claude/gateway.yaml` at container start; client-side configuration is pushed centrally through the gateway's managed-settings policy rather than set per machine. | `cloudformation/02-gateway.yaml` (config block, `GATEWAY_MANAGED_B64`); `docker/entrypoint.sh` | `client-config.md` §6, §7 |
| CM-7 | Implemented | Least functionality across three surfaces: no HTTP:80 listener anywhere; Grafana's local login form disabled and its background plugin preinstaller disabled (`GF_PLUGINS_PREINSTALL_DISABLED=true`); client web/MCP tools denied in the managed policy. | `cloudformation/02-gateway.yaml` (listener set); `cloudformation/03-observability.yaml` (Grafana env) | `architecture.md` §2; `client-config.md` §6 |
| CM-8 | Partial | Component inventories exist and are maintained by hand — secrets and keys, security groups, encryption at rest — and container bases are digest-pinned. There is no SBOM and no automated component inventory. One mirror script (`mirror-collector.sh`) still lacks the architecture pin/assert its sibling has (SUP-1). | `architecture.md` inventories; `scripts/mirror/mirror-base-images.sh` | `architecture.md` §6, §7, §9; `poam.md` (prior SUP-1); gap: `ato-package-gaps.md` GAP-9 |

---

## 7. CP — Contingency Planning

| Control | Status | Implementation summary | Where implemented | Where documented |
|---|---|---|---|---|
| CP-2 | Not met (documentation) | No contingency plan document exists. Operationally, degraded-mode behavior per component *is* described, and a full rebuild runbook exists — but no declared RTO/RPO, criticality table or DR test record. | — | `conops.md` §6.2; gap: `ato-package-gaps.md` GAP-5 |
| CP-6 / CP-7 | Deviation | Single-region deployment; there is no alternate storage or processing site. Multi-AZ within `us-gov-west-1` is the only redundancy (`MultiAZ` default `true`; gateway `DesiredCount` default `2`). Cross-region DR was not implemented. | `cloudformation/01-database.yaml` (`MultiAZ`); `cloudformation/02-gateway.yaml` (`DesiredCount`) | gap: `ato-package-gaps.md` GAP-5 |
| CP-9 | Implemented | RDS automated backups run at `BackupRetentionDays` (default 14, max 35) with PITR; `DeletionPolicy`/`UpdateReplacePolicy: Snapshot` means a stack delete or replace takes a **final snapshot**; the AMP workspace, both S3 archives and every log group are `Retain`. An on-demand pre-op snapshot command is part of the runbook. | `cloudformation/01-database.yaml` (`Database`); `03` (`Workspace`, `ActivityArchiveBucket`) | `om-runbooks.md` §8 |
| CP-10 | Partial | Recovery is documented and honest about its cost: because the DB endpoint is a cross-stack export locked while imported, restore is effectively **teardown + restore**, a maintenance-window operation, not an update. Greenfield redeployment is a separate, complete runbook. No recovery-time objective is declared and no restore has been rehearsed on the record. | `cloudformation/01-database.yaml` (exports); `scripts/deploy-*.sh` | `om-runbooks.md` §8; `greenfield-deployment.md`; gap: `ato-package-gaps.md` GAP-5 |

---

## 8. IA — Identification and Authentication

| Control | Status | Implementation summary | Where implemented | Where documented |
|---|---|---|---|---|
| IA-2 | Implemented | All three human-facing surfaces authenticate against the same Okta issuer with distinct clients and redirect URIs: gateway (developer sign-in), Grafana (`/grafana/login/generic_oauth`), portal (`/portal/oauth/callback`). | `cloudformation/02-gateway.yaml` (`oidc:`); `03` (`generic_oauth`); `04` (`OIDC_ISSUER`/`OIDC_CLIENT_ID`) | `architecture.md` §3; `conops.md` §5.1 |
| IA-2(1)(2) | Inherited | Multi-factor authentication is an Okta authentication-policy decision at the org tenant; nothing in these templates sets or can verify it. | — | `conops.md` §8.2 |
| IA-5 | Implemented | Authenticator management is inventoried secret by secret with its creator, consumer and rotation mode. The application DB secret rotates automatically (`APP_SECRET_ROTATION_DAYS`, default 90) using an alternating-user scheme whose `finishSecret` forces the gateway service to roll; the remaining secrets have documented manual rotation procedures. | `cloudformation/02-gateway.yaml` (`AWS::SecretsManager::RotationSchedule`, `DbRotationFunction`); `docker/db-admin/app.py` | `architecture.md` §6; `om-runbooks.md` §2, §3, §7; `cost-controls.md` §7 |
| IA-5 | Implemented | Secret **handling** is a coded rule: values never appear on argv (mode-600 `file://` temp files via `put_secret_and_roll`), never in CloudFormation parameters, and each `set-*-secret.sh` rolls its consuming service. | `scripts/common.sh` (`put_secret_and_roll`); `scripts/set-okta-secret.sh`, `set-grafana-oidc-secret.sh`, `set-portal-oidc-secret.sh` | `architecture.md` §6; `.claude/rules/security.md` |
| IA-5(7) | Implemented | No static authenticator is embedded in the client rollout or in an image: the per-task TLS material is generated at image build and never leaves the task, and Postgres credentials arrive only as ECS secret injection at launch. | `docker/entrypoint.sh`; `cloudformation/02-gateway.yaml` (task definition `Secrets`) | `architecture.md` §6 |
| IA-6 | Implemented | The portal admin flow deliberately avoids a shared credential: it drives the gateway's OAuth **device flow** (RFC 8628) so each admin acts as themselves, the gateway re-checks the token's groups claim per call, and `admin_audit` records the individual rather than a key id. | `docker/portal/portal/gateway.py`, `views/admin.py`; `cloudformation/02-gateway.yaml` (`admin_groups` ← `SpendAdminGroups`) | `cost-controls.md` §2.1, §7; accepted risk A11 |
| IA-8 | Not applicable | There are no non-organizational users; every surface requires the org's own Okta tenant. | — | `conops.md` §3 |
| SC-23 / IA (session integrity) | Implemented | Portal OIDC is a full authorization-code flow with `state`, PKCE (S256) and `nonce`; ID-token verification hard-pins RS256, filters JWKS keys, and uses constant-time comparison. Session cookies are HMAC-signed, `HttpOnly`, `Secure`, `SameSite=Lax`. | `docker/portal/portal/crypto.py`, `oidc.py`, `views/auth.py` | `security-assessment-2026-07.md` (verified strengths) |

---

## 9. SC — System and Communications Protection

| Control | Status | Implementation summary | Where implemented | Where documented |
|---|---|---|---|---|
| SC-7 | Implemented | The boundary is an internal ALB in a no-NAT spoke VPC, with AWS API traffic pinned to interface/gateway VPC endpoints and every workload SG carrying explicit egress. | `cloudformation/02-gateway.yaml` (`LoadBalancer`, all `AWS::EC2::VPCEndpoint`); `03`/`04` cross-stack SG rules | `architecture.md` §1, §7; `network-access-controls.md` §1–§3 |
| SC-7(4) | Implemented | Every VPC interface endpoint carries a resource policy scoped to this account/workload — the bedrock-runtime endpoint to exactly the three configured model IDs and their profiles, `secretsmanager` to this deployment's secret ARNs, `aps-workspaces` to this workspace. | `cloudformation/02-gateway.yaml` (`BedrockRuntimeEndpoint`, `SecretsManagerEndpoint`); `cloudformation/03-observability.yaml` (`AmpEndpoint`) | `network-access-controls.md` §3 |
| SC-7(4) | Deviation | The `ecs` interface endpoint carries **no** endpoint policy: GovCloud does not support one on that service and attaching a `PolicyDocument` fails the stack. IAM-side scoping compensates. | `cloudformation/02-gateway.yaml` (`EcsEndpoint`) | `network-access-controls.md` (accepted risks, up front); accepted risk A7 |
| SC-7(5) | Implemented | Deny-by-default: default egress is suppressed on **every** security group (a `127.0.0.1/32` placeholder replaces the implicit allow-all), and a cfn-guard CI rule fails any template that omits explicit egress. | `01`–`04` security groups; `tests/cfn` (cfn-guard ruleset) | `network-access-controls.md` §1; `architecture.md` §7 |
| SC-8 / SC-8(1) | Implemented | No plaintext hop exists. There is no HTTP:80 listener; the ALB re-encrypts to per-task TLS on all three targets, each target group with an explicit `HealthCheckProtocol: HTTPS` (CI-gated); RDS is `sslmode=verify-full` with `rds.force_ssl=1` server-side; Bedrock, AMP and Okta legs are TLS. | `cloudformation/02-gateway.yaml` (`HttpsListener`, `TargetGroup`); `cloudformation/01-database.yaml` (`DBParameterGroup`); `docker/entrypoint.sh` | `architecture.md` §2 (per-hop table) |
| SC-8 | Implemented | The telemetry leg satisfies SC-8 by **absence of transmission**: the ADOT collector runs as a localhost sidecar inside the gateway task with its OTLP receivers on `127.0.0.1` and no port mappings, so no network hop exists to encrypt. This required `CLAUDE_GATEWAY_ALLOW_LOOPBACK=1` (gated on telemetry being enabled; re-permits only loopback/unspecified, leaving IMDS and link-local blocked). | `cloudformation/02-gateway.yaml` (sidecar container, `CLAUDE_GATEWAY_ALLOW_LOOPBACK`) | `architecture.md` §10 note 1; `conops.md` §7; accepted risk A2 (supersedes the earlier plaintext-hop acceptance) |
| SC-12 / SC-17 | Implemented | One customer-managed KMS key (`alias/<prefix>`, `EnableKeyRotation: true`) plus an ACM-imported enterprise leaf for the ALB. A self-managed application CA was considered and **rejected** to avoid an SC-17 shadow-PKI and SC-12 key-custody burden; per-task certs are ephemeral and never leave the task. | `cloudformation/01-database.yaml` (`KmsKey`, `KmsKeyAlias`); `scripts/import-enterprise-cert.sh` | `architecture.md` §6; `om-runbooks.md` §1; accepted risk A2 |
| SC-13 | Partial | The ALB listener defaults to a FIPS TLS policy (`ELBSecurityPolicy-TLS13-1-2-FIPS-2023-04`, parameter `TlsSecurityPolicy`) and KMS provides FIPS-validated key operations. The repo configures **no** AWS FIPS service endpoints, and the crypto modules behind the internal TLS legs are unverified. | `cloudformation/02-gateway.yaml` (`TlsSecurityPolicy`) | `fips-199-categorization.md` §7; gap: `ato-package-gaps.md` GAP-14 |
| SC-28 | Implemented | Everything at rest uses the single customer-managed CMK — RDS storage and snapshots, all Secrets Manager secrets, every CloudWatch log group (template-declared precisely so the CMK and retention apply), the activity and prompt-log archives, the portal artifacts bucket, AMP (creation-time `ENCRYPT_AMP_WITH_CMK`), and ECR at creation. | `01`–`04`; `scripts/common.sh` (`ensure_ecr_repo` CMK encryption) | `architecture.md` §9 |
| SC-28 | Deviation | One exception: the ALB access-logs bucket (SSE-S3). See AU-9 above. | `cloudformation/02-gateway.yaml` (`AlbLogsBucket`) | `architecture.md` §9; accepted risk A5 |

---

## 10. SI — System and Information Integrity

| Control | Status | Implementation summary | Where implemented | Where documented |
|---|---|---|---|---|
| SI-2 | Partial | ECR repositories are created with `scanOnPush=true`; image and stack updates have a full runbook; a minimum client-version floor (`requiredMinimumVersion`, parameter `MinClientVersion`, defaulting to the gateway's own version) keeps the fleet off stale clients. Missing: severity→remediation-window SLA, how `claude` binary CVEs are learned given locked auto-updates, SBOM, pen-test scope. | `scripts/common.sh` (`ensure_ecr_repo`); `cloudformation/02-gateway.yaml` (`MinClientVersion`) | `om-runbooks.md` §5, §6; `client-config.md` §5.8, §6; gap: `ato-package-gaps.md` GAP-9 |
| SI-3 | Inherited | Endpoint malicious-code protection is the managed-laptop build's; nothing in this system scans content. | — | `conops.md` §8.2 |
| SI-4 | Partial | Four CloudWatch alarms exist (`<prefix>-certificate-expiry`, `-db-rotation-errors`, `-missing-telemetry`, `-missing-activity-logs`), plus Container Insights and a diagnostics toolkit. Coverage gaps are known and tracked: no RDS alarms (AUD-1), no `failed_translations` detection (AUD-2), unmonitored Firehose leg (AUD-3), no ALB/ECS availability alarms (AUD-5). | `cloudformation/02-gateway.yaml`, `03-observability.yaml` (`AWS::CloudWatch::Alarm`); `scripts/diagnostics/` | `monitoring-and-retention.md` §1; `om-runbooks.md` §9, §9a; `poam.md` |
| SI-4 | Deviation | The `<prefix>-missing-telemetry` alarm detects **total cessation, not degradation** — `Average ≤ 0` resets on any non-zero minute, and its heartbeat is the collector's own self-metrics, which keep flowing even if 100% of client metrics are dropped. It is a floor, not a coverage guarantee. | `cloudformation/03-observability.yaml` (`MissingTelemetryAlarm`) | accepted risk A16; `poam.md` (prior AUD-2) |
| SI-5 | Partial | Alarm notification is an organizational prerequisite, not a system capability: the stacks create **no** SNS topic and accept one optional `ALARM_SNS_TOPIC_ARN`. If it is empty the alarms still show state but nobody is told. | `cloudformation/02-gateway.yaml`, `03-observability.yaml` (`AlarmSnsTopicArn`) | `monitoring-and-retention.md` §2; gap: `ato-package-gaps.md` GAP-8 and the org dependency block |
| SI-7 | Implemented | Software integrity is verified at every hop into the boundary: the release mirror GPG-verifies the manifest and **fails closed** unless `ALLOW_UNVERIFIED_MANIFEST=1` is set explicitly; every mirrored artifact is SHA-256-checked on consumption (`require_mirrored_file`, `verify_sha256`); base images are digest-pinned; ECR tags are immutable; the Windows and Linux installers verify the binary's SHA-256 from a staged copy before installing. | `scripts/mirror/mirror-claude-release.sh`; `scripts/common.sh`; `scripts/mirror/mirror-base-images.sh`; `client/Install-ClaudeCode.ps1`, `client/install-claude-code.sh` | `conops.md` §5.1; `.claude/rules/offline-build.md`; `.claude/rules/security.md` |
| SI-10 | Implemented | Operator-supplied and user-supplied inputs are validated server-side: the portal validates the cost-center/team **pair** and the platform selection regardless of what the form posted, and a malformed cost-center→teams mapping fails the task at boot. Money is exact integer-cents arithmetic with strict format validation. | `docker/portal/portal/selection.py`, `money.py`, `config.py` | `cost-controls.md` §2.1; `security-assessment-2026-07.md` |
| SI-12 | Implemented | Handling and retention for every record type — destination, retention, encryption, sensitivity flag — is tabulated in one place, with parameter names and shipped defaults. | `01`–`04` retention parameters | `monitoring-and-retention.md` §3–§5 |

---

## 11. SA / SR — Acquisition and Supply Chain

| Control | Status | Implementation summary | Where implemented | Where documented |
|---|---|---|---|---|
| SA-10 | Implemented | Developer configuration management is the repository: infrastructure is entirely declarative, parameters are externalized to `deploy.env`, and deploy scripts persist their outputs back so there are no copy-paste steps between stages. | `cloudformation/`; `scripts/common.sh` (`set_env_var`) | `greenfield-deployment.md`; `conops.md` §5.5 |
| SA-11 | Implemented | Developer testing is enforced, not optional: five suites (`tests/lambda`, `tests/portal`, `tests/bash`, `tests/cfn`, `tests/powershell`) run under `make test` and in CI, and `tests/cfn/rules.guard` encodes the hard rules as gates — CMK on log groups and secrets, log groups surviving teardown, explicit SG egress, HTTPS health-check protocol on HTTPS target groups, RDS CMK storage, S3 public-access block, and the ALB staying internal/IPv4-only. | `Makefile`; `tests/cfn/rules.guard`; `.github/workflows/tests.yml` | `.claude/rules/process.md` |
| SA-22 | Partial | Component versions are pinned and updated deliberately through a documented runbook (base images, Grafana, the ADOT collector, the `claude` client). No formal supported-version policy or end-of-support tracking exists. | `scripts/mirror/mirror-base-images.sh`; `scripts/mirror/grafana-plugin.pin` | `om-runbooks.md` §5, §6; gap: `ato-package-gaps.md` GAP-9 |
| SR-3 | Implemented | A hard architectural separation implements supply-chain control: the build/deploy host reaches **only** AWS service endpoints, all external artifacts flow through `scripts/mirror/*.sh` on a separate egress host, and build scripts *consume* the transferred mirror — failing closed with instructions naming the exact mirror script rather than fetching. No Dockerfile fetches anything at build time; Python dependencies install `--no-index` from committed, exact-pinned wheels. | `scripts/mirror/`; `scripts/common.sh` (`require_mirrored_file`); `docker/*/Dockerfile` | `.claude/rules/offline-build.md`; `om-runbooks.md` §4, §5, §6 |
| SR-4 | Implemented | Provenance is recorded per artifact: GPG-verified release manifest, SHA-256 pins for the Grafana plugin (`grafana-plugin.pin`) and the RDS CA bundle, digest-pinned base images persisted into `deploy.env`. | `scripts/mirror/mirror-claude-release.sh`, `mirror-grafana-plugin.sh`, `mirror-rds-ca-bundle.sh`, `mirror-base-images.sh` | `om-runbooks.md` §4, §5, §6 |
| SR-11 | Partial | Authenticity checks are consistent except in two places tracked as findings: `mirror-collector.sh` lacks the `--platform` pin and post-pull architecture assert its sibling has (SUP-1), and vendored wheels are installed without `--require-hashes`, so transitive versions are pinned only by which file sits in `vendor/`. | `scripts/mirror/mirror-collector.sh`, `mirror-python-deps.sh` | `poam.md` (prior SUP-1; the wheel-hash item) |

---

## 12. Families not addressed by this repository

These families are **out of scope for the code** and belong to the deploying
organization or to documentation that does not yet exist. They are listed so a
reviewer sees the absence deliberately rather than discovering it.

| Family | Why it is not here | Where it is tracked |
|---|---|---|
| AT — Awareness and Training | Security and role-based training, including AI-usage training, is an organizational program. | org dependency block, `ato-package-gaps.md` |
| PS — Personnel Security | Screening, agreements, transfer/termination actions are organizational. | org dependency block, `ato-package-gaps.md` |
| PE — Physical and Environmental | Fully inherited from AWS GovCloud and the org's facilities. | §2 above |
| IR — Incident Response | No incident-response plan exists; containment primitives do (Okta group removal, `desired-count 0`, ALB rule removal, key rotation, spend clamp) but are not assembled into a procedure. | `ato-package-gaps.md` GAP-4 |
| MP — Media Protection | Teardown retains log groups, both S3 archives, a final RDS snapshot, ECR images and the AMP workspace; no sanitization/destruction procedure or authorization step is written. | `ato-package-gaps.md` GAP-15; `om-runbooks.md` §13 |
| PL — Planning | No system security plan narrative, rules of behavior, or architecture-change planning document. | `ato-package-gaps.md` GAP-7 |
| RA — Risk Assessment | Vulnerability scanning posture is partly implemented (ECR `scanOnPush`) but undocumented; no risk-assessment document. | `ato-package-gaps.md` GAP-9 |
| PT — PII Processing and Transparency | Identity attributes, per-user usage attribution and (when enabled) command/prompt content are processed; no privacy threshold analysis exists. | `ato-package-gaps.md` GAP-6 |

---

## 13. Deviations and accepted risks, collected

Surfaced here in one place so a reviewer does not have to find them in the
rows. Full rationale, compensating controls and alternatives-considered are in
`security-assessment-2026-07.md` §2; the identifiers
below are that document's.

| Ref | Deviation | Controls touched | Row |
|---|---|---|---|
| A1 | S3 Object Lock deferred — no WORM on any audit archive | AU-9, AU-11 | §4 |
| A2 | Telemetry SC-8 met by absence of transmission (loopback sidecar), not encryption; requires `CLAUDE_GATEWAY_ALLOW_LOOPBACK=1`. **Reverses** the earlier "plaintext-but-SG-scoped" acceptance, which was withdrawn as unimplementable | SC-8, SC-12, SC-17 | §9 |
| A3 | Spend enforcement fails closed — a spend-store outage halts inference fleet-wide | AU-5, availability | `cost-controls.md` §5 |
| A4 | Bedrock prompt logging, when enabled, is account- and region-wide and has no per-user attribution | AU-2, AU-3 | §4 |
| A5 | ALB access-logs bucket is SSE-S3, not the CMK (ELB platform limitation) | SC-28, AU-9 | §4, §9 |
| A6 | `ClientIngressCidr` template default is `10.0.0.0/8` and must be tightened per deployment | AC-17, SC-7 | §3 |
| A7 | `ecs` interface endpoint carries no endpoint policy (unsupported in GovCloud) | SC-7(4) | §9 |
| A8 | Client-side `Bash` (curl/wget) and `Agent` subagents are deliberately not denied | AC-20, CM-7 | §3 |
| A9 | Group changes take effect only at session/token expiry — TTL is the deprovisioning bound | AC-2(3) | §3 |
| A10 | The portal task holds the **read-only** spend key | AC-6(1) | §3 |
| A15 | Gateway access control is email-domain-based; Okta groups are not gateway authorization | AC-3 | §3 |
| A16 | `TelemetryFailClosed` is an availability trade; the missing-telemetry alarm detects cessation, not degradation | AU-5, SI-4 | §4, §10 |

---

*Maintenance: update a row in the same change that alters the resource it
cites. If a control's status changes because a finding closed, update
`poam.md` in the same change. New documentation gaps go to
`ato-package-gaps.md`, not here.*
