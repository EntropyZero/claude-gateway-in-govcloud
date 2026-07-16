# Okta configuration request — email template

Fill the `<PLACEHOLDERS>`, delete the italic notes, and send to the Okta
administrator. This is written for the **org authorization server** (no
custom authorization server is created or required) and a **confidential
Web app** — the two things this deployment specifically needs.

*Security note: don't send the client secret back in plain email — use a
secrets channel (vault, one-time-secret link, etc.). The rest is fine over
email.*

---

**To:** Okta administrator
**Cc:** <project lead>, <security/ATO contact>
**Subject:** Okta OIDC app request — Claude gateway (`<FQDN>`): Web app + groups

Hi <name>,

We're deploying an internal application (the Claude apps gateway, with a
Grafana admin dashboard) that signs users in via Okta OIDC against our org
authorization server. Could you set up the following OIDC application and
send back the details at the bottom? Happy to hop on a call.

## 1. Application — OIDC **Web** app (confidential)

- **Sign-in method:** OIDC — **Web Application** (this is important: it must
  be the *Web* type so it has a **client secret**. A Single-Page/Native/public
  app has no secret and will not work — both the gateway and Grafana are
  server-side confidential clients and require the secret).
- **Grant type:** **Authorization Code**. (Our clients also send PKCE (S256) —
  fine to leave PKCE allowed; it's used *in addition to* the secret, not
  instead of it.)
- **Sign-in redirect URIs** — register **both** on this one app:
  - `https://<FQDN>/oauth/callback`  (the gateway)
  - `https://<FQDN>/grafana/login/generic_oauth`  (the Grafana dashboard)
- **Sign-out redirect URI:** not required.

*One app covers both the gateway and Grafana (both redirect URIs above). If
you'd rather isolate them, a second Web app dedicated to Grafana also works —
then we'd need a second client ID/secret. Either is fine; one app is simpler.*

## 2. Authorization server — the **org** server

- Use the **org authorization server** (issuer = our Okta domain,
  `https://<OKTA_DOMAIN>`). **Please do not create a custom authorization
  server** — our deployment is configured for the org server.

## 3. Groups — needed for dashboard role mapping

Grafana maps Okta group membership to its roles, so the app needs to return
the user's groups:

- On the app's OIDC settings, configure the **groups claim** so the user's
  Okta group memberships are returned when the `groups` scope is requested
  (the org server's built-in `groups` scope). A filter of **Matches regex**
  `.*` is simplest, or restrict to a prefix (e.g. groups starting with
  `grafana`/`claude`). Grafana requests the `groups` scope automatically.
- Create (or confirm) the admin group **`<GRAFANA_ADMIN_GROUP>`** (default
  name `grafana-admins`) and add our test user `<TEST_USER>` plus the
  intended dashboard admins. *(Role mapping is strict — a user in none of the
  mapped groups is denied the dashboard, so the test user must be in this
  group.)*

## 4. Assignment & email domains

- **Assign** the app to the people who should sign in — the developer
  population for the gateway, and the admins above for the dashboard. Only
  assigned users can authenticate.
- The gateway restricts sign-in to specific email domains
  (**`<ALLOWED_EMAIL_DOMAINS>`**), so assigned users need an Okta profile
  email in one of those domains.

## What to send back

1. **Client ID** (fine over email).
2. **Client Secret** — via a **secure** channel, not plain email.
3. Confirmation of the **issuer** (org server): `https://<OKTA_DOMAIN>`.
4. Confirmation that **groups are returned**: the easiest proof is the app /
   authorization-server **Token Preview** (or a real test login) for a user in
   `<GRAFANA_ADMIN_GROUP>`, showing a `groups` array in the token. *(Note: the
   groups won't appear in the `.well-known` discovery metadata — that's
   expected; it has to be checked from an actual token.)*
5. The exact **group name(s)** if they differ from `grafana-admins`.

Thanks very much — this is the last dependency before we can test end-to-end.

Best,
<name>

---

## Placeholder cheat-sheet (delete before sending)

| Placeholder | Value / where it comes from |
|---|---|
| `<FQDN>` | `GATEWAY_FQDN` in `scripts/deploy.env` (e.g. `claude-gateway.<domain>`) |
| `<OKTA_DOMAIN>` | your Okta org domain, e.g. `customerlogin.thecustomer.gov` |
| `<GRAFANA_ADMIN_GROUP>` | `GRAFANA_ADMIN_GROUP` in `deploy.env` (default `grafana-admins`) |
| `<TEST_USER>` | the user you'll log in with during the test run |
| `<ALLOWED_EMAIL_DOMAINS>` | `ALLOWED_EMAIL_DOMAINS` in `deploy.env` |

What you'll receive maps to `deploy.env` as: Client ID → `OKTA_CLIENT_ID`
(and `GRAFANA_OKTA_CLIENT_ID` = the same if one app); Client Secret →
entered at the `set-okta-secret.sh` / `set-grafana-oidc-secret.sh` prompts
(never stored in `deploy.env`); issuer → `OKTA_ISSUER` (the bare domain, with
`OKTA_AUTH_SERVER_TYPE=org`).
