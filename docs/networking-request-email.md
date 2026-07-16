# Networking request — email template

Fill the `<PLACEHOLDERS>`, delete the italic notes, pick the Zscaler option
that matches your setup, and send. The three asks (certificate, DNS, Zscaler)
can proceed in parallel; only the DNS **target value** waits on the gateway
deploy, so the name can be reserved now.

*Chicken-and-egg note: the certificate is signed against the FQDN only (not
the ALB name), so it's actionable immediately. The CNAME target
(`internal-*.elb.amazonaws.com`) only exists after we deploy the load
balancer — we'll send that value the same day and ask you to populate the
record then.*

---

**To:** Network Engineering / PKI / Zscaler admins
**Cc:** <project lead>, <security/ATO contact>
**Subject:** Provisioning request — Claude gateway (GovCloud): enterprise cert, internal DNS CNAME, Zscaler access

Hi team,

We're standing up an internal-only application in AWS GovCloud
(`us-gov-west-1`) — the Claude apps gateway for our developers. It sits
behind an **internal** Application Load Balancer (no public exposure) and is
reached only from our corporate network. To bring it online we need three
things from you, detailed below. Happy to jump on a call to walk through any
of them.

---

## 1. Enterprise-CA TLS certificate  *(actionable now)*

The ALB terminates TLS with a certificate signed by our **internal**
enterprise CA (a public CA can't issue for this name/deployment). We'll
generate the key and CSR and handle the AWS import — we just need the CA to
sign.

- **Subject / SAN:** `<GATEWAY_FQDN>` (e.g. `claude-gateway.<corp-domain>`)
  — the SAN must be **exactly** this FQDN.
- **Key / algorithm:** EC P-256 by default (CSR attached). *If our CA only
  issues RSA, tell us and we'll regenerate the CSR as RSA-2048/3072 — quick.*
- **Extended Key Usage:** must include **serverAuth**.
- **Return format (PEM):** the signed leaf certificate, and the CA chain as a
  separate file with **intermediates first, root last**.
- **Validity:** your standard server-cert lifetime is fine. Please note it —
  this cert is imported into AWS and does **not** auto-renew, so we'll set an
  expiry alarm and need a renewal owner on your side. *(Rotation is
  in-place and re-triggers a one-time client trust prompt, so we coordinate a
  heads-up before each renewal.)*

**CSR:** attached / provided separately as `<GATEWAY_FQDN>.csr`.

---

## 2. Internal DNS — CNAME record

A single CNAME in corporate DNS pointing our friendly name at the load
balancer's AWS DNS name:

| Record | Type | Target |
|---|---|---|
| `<GATEWAY_FQDN>` | CNAME | `<ALB_DNS_NAME>` — *we'll send this after deploy; format:* `internal-<name>-<id>.us-gov-west-1.elb.amazonaws.com` |

Notes for your resolver config:
- The ALB target is a **public** DNS record that returns **private** IP
  addresses — resolvable from any resolver, but routable only inside our VPC.
  So this is a normal CNAME in the corporate zone; **no** split-horizon /
  private hosted zone / conditional forwarder is required.
- **Please confirm:** does our resolver enforce **DNS-rebinding protection**
  (stripping RFC1918/private answers out of public-zone responses)? If so it
  would break resolution of the ALB name and we'll need a small exception —
  worth checking before go-live.

---

## 3. Zscaler access

End users reach `<GATEWAY_FQDN>` from managed laptops. **Pick the option that
matches how we deliver this app** (ZPA is preferred):

### Option A — ZPA application segment  *(preferred)*
- **App segment:** `<GATEWAY_FQDN>`, **TCP 443**, served by App Connectors
  that can route to our workload VPC (over the Transit Gateway).
- **Do NOT enable TLS inspection** on this segment — the client pins the
  gateway certificate's fingerprint, and interception breaks it.
- **Access policy:** scope to the developer group `<DEV_OKTA_OR_AD_GROUP>`.
- **App Connector DNS:** the connectors must be able to resolve
  `<GATEWAY_FQDN>`. On-prem connectors using AD DNS already can; connectors
  running **in an AWS VPC** need a Route 53 Resolver outbound rule forwarding
  our corporate zone to AD DNS (otherwise users see an unexplained ZPA
  timeout).

### Option B — ZIA bypass  *(if egress is via ZIA forward proxy instead)*
- **SSL-inspection exemption** for `<GATEWAY_FQDN>` (inspection breaks the
  certificate fingerprint pin).
- **Proxy/app bypass** for `<GATEWAY_FQDN>` so traffic doesn't egress via
  public Zscaler proxy IPs (the client's private-network check fails
  otherwise).

### Also needed for the offline client rollout (either option)
- **UNC file-share app segment:** the installer pulls `claude.exe` from
  `\\<FILE_SERVER_FQDN>\<share>` over **TCP 445** — this needs its **own** ZPA
  app segment (addressed by the fileserver FQDN), separate from the gateway
  segment above.
- **Machine Tunnel** *(if we push the client via Intune/SCCM in device/SYSTEM
  context)*: SYSTEM traffic isn't carried by the ZPA **user** tunnel, so the
  device-context install can't reach the UNC share without a Zscaler Machine
  Tunnel. *(Flag to the endpoint-management team if that's separate.)*

---

## 4. Related confirmations  *(networking-adjacent — quick to overlook)*

If the workload VPC egresses through a **central inspection firewall** (TGW →
central egress), please allow-list outbound from the gateway subnets to:
- our **Okta issuer** host (`<OKTA_ISSUER_HOST>`) — this is the only
  internet-bound dependency; and
- *(only if we don't use local VPC endpoints)* the AWS ECR / S3 / Amazon
  Managed Prometheus service domains for `us-gov-west-1`.

---

## What we need back from you

1. **App Connector source CIDR(s)** — the IP ranges our connectors present to
   the ALB. We lock the load-balancer security group to exactly these, so we
   can't finish without them: `<PENDING — please provide>`.
2. **Certificate validity period** + who owns renewal on your side.
3. **DNS-rebinding-protection** answer (§2).
4. A rough **turnaround / ticket number** so we can sequence the deploy.

Thanks very much — this unblocks our test deployment. Reply-all or grab me at
`<contact>`.

Best,
`<name>`

---

## Placeholder cheat-sheet (delete before sending)

| Placeholder | Where it comes from |
|---|---|
| `<GATEWAY_FQDN>` | `GATEWAY_FQDN` in `scripts/deploy.env` |
| `<ALB_DNS_NAME>` | `AlbDnsName` output of the gateway stack (after `deploy-gateway.sh`) |
| `<DEV_OKTA_OR_AD_GROUP>` | the developer group gated for gateway access |
| `<FILE_SERVER_FQDN>` / `<share>` | where you stage `claude.exe` for the offline installer |
| `<OKTA_ISSUER_HOST>` | host part of `OKTA_ISSUER` |
| App Connector source CIDR | becomes `CLIENT_INGRESS_CIDR` in `deploy.env` |
