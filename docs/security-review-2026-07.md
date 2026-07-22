# Security & operational review — 2026-07

> **This doc is also the session handoff.** A follow-up session should read this
> top section first, then the README, then the batched findings below.

## Handoff — where things stand

**What this is.** A client-configurable, code-driven deployment of Anthropic's
self-hosted Claude apps gateway for Claude Code in AWS GovCloud `us-gov-west-1`,
Bedrock inference (Opus 4.8 + Sonnet 4.5), Okta OIDC, offline Windows rollout,
and an optional usage/cost observability stack. Full architecture and rationale
are in `README.md` (Design decisions, VPC endpoints, Usage & cost observability
sections). Repo: `github.com/EntropyZero/claude-gateway-in-govcloud`, branch
`main`.

**What is built and committed** (working tree clean as of this doc):
- `cloudformation/01-database.yaml` — RDS PostgreSQL store.
- `cloudformation/02-gateway.yaml` — ALB + TLS, ECS Fargate gateway, IAM,
  secrets, VPC endpoints (Bedrock + optional supporting/S3), egress-proxy env,
  ACM expiry alarm, ALB access logs, telemetry-forward toggle, activity-log
  toggle.
- `cloudformation/03-observability.yaml` — AMP workspace, ADOT collector
  (OTLP→SigV4 remote_write), self-hosted Grafana at `/grafana`, activity-log
  archive chain (CloudWatch window → Firehose → S3).
- `docker/` — gateway container + entrypoint; `docker/grafana/` provisioned
  image (AMP datasource + usage dashboard).
- `client/` — offline release mirror + non-admin Windows installer
  (`-CostCenter`/`-Team` OTEL attributes, full update lockdown).
- `scripts/` — deploy.env-driven automation; **outputs auto-persist back into
  `deploy.env`** (cert ARN, image URIs, the AMP telemetry params) via `set_env_var` in
  `common.sh`, so there are no manual copy-paste steps between runs.

**Status as of 2026-07-15: batches A and D are APPLIED, batch B is
documented (README "ZPA & landing-zone prerequisites" + script/installer
changes), and C7 + C8 are IMPLEMENTED (Grafana Okta SSO + `GF_*`
hardening).** The remaining open work is the rest of batch C — see "What
remains" below. Line references in the findings predate the fixes (commit
`30bb899`) and have drifted.

**Decisions made (user, 2026-07-15):**
1. **Client distribution is the precompiled native binary only** — the
   Node/npm distribution is out of scope. B5 was re-scoped accordingly: the
   README documents the enterprise-CA check for the native build, and the
   installer gained `-ExtraCaCertPath` (writes `NODE_EXTRA_CA_CERTS`, which
   the precompiled build honors) as the fallback if the Windows store isn't
   consulted. Still verify once against the pinned version.
2. **S3 Object Lock (C9) is DEFERRED** — revisit later if/when determined
   necessary; not part of the hardening batch for now.
3. **Grafana auth = Okta SSO (C7 Option 1)** — implemented: generic-OAuth
   SSO against the same Okta issuer, strict Okta-group→role mapping, login
   form disabled (break-glass `admin` behind `GRAFANA_DISABLE_LOGIN_FORM=false`),
   new `<prefix>/grafana-oidc-client-secret` + `set-grafana-oidc-secret.sh`.
   Option 3 (separate ZPA app segment for `/grafana`) is documented as a
   client-side option, not code.

**Corrections already folded in during review:**
- B1 (App Connector DNS) was corrected: an internal ALB's
  `*.elb.amazonaws.com` name is a *public* record returning private IPs
  (resolvable anywhere), so the only DNS requirement is that App Connectors
  resolve the **corporate CNAME**. See B1 for the one config case (in-VPC
  connectors) and the rebinding edge case.

**Self-review of the fix batch (2026-07-15, multi-agent review of the diff)**
surfaced and fixed several issues in the fixes themselves:
- Grafana SSO had no egress-proxy plumbing — in a proxy-mandated landing
  zone the Okta token exchange would time out with the login form disabled
  (total lockout). 03 now takes `HttpsProxyUrl` (same value as the gateway).
- Grafana (no OIDC discovery) derives its OAuth endpoint URLs from the
  issuer. Initially this assumed a custom auth server (`<issuer>/v1/...`);
  it now supports the **org** server too via an `OktaAuthServerType`
  toggle (`org` → `<issuer>/oauth2/v1/...`, built-in `groups` scope;
  `custom` → `<issuer>/v1/...`), because this deployment can only use the
  org authorization server. The `OktaIssuer` pattern accepts either form
  and rejects a trailing slash.
- The deploy-gateway.sh telemetry guard now only clears
  `OBSERVABILITY_OTLP_URL` on a definitive missing/never-came-up stack
  (incl. `ROLLBACK_COMPLETE`); any other describe-stacks failure
  (permissions/throttle/expired creds) is fatal instead of silently
  disabling forwarding fleet-wide.
- `DeregistrationDelaySeconds` is now a parameter with the honest trade-off
  documented (full delay is always waited → +5 min per deploy; streams
  older than the delay are still cut on deploys).
- `NO_PROXY` scoped to `.${NamePrefix}.internal` (bare `.internal` would
  bypass the proxy for corporate `*.internal` zones).
