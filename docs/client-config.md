# Client configuration & enforcement model

How Claude Code is configured on developer laptops, and how gateway-only use is
enforced — under a **no-admin default**. The Windows rollout
(`client/Install-ClaudeCode.ps1`, the download portal ZIP) installs and
configures entirely in user scope; nothing it does needs administrator rights.
Where an organization wants *forced* gateway login, that is delivered through
an admin channel (GPO/MDM), documented in full below.

This is an operations how-to (like [`test-run-runbook.md`](test-run-runbook.md))
and is deliberately **not** part of the PDF review package. The ConOps
([`conops.md`](conops.md)) references this model; the security-review fix log
([`security-review-2026-07.md`](security-review-2026-07.md), 2026-07-22 entry)
records the redesign.

## Verification status — read this first

Per the repo honesty rule (`.claude/rules/process.md`), claims here are tagged:

- **[BINARY-VERIFIED]** — checked by inspecting the `claude` build in this
  repo's local mirror (`mirror/2.1.211/`, gitignored; the
  `deploy.env.example` `CLAUDE_VERSION` default may lag it — pin whatever you
  mirror). Confirmed in that build: the `/login` picker exposes a **"Cloud
  gateway"** option and a **"Gateway URL"** prompt; `forceLoginGatewayUrl`
  exists only to **pre-fill and auto-connect**; managed settings are read from
  `%ProgramFiles%\ClaudeCode\managed-settings.json` (the path Claude Code moved
  to from `%ProgramData%` at v2.1.75).
- **[DOC-VERIFIED]** — confirmed against Claude Code / Anthropic documentation
  but not exercised in this deployment (e.g. the gateway `/managed/settings`
  push, `forceLoginMethod` / `requiredMinimumVersion` semantics as
  managed-only keys).
- **[NEEDS TEST-RUN CONFIRMATION]** — behavior we assert but have not yet run
  end to end: the live interactive Cloud-gateway login, the gateway
  `/managed/settings` push (including `MANAGED_CLI_GROUPS`), and a GPO-delivered
  `HKLM\SOFTWARE\Policies\ClaudeCode` source being honored by the CLI.

---

## 1. The no-admin default

Every workstation-side action is user-scope. No installer step writes a
machine-wide or policy-source setting, and a SYSTEM-context run is refused
outright (`client/Install-ClaudeCode.ps1` preconditions).

### 1.1 What the installer / portal ZIP does

Running `Install-ClaudeCode.ps1` (directly from the share, or via the
double-click `install.cmd` inside the download-portal ZIP) does exactly three
things, all in the developer's own profile:

- **Binary** → `%USERPROFILE%\.local\bin\claude.exe`, verified (SHA-256 against
  the release manifest + Anthropic Authenticode) on a local staging copy before
  it is moved into place.
- **User PATH** → `%USERPROFILE%\.local\bin` is appended to the *user* `Path`
  environment variable (registry-backed, persists; no machine PATH edit).
