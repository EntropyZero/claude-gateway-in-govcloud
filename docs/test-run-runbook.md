# Test-run runbook — 2026-07-16

First end-to-end deploy of the full stack into a **test account** (AWS
GovCloud `us-gov-west-1`). Goal: prove the whole chain stands up and the
things only a live run can validate actually work — the DB bootstrap, the
first app-secret rotation, the HTTPS target groups going healthy, and the
Grafana Okta login.

Run everything from **one host** that has Docker, internet egress, and AWS
credentials for the test account. (If you split the image builds onto a
separate egress host, copy the persisted `KMS_KEY_ARN`, `IMAGE_URI`,
`DBADMIN_IMAGE`, `GRAFANA_IMAGE`, `COLLECTOR_IMAGE`, and `CERTIFICATE_ARN`
lines from that host's `deploy.env` into the deploy host's — the scripts
persist them automatically but only into the local file.)

Legend: ☐ = do it · 🔎 = checkpoint, confirm before moving on.

---

## 0. Pre-flight (do these before touching AWS)

**Account & tooling**
- ☐ AWS creds for the test account exported (or `AWS_PROFILE` set); confirm:
  `aws sts get-caller-identity --region us-gov-west-1`
- ☐ The deploy identity can create IAM roles, KMS keys, Lambda, RDS, ECS,
  ELBv2, Secrets Manager, ECR, and **set stack policies** (the deploy
  scripts call `set-stack-policy`).
- ☐ `docker`, `jq`, `openssl`, `aws` v2 present on the host.

**Re-deploying into an account with earlier test runs (log-group adoption)**
- ☐ The templates now pre-create three log groups that the services used to
  auto-create: `/aws/rds/instance/${NAME_PREFIX}-store/postgresql` (01) and
  `/aws/lambda/${NAME_PREFIX}-db-bootstrap` / `-db-rotation` (02). If a
  previous run left the auto-created (or now-retained) versions behind, the
  next 01/02 deploy **fails on the name collision**. Export anything you need
  from them, then delete before deploying:

  ```bash
  for g in "/aws/rds/instance/${NAME_PREFIX}-store/postgresql" \
           "/aws/lambda/${NAME_PREFIX}-db-bootstrap" \
           "/aws/lambda/${NAME_PREFIX}-db-rotation"; do
    aws logs delete-log-group --region "$AWS_REGION" --log-group-name "$g" || true
  done
  ```

  (After teardown, ALL log groups now survive by design — the same applies to
  the `/ecs/*` and `/claude/*` groups before any full re-create from scratch.)

**Bedrock**
- ☐ Model access enabled for Claude in the test account, and confirm the
  exact GovCloud inference-profile IDs:
  `aws bedrock list-inference-profiles --region us-gov-west-1 --query "inferenceProfileSummaries[?contains(inferenceProfileId,'anthropic')].inferenceProfileId"`
  (defaults assume `us-gov.anthropic.claude-opus-4-8` and
  `us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0`).

**Okta** (see the "two client secrets" note in the deploy.env comments)
- ☐ Decide the authorization server and set `OKTA_AUTH_SERVER_TYPE`:
  - **org** (this deployment): `OKTA_ISSUER` is the bare domain
    (`https://customerlogin.thecustomer.gov`, no trailing slash). The org
    server's built-in `groups` scope returns groups — no custom server or
    claim config needed. Grafana requests the `groups` scope automatically.
  - **custom**: `OKTA_ISSUER` ends in `/oauth2/<id>`; configure a `groups`
    scope + claim on that server (see the Okta notes in the project docs).
- ☐ Confirm groups actually come back: use the Okta app's token/preview (or
  a real login) for a user in the admins group and check a `groups` array is
  present. Discovery metadata does NOT list it — verify from a token.
- ☐ OIDC web app with **both** redirect URIs registered:
  `https://<FQDN>/oauth/callback` and
  `https://<FQDN>/grafana/login/generic_oauth`
  (one app if reusing; or a second app for Grafana — then a second client_id).
