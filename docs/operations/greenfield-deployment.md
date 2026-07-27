# Greenfield deployment runbook

The reusable, org-agnostic path from **an empty VPC in a GovCloud
landing-zone spoke account** to **a Windows client installed and
authenticated end to end** against the Claude apps gateway. It differs from
its two siblings: [`test-run-runbook.md`](test-run-runbook.md) is the
**dated log of the first live run** (test-account shortcuts, self-signed
certs, migration notes for pre-sidecar deployments — consult it when a step
here breaks, its §10 is the troubleshooting index), and
[`om-runbooks.md`](om-runbooks.md) covers **steady state after** this runbook
finishes (rotations, updates, alarms, teardown). Deep explanations are
linked, not duplicated — this document is the spine: every command in order,
with the org-prerequisite lead times sequenced first because they, not AWS,
set the calendar.

Two hosts (`.claude/rules/offline-build.md`): an **egress host** (internet,
no AWS needed) runs the `scripts/mirror/` tools, and the **build/deploy
machine** (Docker + AWS service endpoints only, **no internet**) runs
everything else against the transferred `mirror/` directory. The split
lands in Phase 4; if the two hosts keep separate checkouts, remember
`deploy.env` is persisted locally on whichever host ran a script — the
build/deploy machine's copy is the one that accumulates `KMS_KEY_ARN`,
`IMAGE_URI`, `DBADMIN_IMAGE`, `GRAFANA_IMAGE`, `COLLECTOR_IMAGE`, and
`CERTIFICATE_ARN`.

Shell convention: command examples expand variables like `$GATEWAY_FQDN` and
`$CLAUDE_VERSION` in **your** shell — the scripts source `deploy.env`
internally, but argument expansion does not, so load it first:
`set -a; . scripts/deploy.env; set +a`.

Legend: ☐ = do it · 🔎 = checkpoint, confirm before moving on ·
**[NEEDS TEST-RUN CONFIRMATION]** = per the repo honesty rule, a step whose
behavior is script/doc-verified but has never been exercised live (the
tags track the fix log in
[`../ato/security-review-2026-07.md`](../ato/security-review-2026-07.md) and
the Status block in `CLAUDE.md` — keep them in sync as your run proves
things).

Phases: **0** org prerequisites → **1** account & workstation prep →
**2** certificate → **3** database (01) → **4** mirror + images →
**5** gateway (02) → **6** DNS + Zscaler activation → **7** verify + Okta
secret → **8** observability (03) + 02 re-run → **9** optional portal (04) →
**10** client rollout → **11** end-to-end validation.

---

## Phase 0 — Org prerequisites: send these FIRST

Three request emails go to three different teams, and their turnaround —
not the AWS work — dominates the schedule. All three are actionable on day
one; send them before touching AWS. Templates (fill placeholders from
`scripts/deploy.env`):

- ☐ **Networking / PKI / Zscaler** —
  [`../requests/networking-request-email.md`](../requests/networking-request-email.md).
  Three asks in one mail: the **enterprise-CA certificate** (CSR signed
  against `GATEWAY_FQDN` — actionable immediately, no AWS dependency), the
  **internal DNS CNAME** (the *name* can be reserved now; the *target value*
  is the `AlbDnsName` stack output that exists only after Phase 5 — tell the
  DNS team a same-day follow-up carries it), and **Zscaler access**, which
  has **two independent halves**:
  1. the **client-side** entry for `GATEWAY_FQDN` (ZPA app segment on TCP
     443, or ZIA SSL-inspection exemption + app bypass) — TLS inspection
     here breaks the client's certificate fingerprint pin, and
     `verify-gateway.sh` hard-fails if it sees a Zscaler-issued cert; and
  2. the **server-side egress** ALLOW **plus SSL-inspection exemption** for
     the **Okta issuer FQDN** on the ZIA location that carries the workload
     VPC's central egress. This is server-originated traffic with no Zscaler
     user identity, so default policy both intercepts and blocks it. **This
     exact item blocked the first live run**: the gateway boot failed at
     OIDC discovery with a 403 until it landed. Do not treat it as optional
     or as covered by half 1 — it is a separate rule on a separate path.