- **User configuration** → an `env` block merged into
  `%USERPROFILE%\.claude\settings.json` (the developer's own settings file). The
  merge preserves every existing top-level key and every unrelated `env` key,
  and refuses to overwrite a file it cannot parse. The keys written:

  | Key (under `env`) | Set by | Purpose |
  |---|---|---|
  | `DISABLE_UPDATES` = `1` | `-DisableUpdates` | Blocks all update paths (background checks **and** manual `claude update` / `claude install`) — keeps users on the distributed build |
  | `DISABLE_AUTOUPDATER` = `1` | `-DisableUpdates` | Background-check lockdown, defense in depth |
  | `OTEL_RESOURCE_ATTRIBUTES` | `-Team` / `-CostCenter` | Telemetry grouping labels (`team=…,cost_center=…`); telemetry itself is enabled centrally by the gateway |
  | `NODE_EXTRA_CA_CERTS` | `-ExtraCaCertPath` | Enterprise CA trust for the gateway TLS chain (the precompiled binary honors it) |

These are ordinary environment variables, honored from the user settings file —
**not** policy keys. The installer never writes
`%ProgramFiles%\ClaudeCode\managed-settings.json` and never touches
`HKx\SOFTWARE\Policies\ClaudeCode`.

### 1.2 The one-time sign-in flow

Gateway sign-in is **interactive and needs no settings** [BINARY-VERIFIED for
the picker/prompt; NEEDS TEST-RUN CONFIRMATION for the live round-trip]. Both
the installer and the portal ZIP `README.txt` print these three steps:

1. Open a **new** terminal and run `claude`.
2. Run `/login` and choose **"Cloud gateway"**.
3. Paste the **gateway URL** (e.g. `https://claude-gateway.example.com`) when
   prompted.

At first connect Claude Code validates the ALB certificate chain and then pins
the leaf's SHA-256 fingerprint (**trust-on-first-use**, per hostname); the
developer confirms it against the fingerprint IT published. This is why TLS
inspection must not sit in front of the gateway FQDN
([`networking-request-email.md`](networking-request-email.md) §3).

No `forceLoginMethod` / `forceLoginGatewayUrl` is written to make this happen —
those keys only *pre-fill and auto-connect* the URL and are managed-only
(§2). The interactive flow is the no-admin default; the forced flow is the
opt-in admin-channel variant.

### 1.3 What the gateway pushes centrally

After a client authenticates, the **gateway pushes settings to it** via its
`/managed/settings` endpoint — the same mechanism it already uses to hand
clients their telemetry (OTLP) configuration [DOC-VERIFIED; NEEDS TEST-RUN
CONFIRMATION].

A new `deploy.env` knob **`MANAGED_CLI_GROUPS`** (CloudFormation parameter
`ManagedCliGroups`, wired in parallel) makes the gateway push
`DISABLE_UPDATES` / `DISABLE_AUTOUPDATER` to members of the listed Okta groups,
so update lockdown can be enforced centrally without touching the workstation.
It **requires the Okta groups claim** (the `groups` scope alone yields no group
membership on an org authorization server — see
[`okta-request-email.md`](okta-request-email.md) and
[`conops.md`](conops.md) §8.2). With no groups listed, the gateway pushes
telemetry config only.

Central push is a per-connected-client server-side control; it does **not**
require or imply any admin rights on the laptop.

### 1.4 Why no admin is needed — old managed-settings key → no-admin replacement

The previous rollout wrote a machine-wide `managed-settings.json`. Every key it
carried now has a user-scope replacement or a server-side / network
compensation, which is why the installer no longer needs elevation:

| Old managed-settings key | What it did | No-admin replacement / compensation |
|---|---|---|
| `forceLoginMethod: "gateway"` | Force the CLI onto gateway login | Interactive `/login` → "Cloud gateway" (§1.2); the **network blocks consumer `claude.ai`/Anthropic endpoints**, so clients can only reach the gateway FQDN. Hard enforcement → GPO/MDM (§2) |
| `forceLoginGatewayUrl` | Pre-fill + auto-connect the URL | Developer pastes the URL once (printed by the installer and the portal ZIP). Forced pre-fill → GPO/MDM (§2) |
| `requiredMinimumVersion` | Refuse to start below a version floor | The **gateway enforces a minimum client version (2.1.195+) server-side**; the mirror-only network path pins the distributed build. Client-side hard floor → GPO/MDM (§2) |
| `env.DISABLE_UPDATES` / `env.DISABLE_AUTOUPDATER` | Lock auto-update | Written to the **user** settings `env` block by the installer; the gateway can also push it centrally via `/managed/settings` (`MANAGED_CLI_GROUPS`, §1.3); the mirror-only network path is the real control |
| `env.OTEL_RESOURCE_ATTRIBUTES` (`team` / `cost_center`) | Telemetry grouping | User settings `env` block (`-Team` / `-CostCenter`) |
| `env.NODE_EXTRA_CA_CERTS` | Enterprise CA trust | User settings `env` block (`-ExtraCaCertPath`) |
| (Okta auth, allowed email domains) | Who may use the gateway | **Gateway enforces Okta authentication + allowed email domains server-side** — never a client setting |

The residual gap versus the old model is a *client-side hard lock* on login
method and version floor. Without forced login, that gap is covered by two
compensations working together: the **network** admits only the gateway FQDN
(consumer endpoints blocked), and the **gateway** rejects unauthenticated,
wrong-domain, or below-minimum-version clients server-side. When an
organization needs the hard client-side lock on top of those, it uses the
admin channel below.

---

## 2. The GPO/MDM path for forced login (admin channel)

When the organization wants **enforced gateway-only login** — the CLI itself
refusing any other login method, auto-connecting the gateway URL, and refusing
to start below a version floor — those settings are delivered as **managed
settings** through Group Policy (or an equivalent MDM configuration profile),
not by the user-run installer. `forceLoginMethod`, `forceLoginGatewayUrl`, and
`requiredMinimumVersion` are keys Claude Code honors **only from a managed
source** [DOC-VERIFIED], and a managed source **overrides user and project
settings** — so a developer cannot edit their way around them.

The managed-settings JSON to deliver (single object, one line for the registry
value):

```json
{"forceLoginMethod":"gateway","forceLoginGatewayUrl":"https://<GATEWAY_FQDN>","requiredMinimumVersion":"2.1.195"}
```

Bump `requiredMinimumVersion` when you raise the fleet's floor (default is
`2.1.195`, the gateway's server-side minimum). There are two interchangeable
delivery mechanisms; pick whichever fits the fleet's GPO conventions.

### 2.1 Mechanism A — GPP Registry item (recommended)

Deliver the settings as a single registry string value under the machine
policy hive. Claude Code reads managed settings from
`HKLM\SOFTWARE\Policies\ClaudeCode`, value name `Settings`, type `REG_SZ`, whose
data is the one-line JSON above [DOC-VERIFIED].

Steps an AD admin can follow:

1. In the Group Policy Management Console, edit a GPO linked to the OU
   containing the developer workstations (or a security group filtered to
   them).
2. Navigate to **Computer Configuration → Preferences → Windows Settings →
   Registry**. Machine-scope, so the policy applies regardless of which user
   logs on.
3. **New → Registry Item.** Set:
   - Action: **Update** (creates the value if missing, updates it if present —
     the safe default).
   - Hive: **HKEY_LOCAL_MACHINE**
   - Key Path: **`SOFTWARE\Policies\ClaudeCode`**
   - Value name: **`Settings`**
   - Value type: **`REG_SZ`**
   - Value data: the single-line JSON above, with `<GATEWAY_FQDN>` substituted.
4. Apply. Clients pick it up on the next Group Policy refresh (or
   `gpupdate /force`).

Use **Update** (not Replace) so the item is refreshed in place on each policy
cycle without churn.

### 2.2 Mechanism B — GPP Files item (managed-settings.json)

Alternatively, deploy the same JSON as a file to the machine-wide managed path.
Claude Code reads `%ProgramFiles%\ClaudeCode\managed-settings.json`
[BINARY-VERIFIED against the mirrored 2.1.211 binary] — **not** `%ProgramData%`
(the path moved at v2.1.75). `%ProgramFiles%` is admin-write-only, which is what
makes the file tamper-resistant.

Steps:

1. Stage `managed-settings.json` (containing the JSON object above) on a
   share every client can read, e.g. `\\fileserver\software\claude\`.
2. In the GPO, navigate to **Computer Configuration → Preferences → Windows
   Settings → Files**. (Computer Configuration, so the destination resolves to
   the machine's `%ProgramFiles%` and the copy runs with machine rights — a
   standard user cannot write there.)
3. **New → File.** Set:
   - Action: **Update** (or Replace to overwrite on every refresh).
   - Source file(s): the UNC path, e.g.
     `\\fileserver\software\claude\managed-settings.json`.
   - Destination file:
     `%ProgramFiles%\ClaudeCode\managed-settings.json`.
4. Apply; clients copy the file on the next Group Policy refresh.

MDM equivalent: a device-scoped configuration profile (Intune Win32 app or a
custom profile / script) that writes the same file to
`%ProgramFiles%\ClaudeCode\` or the same value to
`HKLM\SOFTWARE\Policies\ClaudeCode`. Deliver it in **device** context, not user
context.

### 2.3 Why not `HKCU\SOFTWARE\Policies\ClaudeCode`

Claude Code also treats `HKCU\SOFTWARE\Policies\ClaudeCode` as a managed source
[DOC-VERIFIED], and a per-user policy value would not need admin rights to
write in principle. It is **not** used here because on hardened fleets the
`Policies` subtree is **GPO-locked / ACL-restricted** (STIG/CIS baselines deny
standard users write access to `...\Policies\...` even under HKCU), so a
user-run install cannot reliably write it — which is exactly why the installer
no longer attempts any policy-source write and leaves enforcement to the
machine-scope channels above.

### 2.4 Upgrading from an earlier installer — clear stale managed settings

An earlier version of `Install-ClaudeCode.ps1` wrote forced-login keys
(`forceLoginMethod` / `forceLoginGatewayUrl` / `requiredMinimumVersion`) to a
**managed** source — `HKCU\SOFTWARE\Policies\ClaudeCode` on a non-admin run, or
`%ProgramFiles%\ClaudeCode\managed-settings.json` (formerly `%ProgramData%`)
when elevated. Because managed sources **override** the new user-scope
settings, any of those left behind on a machine will keep taking effect after
you switch to the current installer: a stale `forceLoginGatewayUrl` can lock
the login screen to an old URL, and a stale `requiredMinimumVersion` can block
an approved build. The current installer does **not** clean these up (it writes
nothing to managed sources, so it has no basis to).

On a machine that was ever provisioned by the old installer, clear the stale
managed settings **unless** you are deliberately taking them over via the
GPO/MDM channel above. As the user (for the HKCU value) and as an admin (for
the file):

```powershell
Remove-Item -Path 'HKCU:\SOFTWARE\Policies\ClaudeCode' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $env:ProgramFiles 'ClaudeCode\managed-settings.json') -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $env:ProgramData 'ClaudeCode\managed-settings.json') -Force -ErrorAction SilentlyContinue  # pre-2.1.75 path
```

Then confirm with `/status` (below) that no unexpected managed source remains.
Fresh fleets that never ran the old installer are unaffected. [NEEDS TEST-RUN
CONFIRMATION for any test laptops provisioned during earlier runs.]

### 2.5 Verifying the active configuration

Inside `claude`, run **`/status`** — it shows the **active setting sources**,
so an admin can confirm the managed source is present and winning over user
settings [DOC-VERIFIED; NEEDS TEST-RUN CONFIRMATION that a GPO-delivered
`HKLM` source shows up as expected in this environment]. Precedence, highest
first: managed source (GPO/MDM) → project settings → user settings
(`%USERPROFILE%\.claude\settings.json`). A `forceLoginMethod` shown as sourced
from the managed layer confirms the lock is in force.

---

## 3. Summary — two composable channels

- **Installer + user settings (no admin):** the binary, PATH, telemetry tags,
  update lockdown, and enterprise CA trust — everything a developer needs to
  install and sign in interactively. This is the default and covers the whole
  fleet with zero elevation.
- **Gateway `/managed/settings` (server-side):** central telemetry config for
  every connected client, plus optional update lockdown for named Okta groups
  (`MANAGED_CLI_GROUPS`).
- **GPO/MDM managed settings (admin channel, opt-in):** forced gateway login,
  auto-connect URL, and a client-side version floor, when the organization
  wants a hard lock beyond the network + gateway server-side controls.

The channels compose cleanly: the installer never contends with the admin
channel because it writes no policy source, and the gateway push and GPO
managed settings target different keys (central telemetry/update lockdown vs.
forced-login enforcement).
