# Client configuration & enforcement model

How Claude Code is configured on developer laptops. The Windows rollout
(`client/Install-ClaudeCode.ps1`, the download portal ZIP) installs the binary
and writes workstation config **entirely in user scope** — no admin rights.
**Gateway login, however, requires one admin-delivered managed setting**:
Claude Code only offers the "Cloud gateway" login when it is present, by
Anthropic's design (§1.2). That setting is delivered by GPO/MDM (the AD request
is [`ad-request-email.md`](ad-request-email.md)) or self-served by a developer
with local admin. So the model is *no-admin install + one required managed
policy for login* — see §2.

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
  `/managed/settings` push (the model allowlist - BINARY-VERIFIED for shape and
  policy ordering, but not yet live), and a GPO-delivered
  `HKLM\SOFTWARE\Policies\ClaudeCode` source being honored by the CLI.

---

## 1. The no-admin binary install and user config

Every *installer* action is user-scope: the binary, the user PATH, and a small
`env` block in the developer's own settings file. No installer step writes a
machine-wide or policy-source setting, and a SYSTEM-context run is refused
outright (`client/Install-ClaudeCode.ps1` preconditions).

**Gateway login is the exception and it is not negotiable:** it requires one
admin-delivered managed setting (§1.2, §2). So the model is *no-admin install +
one small managed policy for login* — not fully admin-free. The managed setting
is static (one gateway URL) and set once, by GPO/MDM for a locked-down fleet or
self-served by a developer who has local admin on their own machine.

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

### 1.2 The sign-in flow — requires the managed policy (§2)

**Gateway sign-in is not available from user scope.** Claude Code offers the
"Cloud gateway" login *only* when `forceLoginMethod: "gateway"` and
`forceLoginGatewayUrl` are present in an **admin-controlled managed source**
(§2). This is Anthropic's deliberate design — so a user can never be socially
engineered into typing a hostile gateway URL that harvests their corporate SSO.
Without the managed policy, `/login` shows the standard account picker with **no
gateway option at all** — there is nothing for the developer to select, and no
place to type a URL.

- [DOC-VERIFIED] Anthropic docs: *"Without this, `/login` shows the standard
  account picker with no gateway option."*
- [BINARY-VERIFIED] The picker has no selectable gateway entry; the binary
  comment reads *`forceLoginMethod: "gateway"` "so users never type the URL"*;
  and a user-level `forceLoginMethod:"gateway"` in `~/.claude/settings.json` is
  explicitly ignored (honored only from source types `hklm`/`plist`/`file`/`helper`).

**With the managed policy in place** (delivered by GPO/MDM, or self-served on a
machine where the developer has local admin — §2), the experience is
**choice-free**:

1. Open a **new** terminal and run `claude`.
2. Claude Code opens directly on the gateway login: the method is **locked** to
   gateway and the URL is **pre-filled** — no menu to pick, no URL to type; the
   developer just **presses Enter** to connect [BINARY-VERIFIED:
   `gatewayScreenLocked`; the binary describes `forceLoginGatewayUrl` as
   "pre-fill and auto-connect", and Anthropic's docs describe the observable
   step as opening the pre-filled screen and pressing Enter].
3. The browser opens for a **one-time Okta SSO** (+MFA). That is the *only* real
   interactive step; the issued token persists (with refresh). A later re-login
   after expiry runs `/login`, still forced to gateway (again no choice).

At first connect Claude Code validates the ALB certificate chain and then pins
the leaf's SHA-256 fingerprint (**trust-on-first-use**, per hostname); the
developer confirms it against the fingerprint IT published — which is why TLS
inspection must not sit in front of the gateway FQDN
([`networking-request-email.md`](networking-request-email.md) §3).

So the managed setting is **not optional**: it is what makes the gateway login
exist *and* makes it one-touch. The AD/GPO request for it is
[`ad-request-email.md`](ad-request-email.md). [NEEDS TEST-RUN CONFIRMATION for
the live Okta round-trip.]

### 1.3 What the gateway pushes centrally

After a client authenticates, the **gateway pushes settings to it** via its
`/managed/settings` endpoint — the same mechanism it already uses to hand
clients their telemetry (OTLP) configuration [DOC-VERIFIED; NEEDS TEST-RUN
CONFIRMATION].