- ☐ **Okta administrator** —
  [`../requests/okta-request-email.md`](../requests/okta-request-email.md).
  An OIDC **Web** app (confidential — it must have a client secret) on the
  **org authorization server**, with **both the Authorization Code AND
  Refresh Token grants** (without Refresh Token, `offline_access` is
  silently ignored and every developer is bounced through browser SSO at the
  session TTL — as short as 1 hour), all needed redirect URIs
  (`/oauth/callback`, `/grafana/login/generic_oauth`, and
  `/portal/oauth/callback` if deploying the portal), a **groups claim**
  configured on the app (the `groups` scope alone is not enough; without the
  claim, Grafana role mapping and portal access deny everyone, and per-group
  spend caps cannot resolve), and the groups themselves
  (`GRAFANA_ADMIN_GROUP`, `ACCESS_GROUP`) with your test user in them.
- ☐ **AD / GPO (or MDM) team** —
  [`../requests/ad-request-email.md`](../requests/ad-request-email.md).
  One machine-policy value (GPP Registry item to
  `HKLM\SOFTWARE\Policies\ClaudeCode`, recommended) carrying
  `forceLoginMethod: "gateway"` + `forceLoginGatewayUrl`. Without it the
  "Cloud gateway" login **does not exist** on any client — it is honored
  only from a managed source, by Anthropic's anti-phishing design. The
  `GATEWAY_FQDN` value is static and known now, so this GPO can be built and
  staged immediately; it only has to be *linked/enabled* by Phase 10.

Dependency map (the chicken-and-egg items, explicit):

| Item | Ready when | Blocks |
|---|---|---|
| Signed certificate | CA turnaround only | Phase 5 (ALB listener) — actually Phase 2 import |
| DNS CNAME (name) | reservable now | — |
| DNS CNAME (target) | **after Phase 5** (`AlbDnsName`) | Phase 6/7 (verify, login) |
| Zscaler client-side (gateway FQDN) | Zscaler turnaround | Phase 7+ (any client reaching the ALB) |
| Zscaler server-side (Okta issuer ALLOW + exemption) | Zscaler turnaround | **Phase 5** — gateway boot fails at OIDC discovery (403) without it |
| Okta app (ID + secret + groups claim) | Okta turnaround | Phase 5 (boot needs a resolvable issuer + client ID), Phase 7 (secret) |
| GPO managed login setting | AD turnaround | Phase 10 (no gateway login option without it) |

☐ **What you need back before Phase 1 ends**: the **App Connector source
CIDR(s)** (→ `CLIENT_INGRESS_CIDR` — the ALB SG locks to exactly these),
the Okta **client ID** (→ `OKTA_CLIENT_ID`) and **client secret** (held for
the `set-*-secret.sh` prompts — never written to `deploy.env`), the issuer
(→ `OKTA_ISSUER`), confirmed group names, and the cert validity period +
renewal owner.

---

## Phase 1 — Account & workstation prep

**Tooling & identity**
- ☐ AWS credentials for the target account
  (`aws sts get-caller-identity --region us-gov-west-1`). The deploy
  identity must be able to create IAM roles, KMS keys, Lambda, RDS, ECS,
  ELBv2, Secrets Manager, ECR — and **set stack policies** (the deploy
  scripts call `set-stack-policy`).
- ☐ `docker`, `jq`, `openssl`, and AWS CLI v2 on the host.

**Bedrock model access**
- ☐ Enable Claude model access in the account, then confirm the exact
  GovCloud inference-profile IDs the defaults assume
  (`us-gov.anthropic.claude-opus-4-8` — un-dated ID — and
  `us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0`):
  ```bash
  aws bedrock list-inference-profiles --region us-gov-west-1 \
    --query "inferenceProfileSummaries[?contains(inferenceProfileId,'anthropic')].inferenceProfileId"
  ```
  If they differ, set `OPUS_BEDROCK_MODEL_ID` / `SONNET_BEDROCK_MODEL_ID`
  in `deploy.env`. Sonnet 4.6 / Sonnet 5 are **not** in GovCloud — do not
  "upgrade" the defaults without checking this output.