- ☐ An Okta group for Grafana admins (default name `grafana-admins`) with
  your test user in it.
- ☐ Have the client secret(s) on hand for the two `set-*-secret.sh` prompts.

**deploy.env**
- ☐ `cp scripts/deploy.env.example scripts/deploy.env` and fill in:
  `VPC_ID`, `VPC_CIDR`, `PRIVATE_SUBNET_IDS` (≥2 AZs), `CLIENT_INGRESS_CIDR`
  (the ZPA connector / VPN CIDR you'll test from — **narrow it**),
  `GATEWAY_FQDN`, `OKTA_ISSUER`, `OKTA_CLIENT_ID`, `ALLOWED_EMAIL_DOMAINS`,
  `GRAFANA_OKTA_CLIENT_ID` (= `OKTA_CLIENT_ID` if reusing the app),
  `GRAFANA_ADMIN_GROUP`.
- ☐ **Landing-zone decision** (top of deploy.env): NAT-equipped VPC → defaults
  are fine. No-NAT spoke → set `CREATE_SUPPORTING_ENDPOINTS="true"`,
  `PRIVATE_ROUTE_TABLE_IDS="rtb-..."`, keep `CREATE_BEDROCK_ENDPOINT="true"`;
  and set `CREATE_AMP_ENDPOINT="true"` on the obs side. For the first test
  run, a NAT-equipped VPC is the simplest path.
- ☐ Leave `OBSERVABILITY_OTLP_URL` **empty** for now (filled after stack 03).

**GPG decision for the image mirror** (the mirror now fails closed)
- ☐ Either put Anthropic's release-signing key on the host and
  `export ANTHROPIC_GPG_KEY=/path/to/key`, **or** decide to accept TLS-only
  trust with `export ALLOW_UNVERIFIED_MANIFEST=1`. Without one of these the
  very first image step stops.

**GovCloud endpoint-policy pre-check** (only if no-NAT / supporting endpoints)
- ☐ Confirm the `logs` endpoint supports policies in this region (the `ecs`
  endpoint policy was deliberately omitted for this reason):
  `aws ec2 describe-vpc-endpoint-services --region us-gov-west-1 --service-names com.amazonaws.us-gov-west-1.logs --query 'ServiceDetails[].VpcEndpointPolicySupported'`
  → if `false`, drop the `PolicyDocument` from that endpoint before deploying.

---

## 1. Certificate

```bash
./scripts/import-enterprise-cert.sh csr <FQDN>          # writes <FQDN>.key.pem + .csr
#    → hand the CSR to the enterprise CA (serverAuth EKU); collect leaf + chain
./scripts/import-enterprise-cert.sh import <FQDN> leaf.pem <FQDN>.key.pem chain.pem
```
🔎 It prints `CertificateArn:` and persists `CERTIFICATE_ARN` into deploy.env,
plus the **SHA-256 fingerprint** — save that; it's what developers pin.

### Test-account shortcut — self-signed ALB cert

If the enterprise CA isn't in the loop yet, self-sign a cert for the FQDN.
The full TLS + fingerprint-pin path works exactly as production (Claude Code
validates the chain first, then pins), so this exercises the real trust flow
— you just have to make the test laptop trust the cert. **Don't publish this
fingerprint as production-trusted.**

```bash
source scripts/deploy.env        # for AWS_REGION
FQDN="$GATEWAY_FQDN"

# 1. Self-signed cert for the FQDN (EC P-256, serverAuth, SAN = FQDN)
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout "${FQDN}.key.pem" -out "${FQDN}.crt.pem" -days 90 \
  -subj "/CN=${FQDN}" \
  -addext "subjectAltName=DNS:${FQDN}" \
  -addext "keyUsage=digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth"

# 2. Import into ACM (no chain for a self-signed cert) and record the ARN
ARN=$(aws acm import-certificate --region "$AWS_REGION" \
  --certificate "fileb://${FQDN}.crt.pem" \
  --private-key  "fileb://${FQDN}.key.pem" \
  --query CertificateArn --output text)
echo "CERTIFICATE_ARN=$ARN"
sed -i "s#^export CERTIFICATE_ARN=.*#export CERTIFICATE_ARN=\"$ARN\"#" scripts/deploy.env

# 3. The fingerprint developers compare at the /login prompt
openssl x509 -in "${FQDN}.crt.pem" -noout -fingerprint -sha256
```

**Then trust it on the test laptop** (chain validation happens before the
pin, so an untrusted self-signed cert fails TLS before you ever see the
fingerprint prompt). Either:
- **Windows cert store** (per-user, no admin) — copy `${FQDN}.crt.pem` over and:
  ```powershell
  Import-Certificate -FilePath .\claude-gateway.crt.pem `
    -CertStoreLocation Cert:\CurrentUser\Root
  ```
  (or `Cert:\LocalMachine\Root` with admin, for all users), **or**
- **`-ExtraCaCertPath`** on the installer — ship `${FQDN}.crt.pem` to the
  laptop and pass `-ExtraCaCertPath C:\path\to\claude-gateway.crt.pem`
  (writes `NODE_EXTRA_CA_CERTS`). This is the same mechanism you'll use for
  the real enterprise CA, so it's worth testing this path.

A self-signed leaf *is* its own trust anchor, so the leaf cert itself is what
goes in the store / PEM — there's no separate CA to import. Do **not** reach
for `NODE_TLS_REJECT_UNAUTHORIZED=0`.

---

## 2. Database stack (01) — FIRST

*If it fails with "version 16.x does not exist for postgres", the template's
default minor version isn't offered in this region. List available ones and
set `DB_ENGINE_VERSION` in `deploy.env`, then re-run:*
```bash
aws rds describe-db-engine-versions --engine postgres --region "$AWS_REGION" \
  --query "DBEngineVersions[?starts_with(EngineVersion,'16.')].EngineVersion" --output text
```


Creates the KMS CMK, RDS (Multi-AZ, pgaudit), and persists `KMS_KEY_ARN`
so the ECR repos built next are born CMK-encrypted.

```bash
./scripts/deploy-database.sh
```
🔎 Watch for: stack `CREATE_COMPLETE`; the `KmsKeyArnResolved` output; the
"Locking the database against replacement/deletion (stack policy)" line; and
`KMS_KEY_ARN` now present in deploy.env. RDS Multi-AZ create takes ~10–15 min.

---

## 3. Build & push images (needs Docker + egress + `KMS_KEY_ARN` from step 2)

```bash
# 3a. Gateway binary → gateway image
./client/mirror-claude-release.sh 2.1.207          # honors ANTHROPIC_GPG_KEY / ALLOW_UNVERIFIED_MANIFEST
cp mirror/2.1.207/claude docker/claude
./scripts/build-and-push-image.sh                  # persists IMAGE_URI

# 3b. DB admin Lambda image (bootstrap + rotation)
./scripts/build-and-push-dbadmin.sh                # persists DBADMIN_IMAGE (+ lambda ECR pull policy)

# 3c. Grafana image
./scripts/build-and-push-grafana.sh                # persists GRAFANA_IMAGE

# 3d. ADOT collector — mirror a pinned upstream image into ECR.
#     Set ADOT_VERSION to the release you want; the script pulls it,
#     pushes to your ECR (CMK-encrypted, immutable), and PERSISTS a
#     digest-pinned COLLECTOR_IMAGE into deploy.env automatically.
ADOT_VERSION=v0.43.0 ./scripts/mirror-collector.sh

# 3e. Download-portal image (only if deploying the optional portal, step 11)
./scripts/build-and-push-portal.sh                 # persists PORTAL_IMAGE
```
🔎 After this, `deploy.env` has all four image vars set by the scripts —
`IMAGE_URI`, `DBADMIN_IMAGE`, `GRAFANA_IMAGE`, and a digest-pinned
`COLLECTOR_IMAGE` (`<acct>.dkr.ecr.<region>.amazonaws.com/claude-gw-adot@sha256:…`).
You do **not** edit `deploy.env` by hand for any of them. Confirm with:
`grep -E 'IMAGE_URI|DBADMIN_IMAGE|GRAFANA_IMAGE|COLLECTOR_IMAGE' scripts/deploy.env`

---

## 4. Gateway stack (02) — bootstrap + first rotation happen here

```bash
./scripts/deploy-gateway.sh
```
This creates the ALB, ECS gateway, IAM, secrets, VPC endpoints, the DB
bootstrap custom resource, and the app-secret rotation schedule. **This is
the run to watch closely** — three of the four live-only validations happen
now:

🔎 **DB bootstrap** — `Custom::DbAppUserBootstrap` reaches CREATE_COMPLETE.
If it hangs, tail `/aws/lambda/<prefix>-db-bootstrap` (VPC Lambda cold start +
SG path to Postgres/Secrets Manager). The custom resource has
`ServiceTimeout: 300`, so a stuck/failing bootstrap fails the stack in ~5 min
(not the ~1h CloudFormation default) — but watch the Lambda log to see *why*.

🔎 **Target group health** — after the service starts:
```bash
aws ecs wait services-stable --region $AWS_REGION --cluster <prefix>-cluster --services <prefix>-gateway
aws elbv2 describe-target-health --region $AWS_REGION --target-group-arn <tg-arn>   # from the stack resources
```
Targets must go `healthy` on the **HTTPS** health check. If they stay
`unhealthy`, the per-task TLS cert / health-check-protocol path is the suspect.

🔎 **First app-secret rotation** (fires automatically, asynchronous):
```bash
aws secretsmanager describe-secret --region $AWS_REGION --secret-id <prefix>/db-app-user \
  --query 'VersionIdsToStages'
aws secretsmanager get-secret-value --region $AWS_REGION --secret-id <prefix>/db-app-user \
  --query 'SecretString' --output text | jq -r .username    # expect gateway_app_clone after 1st rotation
```
If it didn't flip, tail `/aws/lambda/<prefix>-db-rotation` and check the
`<prefix>-db-rotation-errors` alarm. The stack is green regardless (rotation
is async) — this is exactly the "verify, don't assume" item.

🔎 Also confirm the "Locking the ALB against replacement/deletion" line ran.

---

## 5. Corporate DNS + Zscaler

- ☐ Create the CNAME from the `AlbDnsName` stack output:
  `<FQDN>  CNAME  internal-<prefix>-alb-xxxx.us-gov-west-1.elb.amazonaws.com`
- ☐ Configure the Zscaler bypass: ZIA SSL-inspection exemption **and** app
  bypass, or a ZPA app segment for `<FQDN>`. (TLS interception here breaks
  the fingerprint pin — verify-gateway.sh will hard-fail if it sees a
  Zscaler-issued cert.)

---

## 6. Verify the gateway

```bash
./scripts/verify-gateway.sh
```
🔎 DNS (private A only, no AAAA), TLS chain + fingerprint (cross-checked
against the ACM cert when creds allow), and the OAuth endpoints answering.
Run the DNS assertions from an App Connector's resolution context if you're
behind ZPA (synthetic CGNAT answers aren't authoritative — the script says so).

- ☐ Set the gateway Okta secret and roll:
  `./scripts/set-okta-secret.sh`   (paste the client secret at the prompt)

---

## 7. Observability stack (03)

`GRAFANA_IMAGE` and `COLLECTOR_IMAGE` are already in deploy.env from step 3.

```bash
./scripts/deploy-observability.sh
```
🔎 Stack `CREATE_COMPLETE`; note the `OtlpForwardUrl` and
`GrafanaOidcRedirectUri` outputs; deploy.env now has `OBSERVABILITY_OTLP_URL`
persisted. Confirm the redirect URI matches what you registered in Okta.

---

## 8. Grafana Okta secret + wire telemetry

```bash
./scripts/set-grafana-oidc-secret.sh     # paste the (same or dedicated) client secret; rolls Grafana
./scripts/deploy-gateway.sh              # re-run: picks up OBSERVABILITY_OTLP_URL, starts forwarding
```
🔎 The gateway task rolls with telemetry forwarding on. (The deploy-gateway
guard now sees the obs stack exists and keeps the OTLP URL.)

---

## 9. End-to-end validation checklist

- ☐ **Gateway health**: `verify-gateway.sh` all green; targets healthy.
- ☐ **Developer login**: on a test laptop (or any host on the ingress CIDR),
  `claude` → `/login` → **Cloud gateway** → Okta SSO → fingerprint matches
  the published value → a prompt returns a Bedrock completion.
- ☐ **App DB user in use, not master**: the running task authenticates as
  `gateway_app*` (check `/ecs/<prefix>` logs for a clean DB connect; the
  execution role has no access to the master secret).
- ☐ **Rotation proven**: `db-app-user` AWSCURRENT username is
  `gateway_app_clone` (or flips on a manual
  `aws secretsmanager rotate-secret --secret-id <prefix>/db-app-user`), and
  the gateway service rolled afterward.
- ☐ **Grafana**: browse `https://<FQDN>/grafana` → "Sign in with Okta" →
  land as Admin (via `grafana-admins`) → the usage dashboard renders. A user
  in no mapped group is denied (strict role mapping).
- ☐ **Telemetry flowing**: after a few sessions, cost/token panels populate
  in Grafana (metrics → collector → AMP).
- ☐ **pgaudit**: the `postgresql` log group is receiving DDL/connection
  events.

---

## 9b. Optional: installer download portal (04)

Independent of the observability stack — deploy any time **after** the gateway
(02). Shares the ALB / FQDN / cert / Zscaler entry (path-based at `/portal`),
so it needs **no new DNS or Zscaler request**. It does reach the Okta issuer
outbound over the **same** server-side egress path (+ SSL-inspection exemption)
the gateway already requires — same prerequisite, so OIDC can't be verified
live until that lands.

Prereqs: portal image built (step 3e), and the Okta app has the
`https://<FQDN>/portal/oauth/callback` redirect URI + the `<ACCESS_GROUP>`
group populated (see `docs/okta-request-email.md`). **Critically, the app must
have a groups claim configured** on the authorization server (ID token, matches
regex `.*`) — the `groups` scope alone is *not* enough on the Okta org server,
and without the claim the portal denies **everyone** (groups arrive in neither
the ID token nor `/userinfo`), which looks like an authz bug but is Okta config.

```bash
./scripts/deploy-download-portal.sh                       # creates the stack + CMK artifacts bucket
./scripts/publish-portal-release.sh 2.1.207               # uploads the verified mirror output + installer
./scripts/set-portal-oidc-secret.sh                       # paste the portal client secret; rolls the service
```
🔎 Stack `CREATE_COMPLETE`; the target group goes `healthy` on the **HTTPS**
`/portal/healthz` check; `PortalOidcRedirectUri` matches Okta; `deploy.env` now
has `PORTAL_ARTIFACTS_BUCKET`. The publish step re-verifies `claude.exe`
against the manifest SHA-256 before upload.

- ☐ **Portal login + download** (needs the Okta round-trip, so only after the
  Zscaler exemption lands): browse `https://<FQDN>/portal` → Okta SSO → pick
  Team + Cost Center → the ZIP downloads → `install.cmd` inside it carries the
  baked `-GatewayUrl` / `-Sha256` / `-Team` / `-CostCenter` / `-DisableUpdates`.
- ☐ **Access denied path**: a user NOT in `<ACCESS_GROUP>` is denied, and the
  denial lands in the `/claude/<prefix>/portal-audit` log group.
- ☐ **Audit trail**: a successful download writes one JSON line (user, groups,
  team, cost_center, version, sha256, source IP) to that same group.

## 10. If something breaks

- **Custom resource / stack hung**: check the relevant Lambda log group
  first; the bootstrap and rotation functions log every step.
- **Gateway boot: TLS error, then "expected 200 OK, got: 403 Forbidden" on
  the Okta hop**: server-originated egress hitting Zscaler. The TLS error is
  the inspection cert (fix: `EXTRA_CA_CERT_PATH` or an inspection
  exemption); the 403 is ZIA *policy* blocking identity-less server traffic
  — needs an explicit ALLOW for the issuer FQDN from the VPC's egress
  location (see the networking request email, "Server-side egress").
  Probe from the admin instance to distinguish Zscaler (HTML block page)
  from an Okta network-zone 403 (JSON error):
  `curl -sv https://<issuer>/.well-known/openid-configuration`
- **A 02 deploy failed**: deploy-gateway.sh runs with `--disable-rollback` by
  default, so the stack lands in `CREATE_FAILED`/`UPDATE_FAILED` **with its
  healthy resources intact** — fix the cause and re-run the script; the
  deploy continues from the failed resource instead of tearing everything
  down (a full rollback costs ~30 min of db-admin Lambda ENI teardown alone).
  Set `CFN_DISABLE_ROLLBACK=false` to restore classic auto-rollback.
- **Rollback of 02**: the ALB and Database are stack-policy locked against
  replace/delete, so a bad update fails fast rather than destroying them —
  good. ALB deletion protection and access logs are in the template; a
  protected ALB is safe with failed creates because of `--disable-rollback`
  (no rollback ever tries to delete it). **Before deleting the 02 stack**,
  disable protection first:
  `aws elbv2 modify-load-balancer-attributes --load-balancer-arn <arn>
  --attributes Key=deletion_protection.enabled,Value=false`.
  If ALB access-log validation fails AccessDenied at create, check for a
  landing-zone auto-remediation rewriting ALB log config (seen in the first
  test run) before debugging the bucket policy — the policy grants both ELB
  delivery principals. To iterate on the DB itself in a throwaway
  test account, see the teardown-order notes below.
- **Teardown order** (test account cleanup): delete the optional stacks
  **03 and 04 first** (either order — both only import from 02), then 02, then
  01. Caveats: the db-admin Lambda ENIs can linger ~20 min and are
  attached to 01's db-client SG — if a 01 delete fails with a dependency
  violation, wait and retry. Named Secrets Manager secrets enter a 7–30 day
  recovery window; to redeploy the same `NAME_PREFIX` immediately, first
  `aws secretsmanager delete-secret --force-delete-without-recovery` the
  `<prefix>/*` secrets. RDS and the KMS key have `DeletionPolicy: Retain` /
  Snapshot — clean those up manually if you want a truly fresh account.
- **Full reference**: `README.md` (Quick start, VPC endpoints, Teardown &
  update order) and `docs/security-review-2026-07.md` (What remains).

---

### One-page command summary (NAT-equipped VPC, happy path)

```bash
# pre-flight: fill scripts/deploy.env; set ANTHROPIC_GPG_KEY or ALLOW_UNVERIFIED_MANIFEST
./scripts/import-enterprise-cert.sh csr <FQDN>
./scripts/import-enterprise-cert.sh import <FQDN> leaf.pem <FQDN>.key.pem chain.pem
./scripts/deploy-database.sh
./client/mirror-claude-release.sh 2.1.207 && cp mirror/2.1.207/claude docker/claude
./scripts/build-and-push-image.sh
./scripts/build-and-push-dbadmin.sh
./scripts/build-and-push-grafana.sh
#   ... mirror ADOT collector, set COLLECTOR_IMAGE (step 3d) ...
./scripts/deploy-gateway.sh          # watch: bootstrap, target health, first rotation
#   ... create CNAME, Zscaler bypass ...
./scripts/verify-gateway.sh
./scripts/set-okta-secret.sh
./scripts/deploy-observability.sh
./scripts/set-grafana-oidc-secret.sh
./scripts/deploy-gateway.sh          # re-run: enables telemetry forwarding
#   ... optional installer download portal (independent of 03) ...
./scripts/build-and-push-portal.sh
./scripts/deploy-download-portal.sh
./scripts/publish-portal-release.sh 2.1.207
./scripts/set-portal-oidc-secret.sh
```