Two things are pushed:

**a) The model allowlist — always, to every user.** The gateway pushes
`availableModels: [<OPUS_MODEL_ID>, <SONNET_MODEL_ID>]` plus
`enforceAvailableModels: true`, which is what constrains the `/model` picker to
the two configured models. This policy carries **no `match:`**, so it applies to
every authenticated user and needs no Okta groups claim.

> **This is load-bearing, not cosmetic.** `models:` in the gateway config only
> controls what the *gateway serves* — it does not reach into the client's
> picker. Without the allowlist push, `/model` shows Claude Code's own built-in
> menu, none of whose entries this gateway serves, so every selection fails
> upstream as unauthorized (live symptom, 2026-07-24).

Both keys are ordinary Claude Code `settings.json` keys and therefore belong
**inside the policy's `cli:` object** — the blob the gateway forwards as the
client's managed settings. `availableModels` at the *policy* level is an
unrecognized key that fails gateway config validation and prevents boot.

`cli` contents are **not** checked when the config loads, but they are strictly
validated against the CLI settings schema before being served — an unknown key
there is rejected with *"unknown settings key — fix the typo, upgrade the
gateway if this key was added in a newer CLI…"*. So a typo inside `cli` survives
startup and surfaces later, on the `/managed/settings` path. Do not treat `cli`
as a free-form passthrough.

**Policy order is load-bearing.** Selection is first-match-wins over a *single*
policy, and a policy with no `match:` is normalized to `match: {}`, which matches
everyone — so the allowlist policy **must be last**, or every policy after it is
dead config. With it last, the gateway merges its `cli` as a *base* into each
earlier policy, so group members receive the allowlist *and* the lockdown.
[RUNTIME-VERIFIED against the mirrored 2.1.211 gateway, 2026-07-24.] `availableModels` accepts family aliases (`opus`), version
prefixes (`opus-4-5`), and full model IDs; an empty array means "default model
only". [BINARY-VERIFIED against the mirrored 2.1.211 gateway binary, 2026-07-24.]

**b) Update lockdown — also to everyone.** The same catch-all policy carries
`DISABLE_UPDATES` / `DISABLE_AUTOUPDATER` as `cli.env`, the server-side twin of
the installer's `-DisableUpdates`. This **used to** be scoped to Okta groups via
a `MANAGED_CLI_GROUPS` knob; that knob was **retired on 2026-07-24** when spend
limits landed. Pushing the lockdown to everyone is strictly broader coverage and
drops a groups-claim dependency, so nothing is lost by the removal.

The Okta **groups claim is still required**, but now for a different reason:
per-group spend caps (`scope_type` `rbac_group`) resolve against it, so the
gateway requests the `groups` scope unconditionally. See
[`okta-request-email.md`](okta-request-email.md).

Central push is a per-connected-client server-side control; it does **not**
require or imply any admin rights on the laptop.

### 1.4 Which settings are user-scope, and which must be managed

The previous rollout wrote a machine-wide `managed-settings.json`. Most of what
it carried now lives in the user settings file or is compensated server-side —
**but the two login keys genuinely cannot** and must come from a managed source:

| Old managed-settings key | What it did | Where it lives now |
|---|---|---|
| `forceLoginMethod: "gateway"` | Make the CLI offer/use gateway login | **Managed source only (§1.2, §2) — no user-scope substitute exists.** Without it, `/login` has no gateway option at all. The network also blocks consumer `claude.ai`/Anthropic endpoints, but that does not create the login option; only the managed key does. |
| `forceLoginGatewayUrl` | Pre-fill the URL on the login screen (press Enter to connect) | **Managed source only (§2).** There is no user-facing way to type a gateway URL — by design. |
| `requiredMinimumVersion` | Refuse to start below a version floor | The **gateway enforces a minimum client version (2.1.195+) server-side**, and the mirror-only network path pins the distributed build; a *client-side* hard floor is managed-only (§2, optional). |
| `env.DISABLE_UPDATES` / `env.DISABLE_AUTOUPDATER` | Lock auto-update | Written to the **user** settings `env` block by the installer; the gateway also pushes it centrally to every user via `/managed/settings` (§1.3); the mirror-only network path is the real control |
| `env.OTEL_RESOURCE_ATTRIBUTES` (`team` / `cost_center`) | Telemetry grouping | User settings `env` block (`-Team` / `-CostCenter`) |
| `env.NODE_EXTRA_CA_CERTS` | Enterprise CA trust | User settings `env` block (`-ExtraCaCertPath`) |
| (Okta auth, allowed email domains) | Who may use the gateway | **Gateway enforces Okta authentication + allowed email domains server-side** — never a client setting |

