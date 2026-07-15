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
  `deploy.env`** (cert ARN, image URIs, OTLP URL) via `set_env_var` in
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
- 03's `OktaIssuer` now has `AllowedPattern: ^https://.+/oauth2/.+$` — the
  gateway accepts the org-server issuer but Grafana's derived
  `<issuer>/v1/...` URLs 404 on it; this fails at deploy instead of first login.
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

**What remains (open work):**
- **Batch C except C7/C8/C9**: C1 CMKs, C2 internal TLS, C3 RDS
  `verify-full`, C4 pgaudit, C5 endpoint policies, C6 IAM/endpoint model
  scoping, C10 SG egress scoping, C11 secret rotation schedules. Scope
  against the client's ATO boundary and SSP baseline before implementing.
- Deploy-time verification of the applied fixes (nothing has been deployed
  since the changes): fresh `deploy-database.sh` → `deploy-gateway.sh` →
  observability chain in a test account, plus the Grafana Okta login flow
  end-to-end (custom auth server with `groups` claim + redirect URI).

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
push writes `%ProgramData%` managed settings (good) but installs the binary to
SYSTEM's `%USERPROFILE%\.local\bin` and SYSTEM's PATH — the developer never gets
`claude.exe`. And SYSTEM traffic isn't carried by the ZPA *user* tunnel, so the
UNC pull needs a Zscaler **Machine Tunnel**. The per-user install location and
MDM push need reconciling (user-context deployment, or a two-phase install).

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

> **Status:** C7 implemented as Option 1 (Okta SSO, strict group→role
> mapping, login form off, break-glass admin retained) and C8 implemented
> (gravatar off, secure cookies, anonymous/sign-up off, session lifetimes,
> external snapshots off). C9 **deferred** by user decision (2026-07-15).
> C1–C6, C10, C11 remain open pending ATO-boundary scoping.

**C1. No customer-managed KMS keys anywhere.** RDS (`StorageEncrypted: true`,
no `KmsKeyId`), Secrets Manager (RDS master, Okta, JWT, Grafana admin secrets),
S3 (ALB logs + activity archive use SSE-S3/AES256, not SSE-KMS), CloudWatch Logs
(no `KmsKeyId` on any group), ECR (no encryption config). High packages
generally require CMKs so key policies, rotation, and CloudTrail key-usage
events are customer-controlled. The README already flags this for the activity
archive but the code implements it nowhere.

**C2. Plaintext in transit on internal hops (SC-8).** ALB→task `:8080`,
gateway→collector OTLP `:4318`, and Grafana `:3000` are unencrypted HTTP.
Telemetry carries `user.id`/`user.email`/`user.groups` and, when the activity
stream is enabled, full command/tool-input content. Recommend TLS to the target
groups (internal-CA cert on the container, ALB re-encrypt) and TLS/mTLS on the
OTLP receiver.

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

**D11. Base images pinned by tag, not digest** (`debian:bookworm-slim`,
`grafana/grafana-oss:11.5.1`); no cosign/notary verification of the Grafana or
ADOT images.

**D12. `import-enterprise-cert.sh` key file** written with default umask before
`chmod 600` — brief permissive window on a shared build host.

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
