# Security & operational review — 2026-07

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

| Batch | Theme | Recommended handling |
|---|---|---|
| **A** | Will break on first deploy in this exact profile | Fix before ship — unambiguous |
| **B** | ZPA / landing-zone operational prerequisites | Document as prerequisites; some code |
| **C** | FedRAMP High / IL4-5 compliance posture | Deliberate hardening batch; ATO-boundary decisions |
| **D** | Cross-stack trap + quick correctness bugs | Fix — low risk |

---

## A. Will break on first deploy

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

**B1. DNS resolves at the App Connector, not the laptop.** With ZPA, Client
Connector answers the app-segment FQDN with a synthetic 100.64/10 IP; real
resolution of the corporate CNAME → `internal-*.elb.amazonaws.com` → private A
records happens on the App Connector. The repo's DNS design ("corporate-DNS
CNAME… No private hosted zone required") assumes client-side resolution and
never states this. Consequences:
- App Connectors in an AWS VPC cannot resolve the corporate CNAME unless Route
  53 Resolver outbound rules forward the corporate zone to on-prem AD DNS; the
  VPC `.2` resolver knows nothing of it (NXDOMAIN at the connector, looks like a
  ZPA timeout to the user).
- On-prem App Connectors need internet-capable DNS to resolve the public
  `internal-*.elb.amazonaws.com` name (which returns private IPs).

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

**C7. Grafana: single shared admin, broad exposure (AC-2/AU-2).** See the
dedicated explanation below — this is addressable with Grafana's own user model
and/or Okta SSO plus a narrower network path.

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

1. **A + D now** — unambiguous bugs/breakage, low risk.
2. **B** — a "ZPA & landing-zone prerequisites" README section (mostly docs;
   `ClientIngressCidr` guidance and the verify-script caveats are code/comment).
3. **C** — a deliberate compliance-hardening batch (CMKs, end-to-end TLS,
   Grafana SSO/accounts, pgaudit, Object Lock), scoped against the client's ATO
   boundary and SSP control baseline.
