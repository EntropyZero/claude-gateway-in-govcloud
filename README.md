# Claude apps gateway on AWS GovCloud

Infrastructure and rollout tooling for running **Claude Code** in a controlled
enterprise network through Anthropic's self-hosted **Claude apps gateway** in
`us-gov-west-1`, with inference on **Amazon Bedrock** (FedRAMP High / IL4-5) —
targeting **Claude Opus 4.8** by default. No public internet dependencies at
install or inference time.

Everything organization-specific — gateway FQDN, Okta issuer, email domains,
model IDs, network CIDRs — is configuration (`scripts/deploy.env` and
CloudFormation parameters), so the same repo deploys for any client.

- TLS at an internal, IPv4-only ALB using an enterprise-CA-signed certificate
  (imported into ACM), reached via a corporate CNAME such as
  `claude-gateway.example.com`
- ECS Fargate gateway service, RDS PostgreSQL backing store, Okta OIDC sign-in
- Offline Windows client rollout — laptops never contact Anthropic release hosts

Deploy order is in [Quick start](#quick-start); the scripts under `scripts/`
are the executable runbook — read them for the exact AWS calls they make.
The design decisions behind the architecture — and the gotchas that must not
be re-litigated — are in [Design decisions](#design-decisions) below.

## Layout

| Path | Purpose |
|---|---|
| `cloudformation/01-database.yaml` | RDS PostgreSQL store, managed master secret, client security group |
| `cloudformation/02-gateway.yaml` | ALB + TLS listener, ECS Fargate service, IAM, secrets, optional VPC endpoints, cert-expiry alarm, ALB access logs |
| `docker/Dockerfile` | Container around the pinned, verified `claude` binary |
| `docker/entrypoint.sh` | Renders `gateway.yaml`, assembles the Postgres URL |
| `client/mirror-claude-release.sh` | Egress-side: download + verify a pinned release |
| `client/Install-ClaudeCode.ps1` | Offline Windows install — non-admin, Intune/SCCM, or manual |
| `scripts/deploy.env.example` | Per-environment parameters (copy to `deploy.env`) |
| `scripts/import-enterprise-cert.sh` | CSR generation, ACM import, fingerprint output |
| `scripts/build-and-push-image.sh` | Build the gateway image and push to ECR |
| `scripts/deploy-database.sh` | Deploy the database stack |
| `scripts/deploy-gateway.sh` | Deploy the gateway stack |
| `scripts/set-okta-secret.sh` | Set the real OIDC client secret and roll the service |
| `scripts/stack-outputs.sh` | Print both stacks' outputs |
| `scripts/verify-gateway.sh` | Post-deploy DNS / TLS / OAuth endpoint checks |

## Quick start

Prerequisites: a VPC with ≥ 2 private subnets; an Okta OIDC web app (redirect
URI `https://<GatewayFqdn>/oauth/callback` — it must exist before first login
but can be created ahead of the stack); Bedrock model access enabled for
Claude in `us-gov-west-1`; and an egress path from the private subnets to the
Okta issuer — OIDC is the one external dependency no VPC endpoint can cover.
That path can be a local NAT gateway, a Transit Gateway route to a central
egress VPC (TGW is transitive, so central NAT works), or — where the landing
zone mandates it — a central proxy, via `HTTPS_PROXY_URL`. See
[VPC endpoints](#vpc-endpoints).

```bash
cp scripts/deploy.env.example scripts/deploy.env   # fill in VPC, cert, Okta values

# 1. Certificate (one-time; enterprise CA signs the corporate CNAME)
./scripts/import-enterprise-cert.sh csr claude-gateway.example.com
#    ... CA signs the CSR ...
./scripts/import-enterprise-cert.sh import claude-gateway.example.com leaf.pem key.pem chain.pem

# 2. Image (on a machine with egress + Docker)
./client/mirror-claude-release.sh 2.1.207
cp mirror/2.1.207/claude docker/claude
./scripts/build-and-push-image.sh

# 3. Stacks
./scripts/deploy-database.sh
./scripts/deploy-gateway.sh

# 4. Finish: Okta secret, corporate DNS CNAME, Zscaler bypass, then verify
./scripts/set-okta-secret.sh
./scripts/verify-gateway.sh
```

## Windows client rollout (offline)

On a machine with egress, mirror a pinned release and its manifest with
`client/mirror-claude-release.sh <version>` (downloads, checksum-verifies, and
GPG-verifies when the signing key is supplied), and stage `claude.exe`
(win32-x64) plus `CHECKSUMS.txt` on your file share. Then per laptop — **no
admin rights required** — or via Intune/SCCM:

```powershell
powershell -ExecutionPolicy Bypass -File .\client\Install-ClaudeCode.ps1 `
    -BinaryPath \\fileserver\software\claude\2.1.207\claude.exe `
    -Sha256 <platforms.win32-x64.checksum from manifest.json> `
    -GatewayUrl https://claude-gateway.example.com `
    -DisableUpdates
```

The script installs to `%USERPROFILE%\.local\bin\claude.exe` (the same path
the native installer manages), adds that directory to the user PATH, and
verifies the Anthropic Authenticode signature and your supplied SHA-256 — all
at user scope. Managed settings (`forceLoginMethod`, `forceLoginGatewayUrl`,
`requiredMinimumVersion` — 2.1.195 floor, the gateway minimum — and the update
lockdown: `DISABLE_UPDATES=1`, which blocks all update paths including manual
`claude update`/`claude install`, plus `DISABLE_AUTOUPDATER=1` as defense in
depth) are written to whichever managed-settings source the run can reach:

- **Elevated / MDM push** → `%ProgramData%\ClaudeCode\managed-settings.json` —
  tamper-resistant; use this for fleet enforcement.
- **Non-admin run** → `HKCU\SOFTWARE\Policies\ClaudeCode` (`Settings` REG_SZ,
  single-line JSON) — a per-user managed-settings source Claude Code honors
  without elevation. It's user-writable, so it configures rather than
  enforces; these keys are managed-only and would **not** work from a plain
  user `settings.json`.

Ensure your enterprise root CA is in the Windows certificate store (on
domain-joined machines it normally already is, via GPO — no admin needed at
install time) so the ALB cert validates.

Developer experience after install: new terminal → `claude` → `/login` →
**Cloud gateway** (URL pre-filled) → Okta SSO → compare the fingerprint
prompt against the published value.

## Model configuration

Developers get a **two-model menu** in Claude Code — Opus and Sonnet — each a
pair of parameters (client-facing ID → GovCloud inference profile):

| Menu ID (`*_MODEL_ID`) | Bedrock profile (`*_BEDROCK_MODEL_ID`) |
|---|---|
| `claude-opus-4-8` | `us-gov.anthropic.claude-opus-4-8` |
| `claude-sonnet-4-5` | `us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0` |

These defaults are the newest of each tier available in GovCloud: Sonnet 4.6
and Sonnet 5 are **not** offered in GovCloud regions, so Sonnet 4.5 (FedRAMP
High / IL4-5 authorized) is the Sonnet entry. Note the ID-format difference —
Opus 4.8 uses the new un-dated format while Sonnet 4.5 keeps the dated
`-v1:0` suffix. `us-gov-west-1` also supports in-region invocation. Confirm
what your account sees before first deploy:

```bash
aws bedrock list-inference-profiles --region us-gov-west-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'anthropic')].inferenceProfileId"
```

The task-role and VPC-endpoint IAM policies already cover `anthropic.*`
foundation models and `us-gov.anthropic.*` inference profiles, so switching
or adding models is a parameter change only.

## VPC endpoints

What the gateway's network path needs, and which endpoints the stack can
provide:

| Dependency | How it's reached |
|---|---|
| Bedrock inference | `bedrock-runtime` interface endpoint (`CREATE_BEDROCK_ENDPOINT=true`, the default) — inference never touches a NAT/IGW path, and the endpoint policy is the second guardrail on what can be invoked |
| RDS Postgres | No endpoint needed — the DB is in-VPC; traffic is SG-to-SG and never leaves |
| ECR image pull, CloudWatch Logs, Secrets Manager (Fargate's own dependencies) | Via NAT by default. For **fully-private subnets with no NAT**, set `CREATE_SUPPORTING_ENDPOINTS=true` to add `ecr.api`, `ecr.dkr`, `logs`, and `secretsmanager` interface endpoints |
| S3 (ECR image layers are served from S3) | S3 **gateway** endpoint, created whenever `PRIVATE_ROUTE_TABLE_IDS` is set. Gateway endpoints are free — recommended in **every** VPC (keeps image pulls off NAT data-processing charges), and mandatory for pulls to work at all without NAT |
| Okta OIDC | **Public SaaS — no VPC endpoint exists.** Via NAT where available; otherwise set `HTTPS_PROXY_URL` — the task gets `HTTP_PROXY`/`HTTPS_PROXY` plus a `NO_PROXY` covering `.amazonaws.com`, so AWS API calls (including Bedrock's private-DNS endpoint) and the database stay direct and never touch the proxy |

Interface endpoints bill per-AZ-hour, which is why the supporting set is
opt-in — VPCs with NAT don't need them. The free S3 gateway endpoint has no
such trade-off, hence its own switch.

**Landing-zone (Transit Gateway) profile** — no NAT in the workload VPC;
egress rides the TGW to a central egress VPC. Because TGW is transitive,
internet-bound traffic (Okta) works via the central NAT with no proxy
settings; set `HTTPS_PROXY_URL` only if the landing zone mandates a proxy
instead. Keep AWS-service traffic local regardless — it's cheaper than
hairpinning through the TGW (data-processing charges both ways) and keeps
Bedrock on the endpoint policy guardrail:

```bash
CREATE_BEDROCK_ENDPOINT="true"
CREATE_SUPPORTING_ENDPOINTS="true"         # local ECR/logs/Secrets Manager endpoints
PRIVATE_ROUTE_TABLE_IDS="rtb-aaa,rtb-bbb"  # free S3 gateway endpoint (ECR layers)
HTTPS_PROXY_URL=""                         # only if a central proxy is mandated
```

Two landing-zone checks: if shared services already provide **centralized
interface endpoints** (a common landing-zone pattern — endpoints in a shared
VPC with private hosted zones associated to spoke VPCs), leave
`CREATE_SUPPORTING_ENDPOINTS=false` and use those — creating local endpoints
with private DNS fails when a PHZ for the same service domain is already
associated. The S3 **gateway** endpoint is the exception: it cannot be
centralized over TGW, so create it locally in every case. And confirm TGW
routes/SGs allow developer ranges to reach the ALB on 443
(`CLIENT_INGRESS_CIDR`).

## Design decisions

These were settled during the original build-out. Each answers a "why not the
obvious alternative?" question — revisit only with a concrete reason.

| Area | Decision and rationale |
|---|---|
| Inference | Bedrock `us-gov-west-1`, `us-gov.anthropic.*` inference profiles, reached via a `bedrock-runtime` interface VPC endpoint — no NAT/IGW path needed. |
| Gateway | Claude apps gateway (built into the `claude` binary ≥ 2.1.195) on ECS Fargate, plain HTTP :8080 behind an **internal, IPv4-only** ALB. Dual-stack is off deliberately: internal dual-stack ALBs publish public-range AAAA records, which fails Claude Code's `/login` private-network check. |
| TLS | Enterprise-CA-signed certificate, SAN = the corporate CNAME, imported into ACM. Public certs are impossible for `*.elb.amazonaws.com`, and the corporate name survives ALB recreation. Imported certs do **not** auto-renew — alarm on expiry; rotation re-triggers the client fingerprint prompt, so publish the new fingerprint first. |
| DNS | Corporate-DNS CNAME → the ALB's default DNS name; resolves to private IPs, passing the `/login` check. No Route 53 private hosted zone required. |
| Zscaler | The gateway FQDN is bypassed: ZIA SSL-inspection exemption + app bypass (TLS inspection breaks certificate fingerprint pinning; public proxy egress IPs fail `/login`), or a ZPA app segment (ZPA's synthetic CGNAT answers pass the check and ZPA doesn't intercept TLS). Add the FQDN to `NO_PROXY` on laptops if a PAC/explicit proxy is in use. |
| IdP | Okta OIDC; a custom authorization server is preferred, and `userinfo_fallback: true` is set so the org server also works. Redirect URI is `https://<GatewayFqdn>/oauth/callback`. |
| Store | RDS PostgreSQL 16 with `rds.force_ssl`, an RDS-managed master secret, and SG-to-SG access only. Multi-AZ is on by default because a lost store loses spend tracking and caps, not just re-logins. |
| Client install | Fully offline: pinned binary mirrored from Anthropic's release bucket and verified before distribution; managed settings force gateway login, pin a minimum version, and block **all** update paths (`DISABLE_UPDATES=1`). Works for non-admin users (per-user install + HKCU managed-policy source). |
| Guardrails | IAM task role **and** VPC-endpoint policy are independently scoped to `anthropic.*` foundation models / `us-gov.anthropic.*` inference profiles — two separate controls on what the org credential can invoke. |

### Gotchas — do not re-litigate

- **The ALB must stay IPv4-only** (see above). Deletion protection is enabled
  because the ALB's default DNS name changes on recreation; the cert, CNAME,
  and Okta redirect URI all reference the corporate FQDN precisely to absorb
  that event.
- **There is no service-token flow.** CI/CD authenticates to Bedrock directly
  with IAM — it does not go through the gateway.
- **The target group health-checks `/healthz` (liveness) deliberately**, so
  signed-in developers keep working through a Postgres outage. Switch to
  `/readyz` only if you'd rather pull replicas when the store is down.
- **WebSearch is disabled on gateway sessions by design** (no public egress
  from the inference path).

### Operational notes

- **JWT secret rotation** (secret `<NamePrefix>/jwt-secret`) follows the
  prepend → roll → remove pattern: prepend the new value, force a new ECS
  deployment, then remove the old value.
- **Gateway config changes deploy via a stack update** — the rendered
  `gateway.yaml` is part of the task definition, so ECS rolls the service
  automatically; no manual restart needed.
- **Certificate renewal is in-place**: re-run `import-enterprise-cert.sh
  import ... --certificate-arn <arn>` and the ALB listener picks up the new
  cert with no stack update. Publish the new SHA-256 fingerprint to
  developers *before* rotating (first-connect pinning re-prompts). The stack
  includes a CloudWatch alarm on the certificate's `DaysToExpiry`
  (`CERT_EXPIRY_ALARM_DAYS`, default 30; wire `ALARM_SNS_TOPIC_ARN` for
  notifications).
- **ALB access logs** are always on, delivered to a stack-created S3 bucket
  (`AlbLogsBucketName` output; SSE-S3, public access blocked, auto-expiry
  after `ALB_LOG_RETENTION_DAYS`, default 90). The bucket is retained on
  stack deletion.

### Hardening roadmap (post-deploy)

Not yet wired in, planned as policy firms up: per-Okta-group
`managed.policies` (model allowlists, locked CLI settings) and gateway spend
limits in the `gateway.yaml` block of `02-gateway.yaml` (config deploys via a
stack update — ECS rolls the service automatically); OTLP telemetry to your
own collector; and egress blocking from developer subnets to Bedrock /
`api.anthropic.com` (pair with `skipWebFetchPreflight: true`) so the gateway
is the only inference path.

### Key references

- <https://code.claude.com/docs/en/claude-apps-gateway> (plus the `-config`,
  `-deploy`, and `-spend-limits` pages)