- `TASK_CPU`/`TASK_MEMORY` plumbed through deploy.env/deploy-gateway.sh
  (the D7 Rules were otherwise unreachable via the repo's own tooling).
- ECR immutability is back-filled onto pre-existing repos
  (`put-image-tag-mutability` runs every time, not just at create).
- verify-gateway.sh now cross-checks the served cert's SHA-256 against the
  ACM-imported cert when credentials allow — catches ZIA inspection signed
  by a corporate intermediate that the `zscaler` issuer heuristic misses.
- Installer: `-WhatIf` no longer breaks on the staging copy (binary phase
  is described and skipped); elevated interactive runs get a profile-owner
  warning; `csr` key generation removes a pre-existing key file (umask
  doesn't fix modes on overwrite).
- Dedup: `put_secret_and_roll` + `ensure_ecr_repo`/`ecr_login` helpers in
  common.sh; `OBS_STACK_NAME` centralized in deploy.env; CGNAT regex
  hoisted in verify-gateway.sh; `GrafanaServiceName` output uses
  `!GetAtt GrafanaService.Name`.
- README documents the mirror script's fail-closed GPG behavior
  (`ANTHROPIC_GPG_KEY` / `ALLOW_UNVERIFIED_MANIFEST=1`).

**Batch C implementation (2026-07-15, same session):** C1, C3, C4, C5, C6,
C10, C11 implemented; C2 implemented for ALB→gateway and ALB→Grafana
(per-task self-signed certs, ALB re-encrypt), with the gateway→collector
OTLP hop documented as an SSP-scoped option (the gateway validates
telemetry TLS against the system trust store only — no custom-CA setting).
A new latent bug was found during C11: the RDS-managed master secret
auto-rotates every 7 days while ECS injects PGPASSWORD at task start —
tasks older than a rotation would fail new DB connections. The interim
fix (an EventBridge/CloudTrail → Lambda roll hook) was superseded the
same day by **C12** (user-raised): the gateway now connects as a
least-privilege application user whose secret is rotated by the stack's
own Lambda, with the service roll built into the rotation itself; the
master secret became break-glass and its rotation affects no running
task. See the C-batch header below for the item-by-item mapping.

**OTLP collector → localhost sidecar (2026-07-22, closes C2 by hop
elimination).** The first live telemetry attempt disproved the documented C2
posture: the Claude apps gateway **refuses** a non-HTTPS `telemetry.forward_to`
URL unless the host is localhost, so the "plaintext-but-SG-scoped"
gateway→collector OTLP hop the review had accepted could never boot. Decision
(user-approved 2026-07-22): run the ADOT collector as a **localhost sidecar**
inside the gateway task instead of a standalone service in 03. The network hop
is eliminated (loopback within one Fargate task network namespace), which
resolves SC-8 by **absence of transmission** — a stronger posture than TLS,
with no new PKI, cert, or CA (SC-17/SC-12 surface: none). Alternatives
considered and rejected: an enterprise-CA-signed collector leaf (org didn't
want the CA dependency); an ACM public cert + internal LB (ACM has "no
differences" in GovCloud, but it adds an LB + public-domain dependency); a
self-managed application CA (technically sound — OpenSSL/BoringSSL enforce name
constraints and the collector takes inline `cert_pem`/`key_pem` — but rejected
on ATO grounds: SC-17 shadow-PKI finding risk + SC-12 key-custody burden).
Mechanics:
- **02** gains a conditional `otel-collector` container (receivers bound to
  `127.0.0.1:4317` / `127.0.0.1:4318`; **no** port mappings; forward URL is
  the literal `http://localhost:4318`, which is exempt from the gateway's
  HTTPS requirement). The gateway **task role** becomes the telemetry writer —
  `aps:RemoteWrite` on the workspace + the activity log-group `logs:` actions
  (least privilege; no KMS statements, log-group encryption is service-side).
  No new SG ingress for 4317/4318 anywhere (loopback only).
- **Failure posture (added 2026-07-22, user decision): fail-closed by
  default — AU-5.** `TelemetryFailClosed=true` (default) marks the sidecar
  `Essential` and health-checks it (the ADOT `/healthcheck` binary probing
  the `health_check` extension on loopback `:13133`), and the gateway
  container carries a `DependsOn: [otel-collector, HEALTHY]`: a collector
  that exits persistently **or hangs unhealthy** stops the task rather than
  letting the gateway serve unmonitored traffic (ECS replaces the task; the
  ALB routes to the peer task meanwhile). The container `RestartPolicy`
  still absorbs *transient* crashes in place first — only re-failure within
  `RestartAttemptPeriod` escalates. `TelemetryFailClosed=false` flips the
  trade (availability over auditability; telemetry can silently gap) and
  must be recorded in the SSP if chosen. Fail-closed proves the collector is
  healthy, **not** that AMP is ingesting — that gap is closed by the next
  bullet.
- **Missing-telemetry alarm (added 2026-07-22): the end-to-end control.**
  03 now defines `${NamePrefix}-missing-telemetry`: `AWS/Usage
  ResourceCount` with `Resource=IngestionRate` scoped to the workspace via
  `ResourceId`, ≤0 for `MissingTelemetryAlarmMinutes` (default 15)
  consecutive 60 s periods, **`TreatMissingData: breaching`** — load-bearing,
  because AMP *stops emitting* the metric when nothing is ingested ("a
  metric not existing or missing is the same as the value of that metric
  being 0", AMP docs) and the CloudWatch default would park a dead pipeline
  in INSUFFICIENT_DATA forever. Notifies `AlarmSnsTopicArn` when set (same
  variable as 02's alarms); `0` disables. It catches what container health
  cannot: IAM/endpoint breakage, a mis-disabled sidecar, AMP-side ingestion
  failure — and, deliberately, a full gateway outage (no tasks = no
  telemetry; triage order in O&M runbook 9). Known benign firing: the window
  between the 03 deploy and the telemetry-enabled 02 re-run.
  **Scope — metrics only.** This alarm watches the AMP *metrics* pipeline
  (`AWS/Usage IngestionRate`); the AI-activity **audit** stream is a
  separate collector pipeline (`awscloudwatchlogs` → activity log group →
  Firehose → S3) that it does **not** see. A companion, **off-by-default**
  alarm `${NamePrefix}-missing-activity-logs` (`ActivityLogsAlarmMinutes`,
  `AWS/Logs IncomingLogEvents` on the activity group) covers audit-stream
  cessation when enabled. It is off by default and deliberately **not**
  auto-enabled with `FORWARD_ACTIVITY_LOGS`, because the audit stream is
  *intermittent* (events only on real tool use), so a short window would
  false-fire on idle periods; enable it only on continuously-active fleets
  with a window longer than the longest expected quiet gap. It cannot alone
  distinguish a broken pipeline from an idle fleet — the collector's
  `awscloudwatchlogs` exporter errors (in the `otel/` streams) are the more
  direct AU-5 signal, and the audit stream's integrity at rest is separately
  covered by the CMK + IAM-only + Retain posture on the archive.
  **Surfaced limits (not buried):** (1) it detects *total cessation*, not
  degradation — `Average ≤ 0` resets on any non-zero minute, so a pipeline
  dropping 99% of samples does not fire (a floor, not a coverage guarantee);
  (2) it is a *fail-loud* control while unverified — if the AMP vended-metric
  dimensions or GovCloud emission differ from the doc-implied values, the
  metric is permanently missing → `breaching` → the alarm sits in ALARM (or
  flaps), so **verify with `list-metrics` before wiring it to a paging
  topic** (live-verification item (f)); (3) compound-config hazard — with
  `TelemetryFailClosed=false` AND (`AlarmSnsTopicArn` unset **or**
  `MissingTelemetryAlarmMinutes=0`) there is *no* task-stop and *no*
  notification on telemetry loss: total silent loss, an AU-5/AU-12 hole. Do
  not choose fail-open without a live alarm topic; record the deviation in
  the SSP.
- **Shutdown flush (added 2026-07-22).** The gateway→collector `DependsOn`
  also reverses ECS's stop order: on every task stop — including the service
  roll that **every secret rotation forces** — the gateway is SIGTERMed and
  exits first (emitting its final OTLP exports), then the collector drains
  its batch processor (buffers up to 30 s) to AMP/CloudWatch within its
  `StopTimeout: 120` (Fargate max) before SIGKILL. Without the ordering,
  ECS SIGTERMs all containers at once and the final telemetry window of
  every roll is silently dropped.
- **03** deletes its Cloud Map `Namespace` + discovery service, the collector
  SG and the gateway↔collector SG-rule pair, and the collector
  execution/task roles, task definition, and service. It now **outputs** the
  AMP remote-write endpoint, the workspace ARN, and the activity-log-group
  name, and rewires the `aps-workspaces` endpoint SG to admit the **imported
  gateway task SG** (plus a matching egress rule on that SG). Grafana's own
  rules to that endpoint are unchanged. 03 still never exports into 02 — the
  AMP params flow via `deploy.env`, so the two-pass `02 → 03 → re-run 02`
  order is unchanged.
- **Env/scripts:** `OBSERVABILITY_OTLP_URL` and the `OtlpForwardUrl` output
  are gone, replaced by `OBSERVABILITY_AMP_ENDPOINT`,
  `OBSERVABILITY_AMP_WORKSPACE_ARN`, and `OBSERVABILITY_ACTIVITY_LOG_GROUP`
  (auto-persisted by `deploy-observability.sh`; passed to 02 alongside the
  existing `COLLECTOR_IMAGE`).

This **supersedes** findings D6 (collector single-task telemetry blackout — the
sidecar shares the gateway's lifecycle and `RestartPolicy` self-heals, so
`CollectorDesiredCount` is gone) and D8 (fresh-deploy circular order — there is
no forward URL to pre-set), and moots the collector-specific parts of A2/A5.
**Needs live verification:** (a) sidecar end-to-end — metrics landing in AMP
through the loopback receiver; (b) container `RestartPolicy`
(`ContainerRestartPolicy`) support in GovCloud `us-gov-west-1` + CloudFormation
(fallback: `Essential: true` is now the default anyway via
`TelemetryFailClosed`); (c) the pinned ADOT image still honors
`AOT_CONFIG_CONTENT` and accepts the `127.0.0.1` receiver bind; (d) the
fail-closed chain — collector health check goes green at start
(`/healthcheck` against loopback `:13133`), gateway waits on HEALTHY, and a
deliberately-broken collector config stops the task; (e) shutdown flush — a
forced service roll loses no tail-of-window metrics in AMP; (f) the AMP
vended metrics (`AWS/Usage` / `Service=Prometheus`) actually emit in
GovCloud `us-gov-west-1` — doc-implied only
(`aws cloudwatch list-metrics --namespace AWS/Usage --dimensions
Name=Service,Value=Prometheus` after telemetry flows); (g) the
missing-telemetry alarm transitions OK → ALARM when the sidecar is stopped
and back to OK when it resumes.

**No-admin client redesign (2026-07-22, resolves the B4 SYSTEM-context
contradiction).** The Windows rollout was reworked to a **no-admin default**,
which retires B4's self-contradiction rather than merely documenting around it.
`client/Install-ClaudeCode.ps1` is now **entirely user-scope**: it writes the
binary to `%USERPROFILE%\.local\bin`, adds the user PATH, and writes only an
`env` block into the **user** settings file `%USERPROFILE%\.claude\settings.json`
(`DISABLE_UPDATES`/`DISABLE_AUTOUPDATER`, `OTEL_RESOURCE_ATTRIBUTES`,
`NODE_EXTRA_CA_CERTS`). It has **no settings-push mode at all** — the
`-SettingsOnly` / `-RequiredMinimumVersion` parameters and every machine/policy
write (`%ProgramFiles%\ClaudeCode\managed-settings.json`,
`HKx\SOFTWARE\Policies\ClaudeCode`) are gone, and a SYSTEM-context run is
refused outright. B4's contradiction (a SYSTEM push installs the binary into
SYSTEM's own profile while writing machine settings) therefore **cannot arise**:
there is no push mode, and the only install path is user context. Config now
arrives on three composable channels:

- **Workstation config** — the user-settings `env` block above (no elevation).
- **Central config** — the gateway pushes settings to every connected client
  via `/managed/settings` (as it already does for telemetry); a new
  `MANAGED_CLI_GROUPS` (`ManagedCliGroups`) knob pushes
  `DISABLE_UPDATES`/`DISABLE_AUTOUPDATER` to members of the listed Okta groups
  (requires the groups claim).
- **Forced login** — `forceLoginMethod` / `forceLoginGatewayUrl` /
  `requiredMinimumVersion` are managed-only keys, deliberately moved to the
  **GPO/MDM admin channel** (GPP Registry `REG_SZ` at
  `HKLM\SOFTWARE\Policies\ClaudeCode` value `Settings`, or a GPP Files copy of
  `managed-settings.json` to `%ProgramFiles%\ClaudeCode\`). Full AD-admin steps
  in the new `docs/client-config.md`.

Sign-in is interactive (`claude` → `/login` → "Cloud gateway" → paste URL);
without forced login the compensations are the network (gateway FQDN only,
consumer Anthropic endpoints blocked) and the gateway's server-side Okta +
allowed-email-domain + minimum-client-version (2.1.195+) checks. **Needs
test-run confirmation:** (a) the interactive Cloud-gateway login flow end to
end; (b) the gateway `/managed/settings` push, including `MANAGED_CLI_GROUPS`;
(c) a GPO-delivered `HKLM\SOFTWARE\Policies\ClaudeCode` managed source being
honored by the CLI (visible via `/status`). Login-picker option, "Gateway URL"
prompt, and the `%ProgramFiles%` managed path are binary-verified against the
mirrored 2.1.211 build.

**Log-retention hardening (2026-07-18, operator decision).** Prompted by the
test-run observation that some CloudWatch logs outlive teardown while others
do not: (1) **every** `AWS::Logs::LogGroup` in all four templates now carries
`DeletionPolicy`/`UpdateReplacePolicy: Retain` — no log group is destroyed by
a stack teardown, enforced by a new cfn-guard gate
(`log_groups_survive_teardown`); (2) the two **service-auto-created** groups
that previously escaped template control entirely are now pre-created and
adopted — the RDS `postgresql`/pgaudit export group (01, CMK + 731-day
retention, closing the gap where the DB audit trail was the one group
*without* the CMK) and the two db-admin Lambda groups (02, CMK + 365-day) —
with `DependsOn` so RDS/first-invoke adopt them rather than racing to create
their own. The CMK itself was already `Retain`. **Existing-deployment
caveat:** where a service already auto-created one of those three groups, the
next 01/02 stack update fails on the name collision — export and delete the
auto-created group first (runbook §0 note). The adversarial review pass also
surfaced a real trap it then fixed: a CloudFormation change touching ONLY
`DeletionPolicy`/`UpdateReplacePolicy` on an already-deployed resource can be
dropped as a no-op (coverage-roadmap #1543), silently leaving the old delete
policy in force on the five pre-existing groups — so each of those groups
gained a load-bearing `retention-policy: retain-on-teardown` tag that makes
the update a real property diff (do not remove it; the templates say so
inline). **Needs test-run confirmation:** (a) RDS adopts the pre-created
CMK+retention group without error (universal provider practice, but no
explicit AWS doc sentence); (b) after the next 02/03/04 update, the tag is
visible on the live groups (proof the update was not a no-op and the Retain
recorded).

**Test-run deploy fixes (2026-07-16, first end-to-end run).** The first real
deploy surfaced latent deploy-breakers that static checks could not catch;
each is fixed and committed:
- Container images failed on a umask-077 / legacy-Docker-builder host —
  reworked to `--chown` + `RUN chmod` (no BuildKit `COPY --chmod`).
- ALB access-log delivery denied under `BucketOwnerEnforced` — switched the
  bucket policy to the `logdelivery.elasticloadbalancing.amazonaws.com`
  service principal (the legacy per-region ELB account writes via an ACL).
- **All three ECS services** (gateway, collector, Grafana) were missing
  `TaskDefinition` — `cfn-lint` treats it as optional (EXTERNAL/CODE_DEPLOY
  controllers). Fixed + added a `cfn-guard` rule (`ecs_service_has_taskdefinition`).
- DB-bootstrap custom resource had no `ServiceTimeout`, so a failed/hung
  Lambda blocked the stack ~1h — set `ServiceTimeout: 300`.
- **Endpoint-SG cross-stack reachability** (found by the pre-redeploy
  multi-agent review): 02's shared interface-endpoint SG admitted 443 only
  from the gateway + db-admin SGs. When 02 creates the supporting endpoints
  (private DNS forces VPC-wide routing onto them), 03's collector/Grafana
  tasks could not pull images or read secrets → 03 rollback. Fixed by
  exporting the endpoint SG from 02 and adding collector/Grafana ingress in
  03, gated on `CreateSupportingEndpoints` (must match 02). No static gate
  fits this (a semantic cross-stack reachability property); re-check it in
  any change to the endpoint SG or the observability task SGs. Same class,
  found live: an in-VPC operator/build host is also captured by the
  endpoints' private DNS — `AdminClientSecurityGroupId`
  (`ADMIN_CLIENT_SG_ID`) now grants it 443 when set.
- **Gateway config-schema mismatches** (task boot-crashed, fail-closed as
  designed): (a) `OktaIssuer` had no scheme validation, so a bare domain hit
  "oidc.issuer must be an http(s) URL" — added an `https://` `AllowedPattern`.
  (b) `models[].upstream_model` was a bare string but the gateway schema wants
  an OBJECT keyed by upstream name (`bedrock: <id>`) — fixed and added
  `tests/templates/test_gateway_config.py`, which parses the embedded
  GATEWAY_CONFIG_B64 block (invisible to cfn-lint) and asserts the shape.
  Both verified against code.claude.com/docs/en/claude-apps-gateway-config.
- **Gateway→RDS TLS trust** ("self signed certificate in certificate chain"
  on every DB connect): the gateway's Postgres client ignores the libpq
  `sslrootcert=` URL param and verifies against the runtime's default trust
  (the docs commit only to `?sslmode=require`; the native binary reads the
  OS store). RDS CAs are private, so verification always failed. Fixed in the
  gateway Dockerfile: the fetched RDS bundle is installed into the OS trust
  store (`update-ca-trust`) AND exported via `NODE_EXTRA_CA_CERTS` (the
  documented CA-extension mechanism; extends, never replaces, default roots).
  Follow-up finding: `sslrootcert=` could NOT stay in the URL — the driver
  forwards unknown URL params to the server as session parameters, and
  Postgres rejects them ("unrecognized configuration parameter") at boot.
  The URL is now `?sslmode=verify-full` only. The bootstrap Lambda was
  unaffected (pg8000 takes an explicit `cafile`). `build-and-push-image.sh`
  gained `IMAGE_TAG` for same-version rebuilds (immutable tags). Requires
  image rebuild + service roll.
- **ALB access logs raced S3 bucket-policy propagation** (intermittent —
  worked several deploys, then AccessDenied on an identical template): ELB's
  create-time test-write can land before the just-created bucket policy is
  live, and CloudFormation doesn't retry. Moved `access_logs.s3.*` AND
  `deletion_protection.enabled` out of `LoadBalancerAttributes` into a
  post-deploy step in deploy-gateway.sh (`retry_n` helper in common.sh, bats-
  tested). Deletion protection at create was also what wedged failed creates
  in DELETE_FAILED. deploy-gateway.sh now also runs `--disable-rollback` by
  default (`CFN_DISABLE_ROLLBACK`), so a failed deploy keeps healthy
  resources and the re-run continues from the failure instead of paying the
  ~30-min Lambda-ENI rollback each iteration.
- **Server-side Zscaler inspection broke the Okta token exchange**: VPC
  egress to the Okta issuer passes TLS inspection, so the gateway and
  Grafana saw the inspector's derived cert and failed verification at token
  exchange. Preferred fix (added to `docs/networking-request-email.md`): an
  SSL-inspection exemption for the issuer FQDN on the server-side egress
  path — the exchange carries the OIDC client secret, which should not
  transit inspection infrastructure. Interim/fallback implemented:
  `EXTRA_CA_CERT_PATH` (deploy.env) bakes the inspection root CA into both
  images' trust stores (gateway: OS store + combined `NODE_EXTRA_CA_CERTS`
  bundle; Grafana: appended to the system bundle). Empty by default; offline
  builds verified with and without it. Follow-on (confirmed live): with TLS
  trusted, ZIA **policy** then 403'd the identity-less server-originated
  request — the networking ask is now ALLOW + inspection-exemption for the
  issuer FQDN from the VPC egress location. **OPEN as of 2026-07-17:
  awaiting that Zscaler change; gateway boot blocks on OIDC discovery until
  it lands.**
- **ALB access-log AccessDenied — root cause was landing-zone automation**:
  an LZ auto-remediation was rewriting the ALB's access-log config to a
  central logging bucket, fighting the stack and producing intermittent
  AccessDenied that mimicked first a propagation race, then a policy bug.
  The operator removed/exempted the remediation; access logs +
  deletion protection are back in `LoadBalancerAttributes` (declarative,
  drift-checked; in-template protection is safe alongside the script's
  `--disable-rollback` default). The bucket policy retains BOTH ELB delivery
  principals (service principal + legacy regional account 048591011584) —
  belt-and-suspenders that costs nothing and covers either writer.
  `BucketOwnerEnforced` accepts the legacy writer's
  `bucket-owner-full-control` canned ACL, so ACLs stay disabled. NOTE for
  new accounts: if access-log enablement fails, check for LZ automation
  first.

**What remains (open work):**
- **Deploy-time verification** — nothing has been deployed since any of
  these changes: fresh `deploy-database.sh` → `deploy-gateway.sh` →
  observability chain in a test account. Pay attention to: first pull of
  the rebuilt images (RDS CA bundle + TLS entrypoints), the HTTPS target
  groups going healthy, Grafana Okta login end-to-end (org authorization
  server, `OKTA_AUTH_SERVER_TYPE=org`, groups via the built-in scope +
  redirect URI), and a forced secret rotation
  (`aws secretsmanager rotate-secret`) triggering the service roll.
- **Existing-deployment migration**: the RDS storage CMK is a day-one
  decision — a plain 01 update cannot adopt it (fixed instance identifier
  + the db-endpoint/db-secret/kms export locks); the real path is snapshot
  → tear down 03+02 → rebuild 01 → restore data → redeploy (README
  "Teardown & update order"). The HTTP→HTTPS target-group swap needs the
  rebuilt images pushed FIRST and a maintenance window (listener points at
  the empty new TG until the first TLS task is healthy). AMP CMK on an
  existing workspace is gated behind `ENCRYPT_AMP_WITH_CMK` (enabling it
  replaces the workspace and orphans metric history). ECR repos created
  before 01 existed stay SSE-S3 forever (encryption fixed at creation) —
  recreate them if CMK coverage is mandatory.
- **First rotation check** (C12): the app-secret rotation fires
  immediately at stack creation but is ASYNCHRONOUS — the stack goes green
  either way. Confirm the secret flipped to `gateway_app_clone` and the
  service rolled (db-rotation Lambda logs; the
  `<prefix>-db-rotation-errors` alarm catches persistent failures). No
  CloudTrail/EventBridge dependency remains. Note: at long cadences the
  image Lambda is Inactive when rotation fires — one failed invoke while
  Lambda re-optimizes is expected; Secrets Manager retries complete it.
- **GovCloud endpoint-policy support pre-check**: before the first deploy
  with `CREATE_SUPPORTING_ENDPOINTS=true`, confirm the `logs` endpoint
  supports policies in the target region (the `ecs` endpoint policy was
  deliberately omitted for this reason):
  `aws ec2 describe-vpc-endpoint-services --region us-gov-west-1
  --service-names com.amazonaws.us-gov-west-1.logs
  --query 'ServiceDetails[].VpcEndpointPolicySupported'` — if false, drop
  that endpoint's PolicyDocument (IAM-side scoping remains).
- **C2 is now closed by the localhost-sidecar change** (2026-07-22 fix-log
  entry above); only its end-to-end verification remains — confirm metrics
  reach AMP through the sidecar's loopback receiver, and that the container
  `RestartPolicy` deploys in `us-gov-west-1`. C9 Object Lock stays deferred.

**Also-durable context** lives in project memory
(`claude-gateway-project-context.md`): client template (never hardcode
org-specifics), TGW landing zone, GovCloud model constraints, non-admin Windows
installs.

---

Reviewed against the target deployment profile: **AWS GovCloud `us-gov-west-1`,
government entity (FedRAMP High / IL4-5, NIST 800-53), AWS Landing Zone
hub-and-spoke (Transit Gateway; workload VPC is a spoke; central egress;
possibly centralized VPC endpoints), end users on Zscaler-secured Windows
laptops using ZPA (Zscaler Private Access).**

Two passes: a compliance/security review of the IaC and scripts, and an
end-to-end operational trace of each user/deploy flow through ZPA + TGW.
Coverage over precision — findings are not filtered for confidence; a triage
pass should confirm each against the client's actual SSP and network design.

Line references are as of commit `30bb899` and may drift as fixes land.

---

## Triage summary

| Batch | Theme | Status |
|---|---|---|
| **A** | Will break on first deploy in this exact profile | ✅ Applied 2026-07-15 |
| **B** | ZPA / landing-zone operational prerequisites | ✅ Documented (README section) + script/installer changes |
| **C** | FedRAMP High / IL4-5 compliance posture | C7+C8 ✅ implemented; C9 deferred (user decision); rest open |
| **D** | Cross-stack trap + quick correctness bugs | ✅ Applied 2026-07-15 |

---

## A. Will break on first deploy

> **Status: all applied 2026-07-15.** A1 → `AlbIdleTimeoutSeconds` param
> (default 900) + deregistration delay 300; A2 → `.internal` in `NO_PROXY`;
> A3 → `BedrockPrivateDns` param + README centralized-endpoints coverage;
> A4 → landing-zone decision trees in `deploy.env.example` + README; A5 →
> `CollectorImage` now required (mirror to ECR, pin by digest).

**A1. ALB idle timeout unset (60s default) → streaming inference truncates.**
`cloudformation/02-gateway.yaml` `LoadBalancerAttributes` sets deletion
protection, header-drop, and access logs only. A Claude extended-thinking or
long tool-result turn can exceed 60s with no bytes on the wire; the ALB closes
the connection mid-stream (truncated SSE / connection reset). Compounded by
`TargetGroupAttributes: deregistration_delay.timeout_seconds: 30`, which cuts
in-flight streams on every ECS rolling deploy (which config changes trigger).
Fix: `idle_timeout.timeout_seconds` ≥ 300 (ideally 900) as a parameter; raise
deregistration delay or ensure graceful drain.

**A2. `NO_PROXY` missing `.internal` → telemetry/audit forwarding dies under a
proxy.** When `HttpsProxyUrl` is set, the task gets `HTTP_PROXY`/`HTTPS_PROXY`
with `NO_PROXY = localhost,127.0.0.1,169.254.169.254,169.254.170.2,.amazonaws.com`.
The telemetry forward URL `http://otel-collector.<prefix>.internal:4318` is not
covered, so the gateway sends OTLP to the corporate proxy, which cannot resolve
a Cloud Map private name. All metrics — and the activity-log audit stream, if
enabled — silently stop. Fix: add `.internal` (or the exact namespace) to
`NO_PROXY`.

**A3. Bedrock endpoint private-DNS conflict with centralized endpoints.**
`CREATE_BEDROCK_ENDPOINT` defaults true with `PrivateDnsEnabled: true`. If the
shared-services spoke already centralizes a `bedrock-runtime` endpoint (with an
associated private hosted zone), stack creation fails. The README's
centralized-endpoint warning covers only the supporting endpoints, not Bedrock
or the AMP endpoint. Disabling the local endpoint to resolve the conflict also
silently drops the endpoint-policy guardrail the design relies on. Fix: a
`BedrockPrivateDns` toggle + documentation covering all three endpoint sets.

**A4. Default endpoint settings are wrong for a no-NAT spoke.**
`CREATE_SUPPORTING_ENDPOINTS` defaults false and `PRIVATE_ROUTE_TABLE_IDS` is
empty, so a copy-paste deploy into a no-NAT spoke fails at image pull
(`CannotPullContainerError`) unless a TGW→central-NAT path through inspection is
confirmed with ECR/S3 domains allowlisted. Correct guidance exists in the
README but the defaults contradict it for this profile.

**A5. ADOT collector image is `:latest` from public ECR.** Unpinned and
internet-hosted — unreachable at runtime from a locked-down spoke and a
supply-chain risk (upstream can swap the image). Inconsistent with the
checksum+GPG rigor applied to the `claude` binary. Fix: pinned digest default,
mirroring to ECR mandatory.

---

## B. ZPA / landing-zone operational prerequisites

> **Status: addressed 2026-07-15.** B1/B2/B6/B7/B8/B9 → README
> "ZPA & landing-zone prerequisites" section; B2 also in
> `deploy.env.example` comments; B3 → verify-gateway.sh ZPA caveats +
> synthetic-answer note + hard FAIL on a Zscaler-issued cert; B4 →
> installer refuses SYSTEM binary installs, new `-SettingsOnly` two-phase
> mode + README; B5 → re-scoped to the precompiled binary (user decision),
> README note + installer `-ExtraCaCertPath` → `NODE_EXTRA_CA_CERTS`.

**B1. DNS resolves at the App Connector, not the laptop.** With ZPA, Client
Connector answers the app-segment FQDN with a synthetic 100.64/10 IP; the real
lookup happens on the App Connector, using that host's resolvers. The **only**
DNS requirement is that every App Connector can resolve the **corporate CNAME**
(`claude-gateway.example.com`). The CNAME target — the internal ALB's
`internal-*.elb.amazonaws.com` name — is a normal **public** DNS record that
returns the ALB's private IPs from any resolver anywhere (resolvable
everywhere; routable only inside the VPC), so it needs no Resolver inbound
endpoint, conditional forwarder, or private hosted zone. This is unlike a Route
53 private hosted zone, which is split-horizon and only answers inside the
associated VPC.

- **Connectors on-prem** already query AD DNS, so the corporate CNAME resolves
  natively — nothing to configure.
- **Connectors in an AWS VPC** use the `.2` resolver, which knows nothing of the
  corporate zone. Add a Route 53 Resolver **outbound** rule forwarding that zone
  to AD DNS (plus a network path to those DNS servers). NXDOMAIN at the connector
  otherwise, which surfaces to the user as a ZPA timeout with no obvious cause.
- **Edge case — DNS rebinding protection.** A resolver/appliance that strips
  RFC1918 answers out of public-zone responses (an anti-rebinding control, and
  the kind of thing a hardened gov resolver may enforce) would break resolution
  of the `internal-*.elb.amazonaws.com` target. Uncommon, but worth a check; the
  escape is a PHZ associated to the connector VPC (or a hosts entry) so the
  connector never touches the public ELB name.

**B2. `ClientIngressCidr` must be the App Connectors' source IPs**, not user or
CGNAT ranges. Default `10.0.0.0/8` is simultaneously too broad (auditor
finding) and possibly wrong (on-prem connectors in 172.16/12 or 192.168/16, or
in a differently-numbered spoke, are silently dropped by the ALB SG).

**B3. `verify-gateway.sh` is misleading through ZPA.** The `dig` check passes on
the synthetic CGNAT answer even if the corporate CNAME doesn't exist; the AAAA
check can false-fail on ZPA IPv6 synthetic ranges and can never detect a
genuinely dual-stack ALB from behind ZPA. The DNS assertions must be run from
the App Connector's resolution context. The TLS/OAuth checks are correct through
ZPA (no TLS interception), but there is no guard against the FQDN accidentally
routing via ZIA-with-inspection (which would print the Zscaler intermediate's
fingerprint as the "published pinning fingerprint").

**B4. Intune/SCCM (SYSTEM context) install is self-contradictory.** A SYSTEM
push writes `%ProgramFiles%` managed settings (good) but installs the binary to
SYSTEM's `%USERPROFILE%\.local\bin` and SYSTEM's PATH — the developer never gets
`claude.exe`. And SYSTEM traffic isn't carried by the ZPA *user* tunnel, so the
UNC pull needs a Zscaler **Machine Tunnel**. The per-user install location and
MDM push need reconciling (user-context deployment, or a two-phase install).
**Resolved by the 2026-07-22 no-admin redesign** (fix-log entry above): the
installer is user-scope only with no settings-push mode, so there is no
SYSTEM-context settings push to contradict; forced login moved to the GPO/MDM
admin channel (`docs/client-config.md`).

**B5. CLI TLS trust may need `NODE_EXTRA_CA_CERTS`.** The README's prerequisite
is the enterprise root CA in the Windows cert store, but a Node-based
`claude.exe` may not consult it — `/login` could fail TLS before the fingerprint
prompt. The installer's managed `env` block never sets `NODE_EXTRA_CA_CERTS`.
Verify against the pinned CLI version; potential hard break.

**B6. Egress-proxy hostname resolution.** If `HttpsProxyUrl` is a corporate
name, the VPC resolver must resolve it (Route 53 Resolver outbound rules for the
corporate zone) — otherwise the gateway can't resolve its proxy and Okta login
breaks the moment the proxy is configured.

**B7. Central inspection allowlist.** TGW→central-NAT egress for the gateway
subnet must be allowlisted at the inspection firewall for: the Okta issuer,
`aps-workspaces.us-gov-west-1.amazonaws.com` (if the AMP endpoint isn't used),
and ECR/S3 domains (if supporting endpoints aren't used). Near-universal in gov
landing zones; unmentioned.

**B8. X-Forwarded-For collapses to connector IPs.** `trusted_proxies:
["${VpcCidr}"]` means every user appears to originate from a handful of App
Connector IPs in gateway logs/audit. Any per-IP behavior or per-user network
attribution is lost.

**B9. UNC file share over ZPA** needs its own app segment (fileserver FQDN, TCP
445), addressed by FQDN (synthetic IP + Kerberos SPN concerns) — separate from
the gateway segment.

---

## C. FedRAMP High / IL4-5 compliance posture

> **Status: implemented 2026-07-15** (except C9, deferred by user decision).
> - C1 → one CMK (created in 01 or bring-your-own `KMS_KEY_ARN`; exported)
>   covering RDS + master secret, all Secrets Manager secrets, CloudWatch
>   log groups, activity archive (SSE-KMS + Firehose key perms), AMP, and
>   ECR at repo creation. ALB-logs bucket stays SSE-S3 (ELB limitation).
> - C2 → ALB→gateway (`listen.tls`, per-task self-signed cert from the
>   entrypoint) and ALB→Grafana (TLS entrypoint in the image) encrypted;
>   both target groups HTTPS (Name dropped - protocol changes replace TGs).
>   Gateway→collector OTLP hop **eliminated 2026-07-22**: the collector is now
>   a **localhost sidecar** in the gateway task (loopback, no network hop)
>   after the live run showed the gateway rejects a non-HTTPS forward URL off
>   localhost — SC-8 by absence of transmission. See the finding below and the
>   fix-log entry at the top.
> - C3 → `sslmode=verify-full` + GovCloud RDS trust bundle fetched by the
>   build script and baked into the image.
> - C4 → pgaudit (`ddl,role,write` default, parameterized) + log_statement
>   ddl + connection logging; log_parameter off to keep user content out.
> - C5 → policies on ecr.api/ecr.dkr/logs/secretsmanager/aps endpoints and
>   the S3 gateway endpoint (this-account + ECR starport layer bucket).
> - C6 → IAM + endpoint policy scoped to exactly the two configured models
>   (profile IDs + derived foundation-model IDs; region * kept for geo
>   profile fan-out).
> - C10 → explicit egress on every SG (inline lists remove default
>   allow-all; standalone rules where inline would cycle; 03 attaches
>   ALB→Grafana and gateway→collector rules to the imported SGs; proxy
>   port derived by the deploy scripts).
> - C11 → superseded by C12 below: the app DB secret rotates via the
>   stack's own Lambda (roll built into finishSecret); the master secret
>   became break-glass, so its RDS-managed weekly rotation affects no
>   running task. The interim EventBridge/CloudTrail roll hook was
>   removed. JWT/Okta/Grafana runbooks in README.

**C12 (new, user-raised 2026-07-15). Gateway connected as the RDS master
user (AC-6).** The application held `rds_superuser`-adjacent power: it
could create roles, reach any database on the instance, and quiet its own
pgaudit trail (AU-9). IAM database auth doesn't fit (the gateway takes one
static postgres URL; IAM tokens are 15-minute, per-connection).
**Implemented:** a `docker/db-admin/` Lambda container image (pg8000 +
RDS CA bundle) provides (a) a CloudFormation custom resource that
bootstraps `gateway_owner` (NOLOGIN, owns the schema) plus
`gateway_app`/`gateway_app_clone` (LOGIN, `SET role` to the owner at
login, no instance-wide powers), and (b) an alternating-users rotation
function for the new `<prefix>/db-app-user` secret that force-rolls the
gateway service in `finishSecret` (Secrets Manager retries the step; the
label move is idempotent). Tasks inject only the app secret; the master
secret is break-glass. An `ecs` interface endpoint joined the supporting
set (the VPC-attached rotation Lambda needs the ECS API in no-NAT
spokes). First rotation fires at stack creation as an automatic
end-to-end validation.

**C1. No customer-managed KMS keys anywhere.** RDS (`StorageEncrypted: true`,
no `KmsKeyId`), Secrets Manager (RDS master, Okta, JWT, Grafana admin secrets),
S3 (ALB logs + activity archive use SSE-S3/AES256, not SSE-KMS), CloudWatch Logs
(no `KmsKeyId` on any group), ECR (no encryption config). High packages
generally require CMKs so key policies, rotation, and CloudTrail key-usage
events are customer-controlled. The README already flags this for the activity
archive but the code implements it nowhere.

**C2. Plaintext in transit on internal hops (SC-8).** *As raised:* ALB→task
`:8080`, gateway→collector OTLP `:4318`, and Grafana `:3000` are unencrypted
HTTP. Telemetry carries `user.id`/`user.email`/`user.groups` and, when the
activity stream is enabled, full command/tool-input content. Recommend TLS to
the target groups (internal-CA cert on the container, ALB re-encrypt) and
TLS/mTLS on the OTLP receiver.

**Resolution.**
- **ALB→task and ALB→Grafana:** encrypted end-to-end — a per-task ephemeral
  cert generated in the entrypoint, HTTPS target groups with
  `HealthCheckProtocol: HTTPS`, ALB re-encrypt. Implemented 2026-07-15.
- **Gateway→collector OTLP: the accepted "plaintext-but-SG-scoped" risk is
  WITHDRAWN as unimplementable, and the hop is eliminated (2026-07-22).** The
  live run showed the gateway **refuses** any non-HTTPS `telemetry.forward_to`
  URL unless the host is localhost, so the SG-scoped plaintext hop could not
  have booted — the compensating-control argument the review accepted was
  moot. Rather than stand up a collector TLS listener (which would need a cert
  and a CA the gateway trusts — an SC-17/SC-12 burden the org declined; see the
  fix-log entry for the alternatives weighed), the ADOT collector was moved
  **into the gateway task as a localhost sidecar**. The gateway forwards to
  `http://localhost:4318` over the loopback interface inside a single Fargate
  network namespace; there is no on-the-wire telemetry leg to encrypt, so SC-8
  is satisfied by **absence of network transmission** — a stronger posture than
  TLS would have been, with no new key material. As a side effect the gateway
  **task role** is now the telemetry writer to AMP and the activity log group
  (still least-privilege: scoped to this workspace and this log group only).
  **Needs live verification:** metrics landing in AMP through the sidecar's
  loopback receiver.

**C3. RDS `sslmode=require`, not `verify-full`.** `docker/entrypoint.sh`
encrypts but does not validate the server certificate — no protection against
an in-VPC MITM. The stack pins the RDS CA but the client never uses it. Fix:
bundle the RDS CA bundle into the image, use `verify-full`.

**C4. No pgaudit / statement-level DB logging (AU-2/AU-3/AU-12).** The parameter
group sets only `rds.force_ssl`; `EnableCloudwatchLogsExports: [postgresql]`
exports a log that (at Postgres default `log_statement=none`) captures almost
nothing. Material audit-trail gap for a store holding session/spend data. Add
pgaudit + `log_statement`, `log_connections`, `log_disconnections`.

**C5. Interface endpoints have no endpoint policy.** `ecr.api`, `ecr.dkr`,
`logs`, `secretsmanager` (gateway stack) and `aps-workspaces` (observability
stack) have no `PolicyDocument`, unlike the Bedrock endpoint. In a landing zone
the network path then enforces nothing (SC-7 defense-in-depth lost). Scope each
to the specific ARNs used.

**C6. Bedrock IAM/endpoint policies wildcarded.** Task role and endpoint policy
use `foundation-model/anthropic.*` / `inference-profile/us-gov.anthropic.*`
rather than the two configured model IDs, so the app-layer model allowlist isn't
enforced at IAM/network layer. The `bedrock:*::foundation-model` region wildcard
also allows `us-gov-east-1` invocation. Scope to the approved models/region.

**C7. Grafana: single shared admin, broad exposure (AC-2/AU-2).** As configured
the stack only has the bootstrap `admin` account, so whoever needs the dashboard
shares the Secrets-Manager admin password — no individual attribution, and
offboarding means rotating a shared secret. Grafana OSS fully supports per-user
accounts; the fix is to use them. Three options, best to acceptable:

- **Option 1 — Okta SSO (best; reuses the existing IdP).** Grafana speaks OIDC
  natively (`GF_AUTH_GENERIC_OAUTH_*`). Point it at the same Okta auth server as
  the gateway, map an Okta group → Grafana Admin/Editor/Viewer, and set
  `GF_AUTH_DISABLE_LOGIN_FORM=true` to remove the shared local login. Access =
  Okta group membership, MFA via Okta, automatic offboarding, real identity per
  login. Requires registering the redirect URI
  `https://<fqdn>/grafana/login/generic_oauth` in the Okta app and it being
  reachable through ZPA (same pattern as the gateway callback). Users are
  authoritative in Okta, which also sidesteps the persistence problem below.
- **Option 2 — provisioned Grafana users (self-contained fallback).** Create a
  handful of named users with per-user passwords/roles via provisioning or a
  post-deploy API call; keep `admin` as break-glass only. No dependency on the
  OIDC round-trip through ZPA. Trade-off: passwords live in Grafana's DB (another
  store to manage/rotate), MFA not built in. Fine for 3–5 admins in a controlled
  network.
- **Option 3 — narrow the network path (do regardless of 1/2).** `/grafana`
  currently rides the same ALB and `ClientIngressCidr` as developer traffic, so
  every developer can reach the login page. Put the admin console behind a
  separate ZPA app segment scoped to an admins group and/or a tighter
  listener-rule condition.

**Persistence note (blocks Option 1 & 2):** the Grafana task has no volume, so
provisioned/SSO-created users live in the container's ephemeral SQLite and reset
on redeploy. For durable accounts, point Grafana at a small database (reuse the
RDS instance with its own DB) or an EFS volume. SSO (Option 1) mostly sidesteps
this because identity is authoritative in Okta.

Recommendation: Option 1 if extending the Okta app is easy; Option 2 to keep
Grafana auth off the ZPA-OIDC path; Option 3 either way.

**C8. Missing Grafana `GF_*` hardening.** No `GF_SECURITY_DISABLE_GRAVATAR`
(default on → outbound gravatar.com calls with a hashed admin email — egress
dependency + PII leak in a controlled network), no `GF_SECURITY_COOKIE_SECURE`
(cookie likely marked non-secure since `GF_SERVER_PROTOCOL` isn't https),
no session-lifetime limits, no explicit `GF_AUTH_ANONYMOUS_ENABLED=false` /
`GF_USERS_ALLOW_SIGN_UP=false`.

**C9. No S3 Object Lock / WORM on log buckets (AU-9).** ALB-logs and
activity-archive buckets have no versioning/Object Lock; a privileged insider
could delete or shorten retention of audit logs. Recommend Object Lock
(compliance mode) or versioning + a delete-deny bucket policy.

**C10. Security groups rely on default allow-all egress (SC-7).** No
`SecurityGroupEgress` on any SG across the three templates. Scope task-SG egress
to DB / Bedrock-endpoint / Secrets-Manager-endpoint / proxy only.

**C11. Secrets lack rotation schedules (IA-5(1)).** JWT, Okta, and Grafana admin
secrets have no `RotationSchedule`; only JWT rotation is a documented manual
runbook.

---

## D. Cross-stack trap + quick correctness bugs

> **Status: all applied 2026-07-15.** D1 → AMP `Retain` + README
> "Teardown & update order"; D2 → version pattern-constrained to 16.x;
> D3 → `file://` temp file (same pattern in the new Grafana secret
> script); D4 → local staging copy + optional `-SignerThumbprint` + CN
> anchor; D5 → fail-closed (`ALLOW_UNVERIFIED_MANIFEST=1` override);
> D6 → `CollectorDesiredCount` default 2; D7 → CFN `Rules` assertions;
> D8 → deploy-gateway.sh guard + README; D9 → IMMUTABLE +
> `GRAFANA_IMAGE_TAG`; D10 → bare `GetAtt Arn`; D11 → digest-pinning
> guidance in both Dockerfiles; D12 → `umask 077` subshell.

**D1. 02↔03 cross-stack export lock + AMP workspace deletion.** The gateway
stack exports `svc-sg`, `alb-sg`, `https-listener`, `cluster-arn`, all imported
by the observability stack. While 03 exists, any 02 update that *replaces* one
of those resources fails ("Cannot update export … in use") — an SG
`GroupDescription` edit is enough to trigger replacement. Recovery requires
deleting 03 first, and **the AMP workspace has no `Retain` policy, so deleting
03 destroys the workspace and its usage/cost history.** Fix: `DeletionPolicy:
Retain` on the workspace at minimum; document the teardown order.

**D2. `DBEngineVersion` vs hardcoded `Family: postgres16`.** The version is a
free-form parameter but the parameter-group family is hardcoded to `postgres16`;
any non-16 version fails deploy. Constrain the version to 16.x or derive the
family.

**D3. `set-okta-secret.sh` secret on argv.** `--secret-string "$OKTA_CLIENT_SECRET"`
is visible via `ps`/`/proc/<pid>/cmdline`, contradicting the script's own claim.
Use `--secret-string file://<mode-600 tmpfile>` and shred.

**D4. `Install-ClaudeCode.ps1` TOCTOU + weak signer check.** The binary is
verified at `$BinaryPath` (a network share) then copied later — a writer can
swap it in between. And the Authenticode check only substring-matches
`Anthropic` in the subject rather than pinning issuer/thumbprint. Fix: copy to a
local temp, verify the local copy, then move; pin the signer.

**D5. `mirror-claude-release.sh` GPG optional.** Missing `ANTHROPIC_GPG_KEY` is a
warning, not a failure — the pipeline then trusts the manifest on TLS alone.
Should fail closed unless explicitly overridden.

**D6. Collector single task / telemetry blackout.** `DesiredCount: 1` +
`HealthCheckCustomConfig FailureThreshold: 1` means every collector redeploy is a
telemetry gap; consider 2 tasks.

**D7. `TaskCpu`/`TaskMemory` not cross-validated** — invalid Fargate combos
(e.g. 4096 CPU / 2048 MB) fail at deploy with an opaque error.

**D8. Fresh-deploy circular order.** If `OBSERVABILITY_OTLP_URL` is already set
in `deploy.env` while the collector/namespace isn't up, the gateway may fail to
resolve the forward target at startup and crash-loop, rolling back the 02
deploy. Document the ordering (02 without OTLP → 03 → re-deploy 02).

**D9. Grafana ECR repo not `IMMUTABLE`** while the gateway repo is — the one
image that also bakes in the provisioned dashboard can be silently overwritten.

**D10. Double-asterisk ARN** in the activity-log IAM resource
(`${ActivityLogGroup.Arn}*` → `…:*​*`). Cosmetic; functions as a wildcard.

**D11. Base images pinned by tag, not digest** (the gateway base — now
`public.ecr.aws/amazonlinux/amazonlinux:2023` — plus `grafana/grafana-oss`
and the ADOT/Lambda bases); no cosign/notary verification. Mitigated by the
digest-pin guidance + per-image `*_BASE_IMAGE` override for mirroring.

**D12. `import-enterprise-cert.sh` key file** written with default umask before
`chmod 600` — brief permissive window on a shared build host.

---

## E. Installer download portal (04-download-portal.yaml) — new surface

Added 2026-07-18. An **optional** stack: an Okta-secured internal website
(`https://<FQDN>/portal`) that hands a developer a single, pre-configured
Claude Code installer ZIP. New attack surface, and the controls on it:

**What it is.** A small stdlib+boto3 ECS Fargate service behind the existing
ALB via a path-based listener rule (`/portal*`, priority 20; Grafana is 10).
Same cert/DNS/Zscaler entry as the gateway — **no new network prerequisite**.
It streams a per-download ZIP (`claude.exe` stored + streamed from S3,
`Install-ClaudeCode.ps1`, a generated `install.cmd` with the developer's Team /
Cost Center + baked `-GatewayUrl`/`-Sha256`/`-DisableUpdates`, `README.txt`,
optional bundled enterprise CA).

**Authentication & authorization (AC-2/AC-3/IA-2).** The portal app runs the
**full OIDC authorization-code flow itself** — `state` + PKCE (S256) + `nonce`,
ID-token RS256 signature verified against the issuer's JWKS (cached, refetched
once on an unknown `kid` for Okta key rotation), `iss`/`aud`/`exp`/`nonce`
validated — then authorizes on **Okta group membership** (`AccessGroup`, a new
parameter naming one or more groups; a member of any is allowed). The ALB does
**not** use `authenticate-oidc`: it cannot
evaluate a groups claim, and the app must check groups anyway, so the app owns
the whole auth story (listener rule is a plain forward). Groups are read from
the ID token with a `/userinfo` fallback (mirrors the gateway's
`userinfo_fallback`). Non-members are denied **and audited**. The pure-Python
RS256 verifier does public-key modular exponentiation only (no private-key
material, no crypto dependency).

**Session (SC-23).** Short-lived HMAC-signed HttpOnly + Secure + SameSite=Lax
cookie (key = a stack-generated `portal-session-secret`, CMK-encrypted). No
refresh tokens stored; re-auth on expiry. A separate short-TTL transaction
cookie carries `state`/`nonce`/PKCE-verifier across the Okta round-trip.

**At rest (SC-28).** OIDC client secret (placeholder-`SecretString` pattern,
set out-of-band by `set-portal-oidc-secret.sh`) and session secret both
CMK-encrypted. Artifacts S3 bucket is **CMK (SSE-KMS) + bucket key**, public
access fully blocked, `BucketOwnerEnforced`, versioned, and a bucket policy
**denies non-TLS** (`aws:SecureTransport=false`). Task role gets `s3:GetObject`
on exactly that bucket (no `ListBucket`) + `kms:Decrypt`.

**In transit (SC-8).** TLS terminates on the task (self-signed leaf baked into
the image, ALB re-encrypts and does not validate it) — the ALB→task hop is
encrypted like the gateway/Grafana. Continuous byte flow on the streamed
download keeps the ALB idle timeout from tripping; the response is
close-delimited (`Connection: close`, no `Content-Length`).

**Audit (AU-2/AU-3/AU-9).** One JSON line per download **and per denial** —
timestamp, verified `user_email`/`user_groups`, team, cost_center, version,
exe SHA-256, source IP (X-Forwarded-For), user-agent, outcome — to a
**dedicated** CMK-encrypted CloudWatch log group (`/claude/<prefix>/portal-audit`,
retention parameterized). **Flag this group for SIEM.** It is deliberately
NOT routed into the activity-log stream (that surface is never widened).

**Integrity of the served binary.** The portal reads the win32-x64 SHA-256
from the published `manifest.json` (never a client value) and bakes it into
`install.cmd`; the installer verifies SHA-256 + Anthropic Authenticode before
installing. `publish-portal-release.sh` re-verifies `claude.exe` against the
manifest before upload, reusing the GPG-verified mirror output — the portal
never fetches from the internet.

**Egress note.** The portal reaches the Okta issuer outbound over the **same**
server-side egress path (+ Zscaler SSL-inspection exemption) the gateway
already requires — same in-flight prerequisite, not a new one. OIDC therefore
**cannot be verified live until that exemption lands** (see Status /
"needs test-run confirmation").

**Deferred / not-yet-live:** the Okta OIDC round-trip and the streamed download
at real (100+MB) size are not exercisable without a live deploy + the Zscaler
exemption; both are flagged for test-run confirmation. Group-claim delivery
(ID token vs `/userinfo`) depends on the Okta app's claim config and is
doc-verified only.

---

## Recommended sequencing

1. ~~**A + D now**~~ — ✅ done 2026-07-15.
2. ~~**B** — README "ZPA & landing-zone prerequisites" section~~ — ✅ done
   2026-07-15 (plus verify-script and installer code changes).
3. **C** — the remaining compliance-hardening batch (CMKs, end-to-end TLS,
   RDS `verify-full`, pgaudit, endpoint policies, IAM model scoping, SG
   egress, secret rotation), scoped against the client's ATO boundary and
   SSP control baseline. Grafana SSO/hardening ✅ done; Object Lock
   deferred by user decision.
