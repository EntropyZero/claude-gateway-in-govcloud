# Active Directory / GPO request — email template

Fill the `<PLACEHOLDERS>`, delete the italic notes, pick **one** delivery
mechanism (Registry is recommended), and send.

*Why this is needed: Claude Code only offers the "Cloud gateway" login option
when a specific setting is present in an **admin-controlled machine-policy
source**. It is honored ONLY from a managed source — never from a developer's
own settings — so it must be delivered by GPO (or MDM). Without it, the gateway
login does not appear in the app at all. This is one small static value (the
gateway URL), set once. This is Anthropic's design, not a limitation of our
deployment.*

---

**To:** Active Directory / Group Policy administrators (Windows endpoint / Intune team)
**Cc:** \<project lead\>, \<security/ATO contact\>
**Subject:** GPO request — Claude Code gateway-login policy for developer workstations

Hi team,

We're rolling out **Claude Code** (Anthropic's coding CLI) to developers,
pointed at our internal Claude apps gateway in AWS GovCloud. The binary installs
per-user with **no admin rights**, but the **gateway login itself requires one
machine-policy setting** that only an administrator can deliver. Without it, the
"Cloud gateway" option does not appear in the app's login menu — by Anthropic's
design (it stops a user from being tricked into signing in at an arbitrary or
hostile "gateway" URL that would harvest their corporate SSO).

We need a GPO that sets the value below on the developer workstations. It is a
single static value — no per-user data, nothing time-varying (it changes only
if the gateway URL itself changes).

---

## 1. What to set

Claude Code reads a JSON "managed settings" object. We need this object
delivered (substitute our gateway FQDN):

```json
{
  "forceLoginMethod": "gateway",
  "forceLoginGatewayUrl": "https://<GATEWAY_FQDN>",
  "forceRemoteSettingsRefresh": true,
  "parentSettingsBehavior": "merge"
}
```

- `<GATEWAY_FQDN>` = `<claude-gateway.example.com>` — our internal gateway
  (final value from the platform team; the name can be reserved now).

There are two supported delivery mechanisms. **Please use Mechanism A (Registry)
unless you specifically prefer file distribution** — it is one value, no file
staging, and no text-encoding pitfalls.

### Mechanism A — GPP Registry item  *(recommended)*

- **GPO path:** Computer Configuration → Preferences → Windows Settings → **Registry**
- **Action:** `Update`
- **Hive:** `HKEY_LOCAL_MACHINE`
- **Key path:** `SOFTWARE\Policies\ClaudeCode`
- **Value name:** `Settings`
- **Value type:** `REG_SZ`
- **Value data** (single line, substitute the FQDN):

  ```
  {"forceLoginMethod":"gateway","forceLoginGatewayUrl":"https://<GATEWAY_FQDN>","forceRemoteSettingsRefresh":true,"parentSettingsBehavior":"merge"}
  ```

Machine scope (Computer Configuration) so it applies to any user who signs on.
Use **Update** (not Replace) so the item refreshes in place each policy cycle
without churn.

### Mechanism B — GPP Files item  *(alternative)*

- Stage a file `managed-settings.json` (containing the JSON object above) on a
  share every workstation can read, e.g.
  `\\<fileserver>\<share>\ClaudeCode\managed-settings.json`. **The share must
  grant read to Domain Computers / machine accounts:** a Computer-Configuration
  GPP Files copy runs as **SYSTEM** and authenticates to the UNC path as the
  *machine* account, not the logged-on user (the classic GPP-Files failure mode
  — another reason we prefer Mechanism A).
- **GPO path:** Computer Configuration → Preferences → Windows Settings → **Files**
- **Action:** `Update` · **Source:** the UNC path above · **Destination:**
  `%ProgramFiles%\ClaudeCode\managed-settings.json`