**deploy.env**
- ☐ `cp scripts/deploy.env.example scripts/deploy.env` and fill (the
  example's comments are the authoritative per-variable documentation):
  `VPC_ID`, `VPC_CIDR`, `PRIVATE_SUBNET_IDS` (≥2 AZs),
  `CLIENT_INGRESS_CIDR` (**narrow it** to the App Connector / VPN CIDRs
  from Phase 0 — `10.0.0.0/8` is an auditor finding and misses on-prem
  connectors in 172.16/12), `GATEWAY_FQDN`, `OKTA_ISSUER` (must start
  `https://`), `OKTA_CLIENT_ID`, `ALLOWED_EMAIL_DOMAINS`,
  `GRAFANA_OKTA_CLIENT_ID` (= `OKTA_CLIENT_ID` if reusing the app),
  `GRAFANA_ADMIN_GROUP`.
- ☐ **Landing-zone spoke decision** (this runbook's target profile: no NAT,
  TGW to central egress): set `CREATE_SUPPORTING_ENDPOINTS="true"`,
  `PRIVATE_ROUTE_TABLE_IDS="rtb-..."` (free S3 gateway endpoint — required
  for image pulls without NAT), keep `CREATE_BEDROCK_ENDPOINT="true"`, and
  plan `CREATE_AMP_ENDPOINT="true"` for Phase 8. **Exception**: any service
  the landing zone already centralizes via shared endpoints/PHZs must be
  skipped locally or the stack fails on a conflicting DNS name — check with
  `aws route53 list-hosted-zones-by-vpc` and use the per-service
  `CREATE_*_ENDPOINT` switches (see the README "VPC endpoints" section and
  the `deploy.env.example` comments). If the landing zone mandates a
  central proxy for internet egress, set `HTTPS_PROXY_URL`.
- ☐ Leave `IMAGE_URI`, `DBADMIN_IMAGE`, `GRAFANA_IMAGE`, `COLLECTOR_IMAGE`,
  `PORTAL_IMAGE`, `CERTIFICATE_ARN`, `KMS_KEY_ARN`, and the three
  `OBSERVABILITY_*` vars **empty** — the scripts persist them as they run;
  there are no copy-paste steps.

**GPG decision for the release mirror** (it fails closed)
- ☐ Either put Anthropic's release-signing public key on the host and
  `export ANTHROPIC_GPG_KEY=/path/to/key`, **or** deliberately accept
  TLS-only trust with `export ALLOW_UNVERIFIED_MANIFEST=1`. Without one of
  these, the very first mirror step in Phase 4 stops. Record the choice —
  it is a named, auditable override, not a default.

**Endpoint-policy pre-check** (no-NAT profile only)
- ☐ Confirm the `logs` endpoint supports policies in-region (the `ecs`
  endpoint deliberately carries none for this reason):
  ```bash
  aws ec2 describe-vpc-endpoint-services --region us-gov-west-1 \
    --service-names com.amazonaws.us-gov-west-1.logs \
    --query 'ServiceDetails[].VpcEndpointPolicySupported'
  ```

---

## Phase 2 — Certificate

```bash
./scripts/import-enterprise-cert.sh csr "$GATEWAY_FQDN"     # EC P-256 key + CSR (append rsa2048 if the CA requires RSA)
#   → hand the CSR to the enterprise CA (serverAuth EKU); collect leaf + chain
./scripts/import-enterprise-cert.sh import "$GATEWAY_FQDN" leaf.pem "$GATEWAY_FQDN.key.pem" chain.pem
```
🔎 Prints `CertificateArn:` (persisted as `CERTIFICATE_ARN`) and the
**SHA-256 fingerprint — save it**; it is what developers confirm at first
login (Phase 10) and must be published before rollout. Imported ACM certs do
**not** auto-renew; the stack alarms at `CERT_EXPIRY_ALARM_DAYS` (wire
`ALARM_SNS_TOPIC_ARN`), and renewal is
[`om-runbooks.md`](om-runbooks.md) runbook 1.

---

## Phase 3 — Database stack (01) — FIRST

01 comes first because it creates the KMS CMK and persists `KMS_KEY_ARN`,
so the ECR repos created in Phase 4 are born CMK-encrypted (ECR encryption
is creation-time-only). The RDS storage key is likewise a **day-one
decision** — changing it later is a teardown + restore, not an update.

```bash
./scripts/deploy-database.sh
```
🔎 Stack `CREATE_COMPLETE` (Multi-AZ RDS takes ~10–15 min); the
`KmsKeyArnResolved` output; the "Locking the database against
replacement/deletion (stack policy)" line; `KMS_KEY_ARN` now in
`deploy.env`. If it fails with "version 16.x does not exist", pin an
available minor via `DB_ENGINE_VERSION` (query in
[`test-run-runbook.md`](test-run-runbook.md) §2).

---

## Phase 4 — Mirror the release + build and push all images

Two hosts (`.claude/rules/offline-build.md`): step 4a runs on the **egress
host** (internet, no AWS needed except for `mirror-collector.sh` — see its
note); steps 4b–4f run on the **build machine** (Docker + AWS only, no
internet) after you copy `mirror/` over. The GPG decision from Phase 1 gates
the release mirror. Needs `KMS_KEY_ARN` from Phase 3.

```bash
# 4a. EGRESS HOST — verify + stage every external artifact into mirror/
./scripts/mirror/mirror-claude-release.sh "$CLAUDE_VERSION"   # verifies sha256 + (GPG) manifest; fails closed
./scripts/mirror/mirror-grafana-plugin.sh                     # AMP datasource plugin, sha256-pinned (grafana-plugin.pin)
./scripts/mirror/mirror-rds-ca-bundle.sh                      # RDS CA trust bundle (baked into gateway + db-admin images)
# ---- copy the mirror/ directory to the build machine ----

# 4b. BUILD MACHINE — gateway image (stages claude from mirror/, re-verified
#     against the mirror's CHECKSUMS.txt)
./scripts/build-and-push-image.sh                             # persists IMAGE_URI

# 4c. DB-admin Lambda image (bootstrap + rotation)
./scripts/build-and-push-dbadmin.sh                           # persists DBADMIN_IMAGE

# 4d. Grafana image (bakes in the mirrored AMP plugin, re-verified
#     against scripts/mirror/grafana-plugin.pin)
./scripts/build-and-push-grafana.sh                           # persists GRAFANA_IMAGE

# 4e. ADOT collector — mirror the pinned upstream image into ECR. Needs BOTH
#     public.ecr.aws reach and AWS creds: run it wherever both are available
#     (the egress host with AWS creds, or the build machine if your landing
#     zone lets it reach public.ecr.aws).
ADOT_VERSION=v0.49.0 ./scripts/mirror/mirror-collector.sh     # persists digest-pinned COLLECTOR_IMAGE

# 4f. Download-portal image (only if deploying Phase 9)
./scripts/build-and-push-portal.sh                            # persists PORTAL_IMAGE
```
🔎 `grep -E 'IMAGE_URI|DBADMIN_IMAGE|GRAFANA_IMAGE|COLLECTOR_IMAGE' scripts/deploy.env`
— all set by the scripts, none by hand. The mirror output also contains
`claude.exe` + `CHECKSUMS.txt` for the Windows rollout (Phase 10) — stage
`mirror/$CLAUDE_VERSION/` on the internal file share now. Pin the ADOT
version currently proven with this repo (v0.49.0 at time of writing —
check `CLAUDE.md` Status). Base images must come from **your registry
mirror** (`GATEWAY_BASE_IMAGE`, `GRAFANA_BASE_IMAGE`, `LAMBDA_BASE_IMAGE`,
`PORTAL_BASE_IMAGE` if deploying 04) — the build machine cannot reach
Docker Hub, and the upstream defaults exist for dev convenience only; the
builds need no package-repo access at all (README, "Controlled-network
image builds").

---

## Phase 5 — Gateway stack (02)

**Gate: the server-side Zscaler exemption for the Okta issuer (Phase 0,
half 2) must be live** — without it the gateway boot fails at OIDC
discovery with a 403 and the service never goes healthy. Probe first from
an in-VPC host:
```bash
curl -sv "https://<OKTA_ISSUER_HOST>/.well-known/openid-configuration"   # expect 200 JSON
```
(A Zscaler HTML block page = policy ALLOW missing; a TLS error = the
inspection exemption missing — interim fallback is `EXTRA_CA_CERT_PATH`,
see [`../requests/networking-request-email.md`](../requests/networking-request-email.md) §3.)

```bash
./scripts/deploy-gateway.sh
```
Creates the ALB, ECS gateway, IAM, secrets, VPC endpoints, the DB-bootstrap
custom resource, the app-secret rotation schedule, and the spend-cap
`admin:` block + admin keys. Watch three live-only validations:

🔎 **DB bootstrap** — `Custom::DbAppUserBootstrap` reaches
`CREATE_COMPLETE` (on failure it times out in ~5 min; tail
`/aws/lambda/<prefix>-db-bootstrap`).

🔎 **Target-group health** — targets go `healthy` on the **HTTPS** health
check:
```bash
aws ecs wait services-stable --region "$AWS_REGION" --cluster "${NAME_PREFIX}-cluster" --services "${NAME_PREFIX}-gateway"
aws elbv2 describe-target-health --region "$AWS_REGION" --target-group-arn <tg-arn>
```

🔎 **First app-secret rotation** (fires automatically, async — the stack is
green regardless, so verify, don't assume):
```bash
aws secretsmanager get-secret-value --region "$AWS_REGION" \
  --secret-id "${NAME_PREFIX}/db-app-user" --query SecretString --output text \
  | jq -r .username        # expect gateway_app_clone after the first rotation
```
**[NEEDS TEST-RUN CONFIRMATION]** — rotation has not yet been proven live
anywhere; if it did not flip, tail `/aws/lambda/<prefix>-db-rotation` and
check the `<prefix>-db-rotation-errors` alarm.

🔎 The "Locking the ALB against replacement/deletion" stack-policy line ran.

Note: `deploy-gateway.sh` runs with `--disable-rollback` by default — a
failed deploy keeps healthy resources so you fix and re-run
([`test-run-runbook.md`](test-run-runbook.md) §10 for recovery patterns).

---

## Phase 6 — DNS + Zscaler activation

- ☐ Send the DNS team the CNAME **target** (the same-day follow-up promised
  in Phase 0): `GATEWAY_FQDN CNAME <AlbDnsName>` — get it with
  `./scripts/stack-outputs.sh`. The ELB name is a public record returning
  private IPs; no split-horizon zone needed, but confirm the resolver does
  not strip RFC1918 answers (DNS-rebinding protection — asked in the
  networking email).
- ☐ Confirm the **client-side** Zscaler entry is active (ZPA app segment or
  ZIA exemption + bypass for `GATEWAY_FQDN`), and that App Connectors can
  resolve the corporate CNAME (README, "ZPA & landing-zone prerequisites").

---

## Phase 7 — Verify the gateway + set the Okta secret

```bash
./scripts/verify-gateway.sh
```
🔎 DNS (private A records only, no AAAA), TLS chain + fingerprint
(cross-checked against ACM), OAuth endpoints answering. Behind ZPA the
laptop sees synthetic CGNAT answers — run the DNS assertions from an App
Connector's resolution context; the script says so when it detects them. It
hard-fails on a Zscaler-issued cert (client-side bypass not active).

- ☐ Set the gateway's Okta client secret (hidden prompt; rolls the
  service): `./scripts/set-okta-secret.sh`

---

## Phase 8 — Observability (03) + Grafana secret + 02 re-run

Optional stack, strongly recommended (spend visibility). Order within the
phase is load-bearing: 03 emits the AMP parameters that the 02 **re-run**
consumes to attach the ADOT collector as a **localhost sidecar** in the
gateway task — there is no standalone collector service.

```bash
./scripts/deploy-observability.sh          # AMP + Grafana; persists OBSERVABILITY_AMP_ENDPOINT / _WORKSPACE_ARN / _ACTIVITY_LOG_GROUP
./scripts/set-grafana-oidc-secret.sh       # paste the (same or dedicated) client secret; rolls Grafana
# set TELEMETRY_FAIL_CLOSED="false" in scripts/deploy.env (fail-OPEN first — see below),
# then re-run to attach the sidecar. An env prefix on the command does NOT work:
# common.sh sources deploy.env after it and the file's value wins.
./scripts/deploy-gateway.sh
```
🔎 03 `CREATE_COMPLETE`; the `GrafanaOidcRedirectUri` output matches the
URI registered in Okta; the three `OBSERVABILITY_*` vars persisted.

🔎 After the 02 re-run, the gateway task runs **two containers**
(`aws ecs describe-tasks ... --query 'tasks[].containers[].name'` shows the
gateway plus `otel-collector`), and collector log streams appear under the
`otel` prefix.

- ☐ **First-boot posture, then fail closed.** Deploy the first
  telemetry-enabled re-run with `TELEMETRY_FAIL_CLOSED="false"` set **in
  `scripts/deploy.env`** (the example ships it `"true"`; edit the file — a
  `VAR=... ./scripts/...` command prefix is silently overridden when
  `common.sh` sources the file): with fail-closed on, a misconfigured
  collector health check hangs the rollout indefinitely at `services-stable`
  with only a generic "task not healthy" (the single highest-risk item on
  this path — details in [`test-run-runbook.md`](test-run-runbook.md) §8).
  Once the `otel-collector` container reports HEALTHY, flip
  `TELEMETRY_FAIL_CLOSED="true"` in `deploy.env` and re-run
  `./scripts/deploy-gateway.sh` — the default, SSP-recorded AU-5 posture
  (a failed collector then stops the task rather than serving unmonitored
  traffic — stop-on-broken-config **[NEEDS TEST-RUN CONFIRMATION]**; the
  `${NAME_PREFIX}-missing-telemetry` alarm is the end-to-end backstop).
- ☐ The `missing-telemetry` alarm legitimately fires between the 03 deploy
  and the telemetry-enabled re-run — expect it to settle to OK once the
  sidecar's self-metrics heartbeat lands (proven live 2026-07-23).

---

## Phase 9 — Optional: installer download portal (04)

Independent of 03 — any time after Phase 5. Shares the ALB / FQDN / cert /
Zscaler entry (path-based at `/portal`): **no new DNS or Zscaler request**,
but it reaches the Okta issuer over the same server-side egress exemption
the gateway needs. Prereqs: `PORTAL_IMAGE` (Phase 4f), the
`/portal/oauth/callback` redirect URI, the **groups claim** on the Okta app
(without it the portal denies everyone), and `ACCESS_GROUP` populated.

```bash
./scripts/deploy-download-portal.sh                    # stack + CMK artifacts bucket (persists PORTAL_ARTIFACTS_BUCKET)
./scripts/publish-portal-release.sh "$CLAUDE_VERSION"  # re-verifies claude.exe against the manifest, uploads
./scripts/set-portal-oidc-secret.sh                    # paste the portal client secret; rolls the service
```
🔎 Target group healthy on the HTTPS `/portal/healthz` check;
`PortalOidcRedirectUri` matches Okta. The portal's code is test-suite-green
but the stack is **[NEEDS TEST-RUN CONFIRMATION]** end to end (live Okta
round-trip, real-size streamed download) — validation items in Phase 11.

---

## Phase 10 — Client rollout (Windows fleet)

The model (full detail: [`client-config.md`](client-config.md)): **no-admin
binary install + one required admin-delivered managed setting for login +
server-side policy push**. For a single test laptop instead of a fleet, use
the one-time elevated HKLM seed in [`test-run-runbook.md`](test-run-runbook.md)
§9 — this phase is the fleet path.

- ☐ **Managed login setting via GPO/MDM** — confirm the Phase-0 GPO
  ([`../requests/ad-request-email.md`](../requests/ad-request-email.md)) is
  now linked to the developer OU and applied
  (`gpupdate /force`; verify with `/status` inside `claude` — the managed
  source must be listed). Machine scope, Registry mechanism recommended.
  Without it there is **no gateway login option on any client**. Note the
  GPO's `forceRemoteSettingsRefresh: true` makes the CLI exit if it cannot
  fetch the gateway's managed settings — the gateway must be serving
  (Phase 7 green) before this lands on machines, or developers get a
  non-starting CLI. A GPO-delivered HKLM source being honored is
  **[NEEDS TEST-RUN CONFIRMATION]** (self-served HKLM is what the test
  laptop exercised).
- ☐ **Publish the certificate fingerprint** (from Phase 2) through a
  channel developers trust — they confirm it at first connect
  (trust-on-first-use pin). Ensure the enterprise root CA is in the Windows
  cert store (normally already there by GPO) — chain validation happens
  *before* the pin.
- ☐ **Distribute the installer** — either path:
  - **Portal** (Phase 9): developers browse `https://<GATEWAY_FQDN>/portal`
    → Okta SSO → pick Team + Cost Center → ZIP with a pre-baked
    `install.cmd` (`-GatewayUrl` / `-Sha256` / `-Team` / `-CostCenter` /
    `-DisableUpdates`).
  - **Direct** (file share): stage `claude.exe` + `CHECKSUMS.txt` from the
    Phase-4 mirror on the share (over ZPA the share needs its **own** app
    segment, TCP 445), then per laptop, **no admin**:
    ```powershell
    powershell -ExecutionPolicy Bypass -File .\client\Install-ClaudeCode.ps1 `
        -BinaryPath \\<fileserver>\<share>\claude\$env:CLAUDE_VERSION\claude.exe `
        -Sha256 <platforms.win32-x64.checksum from manifest.json> `
        -GatewayUrl https://<GATEWAY_FQDN> `
        -Team <team> -CostCenter <cc> -DisableUpdates
    ```
  Deploy in **user** context — the installer refuses SYSTEM runs. Machines
  provisioned by an *older* installer need stale managed settings cleared
  first ([`client-config.md`](client-config.md) §2.4).
- ☐ **One-time login**: new terminal → `claude` → the login screen is
  locked to gateway with the URL pre-filled (no picker, no typing) → press
  Enter → one-time **Okta SSO** in the browser (+MFA) → confirm the
  fingerprint against the published value → a prompt returns a Bedrock
  completion. The token persists with refresh — if developers are bounced
  through SSO hourly, the Okta app is missing the Refresh Token grant
  (Phase 0). Tell developers: sign out with `claude auth logout`, **never**
  `/logout` — `/logout` strands the client behind an unreachable-hosts
  preflight (recovery: [`om-runbooks.md`](om-runbooks.md) runbook 12).
- ☐ **Clean-profile first run**: verify a first-ever run on a profile that
  has never run `claude` — the onboarding connectivity preflight can block
  every new developer on a gateway-only egress path; the check and the
  rollout decision it forces are in [`test-run-runbook.md`](test-run-runbook.md)
  §9. **[NEEDS TEST-RUN CONFIRMATION]**

---

## Phase 11 — End-to-end validation checklist

The deployment is done when every box below is checked — until then it is
not production-ready. Where a check fails, start at
[`test-run-runbook.md`](test-run-runbook.md) §10 and the alarm runbook
([`om-runbooks.md`](om-runbooks.md) runbook 9).

- ☐ **Gateway health**: `./scripts/verify-gateway.sh` all green; both
  targets healthy; task authenticates to Postgres as `gateway_app*`, never
  the master user (check `/ecs/<prefix>` logs for the clean DB connect).
- ☐ **Developer login round-trip**: locked gateway login → Okta SSO →
  fingerprint match → Bedrock completion (proven live 2026-07-24 on the
  test deployment; re-prove on each new environment).
- ☐ **Model picker constrained**: in a logged-in session, `/model` lists
  **only** `OPUS_MODEL_ID` and `SONNET_MODEL_ID` — not Claude Code's
  built-in menu. Send a prompt on each; confirm background/small-fast tasks
  also succeed (the Haiku-override push). If the built-in menu appears, the
  gateway image predates the `GATEWAY_MANAGED_B64` stanza and ignores the
  env var silently — rebuild with a bumped tag
  ([`test-run-runbook.md`](test-run-runbook.md) §9).
  **[NEEDS TEST-RUN CONFIRMATION]**
- ☐ **Telemetry flowing**: after a few sessions, `claude_code_*` series
  appear in AMP and the Grafana cost/token panels populate. Confirm
  `otelcol_exporter_prometheusremotewrite_failed_translations` stays flat —
  a climbing counter with client activity is the delta-temporality trap
  (its fix — the pushed cumulative-temporality env var — is
  **deploy-confirmed**, but verify it took on this deployment). First stop
  when metrics are missing: `./scripts/diagnostics/diagnose-telemetry.sh`
  (walks the client → gateway → sidecar → AMP chain);
  `./scripts/diagnostics/amp-query.py` queries AMP directly.
- ☐ **Grafana Okta SSO**: `https://<GATEWAY_FQDN>/grafana` → "Sign in with
  Okta" → a `GRAFANA_ADMIN_GROUP` member lands as Admin and the usage
  dashboard renders; a user in no mapped group is denied (strict mapping).
  **[NEEDS TEST-RUN CONFIRMATION]**
- ☐ **Spend-cap smoke test**: set a low cap on the test user, confirm the
  429 with `SPEND_BLOCKED_MESSAGE` once exceeded, then clear it
  (needs `GATEWAY_CA_BUNDLE` set for the script's TLS):
  ```bash
  ./scripts/set-spend-limit.sh --scope user --id <okta-sub|email> --amount 1 --period daily
  ./scripts/set-spend-limit.sh --list
  ./scripts/set-spend-limit.sh --scope user --id <okta-sub|email> --clear
  ```
  Note the availability trade the stack enables
  (`enforcement.fail_closed_on_error`): a spend-store outage halts all
  inference — recovery is [`om-runbooks.md`](om-runbooks.md) runbook 10.
  **[NEEDS TEST-RUN CONFIRMATION]** (verified against a mirrored gateway +
  throwaway Postgres only.)
- ☐ **pgaudit**: the `/aws/rds/instance/${NAME_PREFIX}-store/postgresql`
  log group receives DDL/connection events.
- ☐ **Rotation proof**: `db-app-user` AWSCURRENT username flipped to
  `gateway_app_clone` (Phase 5 checkpoint), or flips on a manual
  `aws secretsmanager rotate-secret --secret-id "${NAME_PREFIX}/db-app-user"`,
  and the gateway service rolled afterward.
  **[NEEDS TEST-RUN CONFIRMATION]**
- ☐ **Alarms wired**: `ALARM_SNS_TOPIC_ARN` set and subscribed;
  `missing-telemetry` alarm OK (its OK→ALARM→OK cycle is cheap to test —
  stop the sidecar); cert-expiry and db-rotation-errors alarms exist.
- ☐ *(if Phase 9)* **Portal**: Okta login + real download works end to end;
  a non-`ACCESS_GROUP` user is denied **and** the denial plus successful
  downloads land in the `/claude/<prefix>/portal-audit` log group.
  **[NEEDS TEST-RUN CONFIRMATION]**
- ☐ *(if enabled)* **Activity archive**: with `FORWARD_ACTIVITY_LOGS=true`,
  events land in the CloudWatch window and the S3 archive. Treat the stream
  as highly sensitive. **[NEEDS TEST-RUN CONFIRMATION]**
- ☐ Update [`../ato/security-review-2026-07.md`](../ato/security-review-2026-07.md)
  and the `CLAUDE.md` Status block for anything this run newly proved (or
  disproved) — the honesty convention only works if runs report back.

---

## One-page command summary (landing-zone spoke, happy path)

```bash
# Phase 0: send the three request emails; wait for cert, Okta app, Zscaler halves, GPO
# Phase 1: fill scripts/deploy.env; enable Bedrock model access;
#          export ANTHROPIC_GPG_KEY=... (or ALLOW_UNVERIFIED_MANIFEST=1)
./scripts/import-enterprise-cert.sh csr "$GATEWAY_FQDN"
./scripts/import-enterprise-cert.sh import "$GATEWAY_FQDN" leaf.pem "$GATEWAY_FQDN.key.pem" chain.pem
./scripts/deploy-database.sh
# -- egress host --
./scripts/mirror/mirror-claude-release.sh "$CLAUDE_VERSION"
./scripts/mirror/mirror-grafana-plugin.sh
./scripts/mirror/mirror-rds-ca-bundle.sh
# -- copy mirror/ to the build machine; everything below runs there --
./scripts/build-and-push-image.sh
./scripts/build-and-push-dbadmin.sh
./scripts/build-and-push-grafana.sh
ADOT_VERSION=v0.49.0 ./scripts/mirror/mirror-collector.sh   # needs public.ecr.aws + AWS creds (see Phase 4e)
./scripts/deploy-gateway.sh                      # gate: Okta-issuer egress exemption live
#   ... DNS CNAME target to the DNS team; confirm client-side Zscaler entry ...
./scripts/verify-gateway.sh
./scripts/set-okta-secret.sh
./scripts/deploy-observability.sh
./scripts/set-grafana-oidc-secret.sh
#   ... set TELEMETRY_FAIL_CLOSED="false" in deploy.env (env prefix won't stick) ...
./scripts/deploy-gateway.sh                      # attach sidecar fail-open
#   ... collector HEALTHY? flip TELEMETRY_FAIL_CLOSED="true" in deploy.env ...
./scripts/deploy-gateway.sh
#   ... optional portal ...
./scripts/build-and-push-portal.sh
./scripts/deploy-download-portal.sh
./scripts/publish-portal-release.sh "$CLAUDE_VERSION"
./scripts/set-portal-oidc-secret.sh
#   ... Phase 10 client rollout; Phase 11 validation checklist ...
```

Teardown (should you need to start over) is the reverse — 04 and 03, then
02, then 01: [`om-runbooks.md`](om-runbooks.md) runbook 13, plus the
test-account caveats in [`test-run-runbook.md`](test-run-runbook.md) §10.