- <https://code.claude.com/docs/en/network-config> ·
  <https://code.claude.com/docs/en/setup> ·
  <https://code.claude.com/docs/en/admin-setup>
- <https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-bedrock.html>

## Per-client checklist

Every deployment fills in `scripts/deploy.env` (FQDN, Okta, network, model)
and then works through:

1. Register the Okta OIDC web app (redirect URI
   `https://<GatewayFqdn>/oauth/callback`); after stack deploy, set the real
   client secret with `scripts/set-okta-secret.sh`.
2. Verify the GovCloud inference-profile IDs against the Bedrock console
   (`aws bedrock list-inference-profiles --region us-gov-west-1`).
3. Issue and import the enterprise-CA certificate
   (`scripts/import-enterprise-cert.sh`). The stack alarms when
   `DaysToExpiry` ≤ 30 (imported ACM certs do not auto-renew) — set
   `ALARM_SNS_TOPIC_ARN` so someone actually hears it.
4. Create the corporate DNS CNAME from the `AlbDnsName` stack output —
   `claude-gateway.example.com.  CNAME  internal-claude-gw-alb-XXXX.us-gov-west-1.elb.amazonaws.com.`
   — then confirm the chain resolves to only private A records and no AAAA,
   and that the OAuth endpoints answer (`scripts/verify-gateway.sh` checks
   all of this).
5. Configure the Zscaler bypass (ZIA exemption or ZPA app segment) for the
   gateway FQDN.
6. Dry-run `Install-ClaudeCode.ps1` on a test laptop **as a non-admin user**;
   confirm with `claude doctor` that the managed settings are picked up.
7. Publish the certificate's SHA-256 fingerprint to developers (first-connect
   pinning prompt).