So the model is **no-admin binary + user config, plus one required managed
setting for login.** The network (only the gateway FQDN is reachable) and the
gateway (rejects unauthenticated / wrong-domain / below-min-version clients)
are compensations that *harden* the deployment, but they do **not** substitute
for the login key — the "Cloud gateway" option simply does not exist on a
client without it. The admin channel below is therefore required, not optional.

---

## 2. The managed-settings path for gateway login (required)

**This is the required path for gateway login, not an optional "enforcement"
add-on.** Claude Code only exposes the "Cloud gateway" login when
`forceLoginMethod: "gateway"` and `forceLoginGatewayUrl` are present in a
**managed source**; delivering them also locks the method and pre-fills the URL
so the developer just signs in (§1.2). `forceLoginMethod`, `forceLoginGatewayUrl`,
and the optional `requiredMinimumVersion` are honored **only from a managed
source** [DOC-VERIFIED + BINARY-VERIFIED], and a managed source **overrides user
and project settings** — so a developer cannot edit their way around them
(nor edit their way *into* the gateway login without one).

Deliver them by **Group Policy / MDM** for a locked-down fleet (the AD request
is [`ad-request-email.md`](ad-request-email.md)), or self-serve them once on a
machine where the developer has **local admin**. The core value is the same
small JSON either way (the two login keys; `parentSettingsBehavior` is an
optional third — §2.1). Two interchangeable mechanisms follow.

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

### 2.3 Why `HKCU\SOFTWARE\Policies\ClaudeCode` is not a no-admin backdoor

Anthropic's settings docs list `HKCU\SOFTWARE\Policies\ClaudeCode` as a real
managed source (lowest policy priority) [DOC-VERIFIED], and a non-admin can
write their own HKCU hive — so it's fair to ask whether a developer could
self-serve the gateway login there without admin. **They cannot, for the login
keys specifically:** the 2.1.211 binary honors `forceLoginMethod` /
`forceLoginGatewayUrl` only from source types `helper` / `plist` / `hklm` /
`file` — **`hklm` but not `hkcu`** [BINARY-VERIFIED: the source-gate function].
So even on a machine where the `Policies` subtree is *not* ACL-locked, a
user-written HKCU policy value is ignored for these keys. (On hardened fleets
the `Policies` subtree is additionally GPO-locked / ACL-restricted under
STIG/CIS baselines, so a non-admin can't write it at all.) This
login-keys-rejected-from-HKCU behavior is security-critical — **[NEEDS TEST-RUN
CONFIRMATION on the exact deployed binary version]**, since it is stricter than
the general source precedence the public docs describe.

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

## 3. Summary — three channels

- **Installer + user settings (no admin):** the binary, PATH, telemetry tags,
  update lockdown, and enterprise CA trust — everything except login, entirely
  in user scope for the whole fleet with zero elevation.
- **Managed settings for login (admin — REQUIRED):** `forceLoginMethod:"gateway"`
  + `forceLoginGatewayUrl` (optionally `requiredMinimumVersion`), delivered by
  GPO/MDM ([`ad-request-email.md`](ad-request-email.md)) or self-served with
  local admin. Without this the gateway login does not exist on the client; with
  it, login is method-locked and URL-prefilled (§1.2). This is the one part that
  is not admin-free.
- **Gateway `/managed/settings` (server-side):** the **client model allowlist**
  (`availableModels` / `enforceAvailableModels`) and central telemetry config for
  every connected client, plus optional update lockdown for named Okta groups
  (to every user; the group-scoped `MANAGED_CLI_GROUPS` knob was retired
  2026-07-24).

The channels compose cleanly and target different keys: the installer writes no
policy source, the managed-settings channel owns forced login, and the gateway
push owns the model allowlist plus central telemetry/update lockdown.