- **File encoding — important:** the JSON file must be **UTF-8 *without* a BOM**.
  Claude Code rejects a byte-order-marked JSON file, and many Windows editors
  (and PowerShell 5.1's `Set-Content -Encoding UTF8`) add a BOM by default. Save
  as "UTF-8" / "UTF-8 (no BOM)", not "UTF-8 with BOM". *(This encoding trap is
  exactly why we prefer Mechanism A.)*
- The destination is `%ProgramFiles%` (admin-write-only — that is the point;
  it's tamper-resistant). Note Claude Code moved this path from `%ProgramData%`
  in a recent version, so please use `%ProgramFiles%`.

---

## 2. Targeting / scope

- Apply to the OU (or security group) holding the developer workstations:
  `<OU_OR_SECURITY_GROUP>`.
- **Computer Configuration** (machine policy) — applies regardless of which user
  logs on.
- Clients pick it up on the next Group Policy refresh (or `gpupdate /force`).

---

## 3. Why it must be machine policy (not a user setting)

- Claude Code honors `forceLoginMethod` / `forceLoginGatewayUrl` **only** from an
  administrator-controlled source — machine registry policy (HKLM), a machine
  `managed-settings.json`, or a macOS managed preference. A value a developer
  places in their own profile (`HKCU`, `%USERPROFILE%\.claude\settings.json`) is
  **ignored**. So a non-admin fleet cannot self-serve this; it must be a GPO.
- **The design reason (worth stating to the reviewer):** the gateway terminates
  the developer's Okta SSO. If any user could point the CLI at an arbitrary
  gateway URL, that would be a credential-phishing / MITM vector. Requiring an
  admin-vetted URL closes it.

---

## 4. What each key does

- `forceLoginMethod: "gateway"` — makes the CLI use (and only offer) the gateway
  login; this is what causes the "Cloud gateway" option to exist and be
  pre-selected.
- `forceLoginGatewayUrl` — the gateway URL, pre-filled on the login screen so
  developers never type it (they press Enter to connect).
- `forceRemoteSettingsRefresh: true` — the CLI **blocks startup until it has
  freshly fetched the gateway's managed settings, and exits if that fetch
  fails**. This is what guarantees the gateway's central policy actually reaches
  the client. Without it, a laptop that cannot reach the gateway starts anyway
  with **no** gateway policy applied — which means the model allowlist is
  absent and Claude Code falls back to its own built-in model menu, none of
  whose entries our gateway serves.

  **Trade-off, stated plainly:** this converts a gateway or network outage from
  "Claude Code runs with an unrestricted model menu" into "Claude Code will not
  start". Since the gateway is already a hard dependency for inference, this
  mostly makes an existing dependency honest rather than adding a new one — but
  it does remove any offline grace period. Drop this key if you would rather
  developers retain a (non-functional for inference) CLI during an outage.
- `parentSettingsBehavior: "merge"` — *(optional; harmless to include)* controls
  whether managed settings supplied by an **embedding process via the SDK**
  (`Options.managedSettings` / `--managed-settings`, e.g. Claude Desktop or an
  IDE extension launching Claude Code) layer underneath this admin policy tier.
  `first-wins` (the default) drops them entirely. It has no effect on the
  gateway's own `/managed/settings` push, and no effect at all on machines where
  developers just run `claude` and sign in. [BINARY-VERIFIED 2026-07-24.]
- *(Optional)* `requiredMinimumVersion: "2.1.195"` — refuse to start below this
  client version. Add it if you want a hard client-side version floor; the
  gateway also enforces a minimum server-side, so this is optional.

---

## 5. Confirming it worked

On a test workstation after the GPO applies (`gpupdate /force`):

- A developer runs `claude`, then `/status`. The "setting sources" should list
  the **managed (policy)** source, with `forceLoginMethod` shown as coming from
  it.
- Running `claude` should go **straight to the gateway login** — no menu, URL
  pre-filled — and open the browser for a one-time **Okta** sign-in.

---

## 6. Notes

- No per-user data; one static value. It changes only if the gateway FQDN
  changes — and any change is a one-line GPO edit.
- This composes with, and is separate from, the enterprise-CA trust and Zscaler
  asks in [`networking-request-email.md`](networking-request-email.md), and the
  Okta app in [`okta-request-email.md`](okta-request-email.md) (the SSO the
  gateway login uses).
- We can provide a test workstation and the exact `<GATEWAY_FQDN>` value, and
  are happy to jump on a call.

Thanks,
\<name / team\>
