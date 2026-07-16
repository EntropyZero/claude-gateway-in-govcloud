<#
.SYNOPSIS
  Offline installer for Claude Code on Windows - no calls to claude.ai or
  downloads.claude.ai. Takes an already-downloaded claude.exe (mirrored from
  downloads.claude.ai/claude-code-releases/<version>/ and verified against the
  GPG-signed manifest), places it in %USERPROFILE%\.local\bin, and adds that
  directory to the user PATH.

.DESCRIPTION
  Designed for MDM/SCCM/Intune push or manual use in networks where the
  public installer's version lookup is blocked. Optionally verifies the
  binary (SHA-256 from manifest.json and/or the Anthropic Authenticode
  signature) and writes managed settings so the CLI logs in to your
  Claude apps gateway and never attempts self-update.

  Works with or without elevation. Everything installs to user scope
  (%USERPROFILE%\.local\bin + user PATH); managed settings are written to
  %ProgramData%\ClaudeCode\managed-settings.json when elevated
  (tamper-resistant, recommended for MDM), or to the per-user managed
  policy source HKCU\SOFTWARE\Policies\ClaudeCode when not. Both are
  honored by Claude Code as managed settings; the HKCU source is
  user-writable, so treat it as a convenience default rather than an
  enforcement channel.

.PARAMETER BinaryPath
  Path to the downloaded claude.exe (win32-x64 build from the release bucket).

.PARAMETER Sha256
  Expected SHA-256 (platforms.'win32-x64'.checksum from the release's
  manifest.json). Install aborts on mismatch.

.PARAMETER GatewayUrl
  Your gateway URL (e.g. https://claude-gateway.example.com). When set, writes
  forceLoginMethod/forceLoginGatewayUrl into managed settings — the
  %ProgramData% managed-settings.json when elevated, the HKCU policy
  registry key otherwise. These keys are managed-only and are NOT honored
  from a user settings.json, which is why the registry source is used.

.PARAMETER DisableUpdates
  Adds DISABLE_UPDATES=1 and DISABLE_AUTOUPDATER=1 to the managed settings
  env block. DISABLE_UPDATES blocks every update path - background checks
  AND manual 'claude update' / 'claude install' - which is what keeps
  users on the version you distribute. DISABLE_AUTOUPDATER (background
  check only) is set alongside as defense in depth.

.PARAMETER CostCenter
  Optional cost-center tag stamped onto all telemetry this workstation
  emits (OTEL_RESOURCE_ATTRIBUTES). Shows up as the 'cost_center' label in
  the usage dashboard. No spaces or commas (use underscores).

.PARAMETER Team
  Optional team tag, same mechanism as CostCenter ('team' label in the
  dashboard). Telemetry itself is enabled centrally by the gateway (it
  pushes the OTLP env vars to every connected client) - these parameters
  only add the grouping attributes.

.PARAMETER RequiredMinimumVersion
  Managed-settings floor; the CLI refuses to start below it. The Claude apps
  gateway requires 2.1.195+.

.PARAMETER SignerThumbprint
  Optional SHA-1 thumbprint of Anthropic's Authenticode signing certificate.
  When set, the signer must match it exactly (stronger than the default
  subject-name check). Read it once from a known-good binary:
  (Get-AuthenticodeSignature claude.exe).SignerCertificate.Thumbprint

.PARAMETER ExtraCaCertPath
  Optional path to a PEM bundle of your enterprise root/intermediate CAs.
  Written into the managed env block as NODE_EXTRA_CA_CERTS - the
  precompiled claude.exe honors it, covering environments where the binary
  does not consult the Windows certificate store for the gateway's
  enterprise-CA TLS chain. Use a local path that exists on every laptop
  (deploy the PEM alongside the binary), not a UNC path.

.PARAMETER SettingsOnly
  Write managed settings only - skip binary install, PATH, and smoke test.
  This is the supported mode for SYSTEM-context pushes (Intune/SCCM device
  context): a SYSTEM run would otherwise install claude.exe into SYSTEM's
  own %USERPROFILE% and PATH, where developers never see it. Two-phase
  rollout: push settings as SYSTEM with -SettingsOnly (lands in
  %ProgramData%, tamper-resistant), and deploy the binary in USER context
  (Intune "user" install behavior, or manual).

.EXAMPLE
  .\Install-ClaudeCode.ps1 -BinaryPath \\fileserver\software\claude\2.1.207\claude.exe `
      -Sha256 3f1c... -GatewayUrl https://claude-gateway.example.com -DisableUpdates
#>
[CmdletBinding(SupportsShouldProcess)]
param(
  [string]$BinaryPath,
  [string]$Sha256,
  [string]$GatewayUrl,
  [switch]$DisableUpdates,
  [ValidatePattern('^[^,\s]*$')][string]$CostCenter,
  [ValidatePattern('^[^,\s]*$')][string]$Team,
  [string]$RequiredMinimumVersion = '2.1.195',
  [string]$SignerThumbprint,
  [string]$ExtraCaCertPath,
  [switch]$SettingsOnly,
  [switch]$SkipSignatureCheck
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }

# Assemble the managed-settings object (gateway login + update lockdown +
# telemetry attributes + enterprise CA trust). Pure: returns an ordered
# hashtable, or $null when there is nothing to write. Kept as a function so
# it can be unit-tested (see tests/powershell/).
function Build-ManagedSettings {
  param(
    [string]$GatewayUrl,
    [switch]$DisableUpdates,
    [string]$CostCenter,
    [string]$Team,
    [string]$ExtraCaCertPath,
    [string]$RequiredMinimumVersion
  )
  if (-not ($GatewayUrl -or $DisableUpdates -or $CostCenter -or $Team -or $ExtraCaCertPath)) {
    return $null
  }
  $settings = [ordered]@{}
  if ($GatewayUrl) {
    $settings['forceLoginMethod']     = 'gateway'
    $settings['forceLoginGatewayUrl'] = $GatewayUrl
  }
  if ($RequiredMinimumVersion) { $settings['requiredMinimumVersion'] = $RequiredMinimumVersion }
  $envBlock = [ordered]@{}
  if ($DisableUpdates) {
    # DISABLE_UPDATES blocks ALL update paths (background + manual
    # 'claude update' / 'claude install') - required for self-distributed
    # pinned versions. DISABLE_AUTOUPDATER (background check only) is
    # added as defense in depth. See code.claude.com/docs/en/setup.
    $envBlock['DISABLE_UPDATES']     = '1'
    $envBlock['DISABLE_AUTOUPDATER'] = '1'
  }
  if ($CostCenter -or $Team) {
    # Resource attributes stamped onto all OTLP telemetry (the gateway
    # enables and routes telemetry itself; these are grouping labels).
    $attrs = @()
    if ($CostCenter) { $attrs += "cost_center=$CostCenter" }
    if ($Team)       { $attrs += "team=$Team" }
    $envBlock['OTEL_RESOURCE_ATTRIBUTES'] = ($attrs -join ',')
  }
  if ($ExtraCaCertPath) {
    # Enterprise CA trust for the gateway's TLS chain; the precompiled
    # claude.exe honors NODE_EXTRA_CA_CERTS.
    $envBlock['NODE_EXTRA_CA_CERTS'] = $ExtraCaCertPath
  }
  if ($envBlock.Count -gt 0) { $settings['env'] = $envBlock }
  return $settings
}

# Tests dot-source this file for the functions above without running the
# installer body.
if ($env:CLAUDE_INSTALLER_DOTSOURCE) { return }

# --- 0. Preconditions -------------------------------------------------------
# A SYSTEM-context run (Intune/SCCM device push) would install the binary
# into SYSTEM's own profile and PATH - developers would never get claude.exe.
# SYSTEM is only supported for the settings phase (-SettingsOnly).
$isSystem = [Security.Principal.WindowsIdentity]::GetCurrent().IsSystem
if ($isSystem -and -not $SettingsOnly) {
  throw ('Running as SYSTEM without -SettingsOnly. Push managed settings as SYSTEM with ' +
         '-SettingsOnly, and deploy the binary in USER context (Intune "user" install ' +
         'behavior) - a SYSTEM install lands in SYSTEM''s %USERPROFILE%, not the developer''s.')
}

if (-not $SettingsOnly) {
  if (-not $BinaryPath) { throw 'BinaryPath is required (omit it only with -SettingsOnly).' }
  if (-not (Test-Path -LiteralPath $BinaryPath -PathType Leaf)) {
    throw "Binary not found: $BinaryPath"
  }
}
if ($ExtraCaCertPath -and -not (Test-Path -LiteralPath $ExtraCaCertPath -PathType Leaf)) {
  throw "ExtraCaCertPath not found: $ExtraCaCertPath"
}

if (-not $SettingsOnly -and $WhatIfPreference) {
  # Under -WhatIf the file cmdlets below are suppressed, so the staged copy
  # would never exist and verification would die on a missing file. Describe
  # the would-be actions instead and fall through to the settings phase
  # (whose registry/file cmdlets honor -WhatIf natively).
  Write-Step ("WhatIf: would stage {0} to TEMP, verify SHA-256/Authenticode, install to {1}, and add that directory to the user PATH" -f `
    $BinaryPath, (Join-Path $env:USERPROFILE '.local\bin\claude.exe'))
} elseif (-not $SettingsOnly) {

# Elevated interactive runs (UAC with helpdesk/admin credentials) install
# into the ELEVATED account's profile - the developer never sees claude.exe.
$elevatedNow = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
               ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($elevatedNow) {
  Write-Warning ("Running elevated: the binary installs into THIS account's profile ({0}) and user PATH. " -f $env:USERPROFILE)
  Write-Warning 'If these are not the developer''s credentials, run non-elevated as the developer (or use -SettingsOnly for the managed-settings push).'
}

# --- 1. Stage locally, then verify the LOCAL copy ---------------------------
# Verifying at $BinaryPath (a network share) and copying afterwards is a
# time-of-check/time-of-use hole - a writer on the share could swap the file
# between the two steps. Everything below operates on this local staging copy.
$staged = Join-Path $env:TEMP ("claude-install-{0}.exe" -f [guid]::NewGuid())
Write-Step "Staging $BinaryPath locally"
Copy-Item -LiteralPath $BinaryPath -Destination $staged
try {
  if ($Sha256) {
    Write-Step 'Verifying SHA-256 against manifest value'
    $actual = (Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $Sha256.ToLower()) {
      throw "SHA-256 mismatch. expected=$($Sha256.ToLower()) actual=$actual - refusing to install."
    }
    Write-Host "    checksum OK ($actual)"
  }

  if (-not $SkipSignatureCheck) {
    Write-Step 'Verifying Authenticode signature'
    $sig = Get-AuthenticodeSignature -LiteralPath $staged
    if ($sig.Status -ne 'Valid') {
      throw "Authenticode status is '$($sig.Status)' (expected Valid). Use -SkipSignatureCheck only if your endpoint agent strips signatures."
    }
    if ($SignerThumbprint) {
      if ($sig.SignerCertificate.Thumbprint -ne $SignerThumbprint.ToUpper().Replace(' ', '')) {
        throw "Signer thumbprint $($sig.SignerCertificate.Thumbprint) does not match pinned $SignerThumbprint"
      }
      Write-Host "    signer thumbprint pinned OK"
    } elseif ($sig.SignerCertificate.Subject -notmatch '(^|[,"\s])CN="?Anthropic') {
      throw "Unexpected signer: $($sig.SignerCertificate.Subject)"
    }
    Write-Host "    signed by: $($sig.SignerCertificate.Subject)"
  }

  # --- 2. Install to %USERPROFILE%\.local\bin -------------------------------
  # Same location the native installer manages, so a future move to the online
  # installer or auto-updates needs no path changes.
  $installDir = Join-Path $env:USERPROFILE '.local\bin'
  $target     = Join-Path $installDir 'claude.exe'

  Write-Step "Installing to $target"
  New-Item -ItemType Directory -Path $installDir -Force | Out-Null

  if (Test-Path -LiteralPath $target) {
    # Windows locks running executables; stop any running instance first.
    $running = Get-Process -Name 'claude' -ErrorAction SilentlyContinue
    if ($running) { throw 'claude.exe is currently running - close it and re-run the installer.' }
  }
  # Move the verified staging copy into place (same volume rename when TEMP
  # and the profile share a volume; never re-reads the share post-verify).
  Move-Item -LiteralPath $staged -Destination $target -Force
  Unblock-File -LiteralPath $target -ErrorAction SilentlyContinue
} finally {
  if (Test-Path -LiteralPath $staged) { Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue }
}

# --- 3. Add to the user PATH (registry-backed, persists) --------------------
Write-Step 'Ensuring install directory is on the user PATH'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$onPath = ($userPath -split ';' | Where-Object { $_ } |
           ForEach-Object { $_.TrimEnd('\') }) -contains $installDir.TrimEnd('\')
if (-not $onPath) {
  $newPath = if ([string]::IsNullOrEmpty($userPath)) { $installDir } else { "$userPath;$installDir" }
  [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
  Write-Host '    added (new terminals pick it up; existing terminals need a restart)'
} else {
  Write-Host '    already present'
}
# Make it usable in THIS session too:
if (($env:Path -split ';') -notcontains $installDir) { $env:Path += ";$installDir" }

}  # end -not $SettingsOnly

# --- 4. Managed settings (gateway login + update lockdown) ------------------
# Elevated:     %ProgramData%\ClaudeCode\managed-settings.json (tamper-resistant)
# Non-elevated: HKCU\SOFTWARE\Policies\ClaudeCode, REG_SZ value 'Settings'
#               holding single-line JSON — a per-user managed-settings source
#               Claude Code honors without elevation. forceLoginMethod /
#               forceLoginGatewayUrl / requiredMinimumVersion are managed-only
#               keys, so a plain user settings.json would NOT work here.
$settings = Build-ManagedSettings -GatewayUrl $GatewayUrl -DisableUpdates:$DisableUpdates `
  -CostCenter $CostCenter -Team $Team -ExtraCaCertPath $ExtraCaCertPath `
  -RequiredMinimumVersion $RequiredMinimumVersion
if ($settings) {
  $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
             ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

  if ($isAdmin) {
    $settingsDir  = Join-Path $env:ProgramData 'ClaudeCode'
    $settingsPath = Join-Path $settingsDir 'managed-settings.json'
    Write-Step "Writing $settingsPath"
    New-Item -ItemType Directory -Path $settingsDir -Force | Out-Null
    $settings | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $settingsPath -Encoding UTF8
  } else {
    $policyKey = 'HKCU:\SOFTWARE\Policies\ClaudeCode'
    Write-Step "Not elevated - writing per-user managed policy to $policyKey"
    New-Item -Path $policyKey -Force | Out-Null

    # Merge with any existing policy JSON so repeated runs / other tooling
    # don't lose keys we didn't set this time.
    $merged = [ordered]@{}
    $existing = (Get-ItemProperty -Path $policyKey -Name 'Settings' -ErrorAction SilentlyContinue).Settings
    if ($existing) {
      try {
        ($existing | ConvertFrom-Json).PSObject.Properties |
          ForEach-Object { $merged[$_.Name] = $_.Value }
      } catch {
        Write-Warning "    existing Settings value is not valid JSON - replacing it."
      }
    }
    foreach ($k in $settings.Keys) { $merged[$k] = $settings[$k] }

    Set-ItemProperty -Path $policyKey -Name 'Settings' -Type String `
      -Value ($merged | ConvertTo-Json -Depth 4 -Compress)
    Write-Host '    note: the HKCU policy source is user-writable (convenience, not enforcement).'
    Write-Host '    For tamper-resistant settings, deploy via MDM/Intune elevated instead.'
  }
}

# --- 5. Smoke test -----------------------------------------------------------
if ($SettingsOnly) {
  Write-Host 'Done (settings only). Deploy the binary in user context separately.' -ForegroundColor Green
} elseif ($WhatIfPreference) {
  Write-Host 'Done (WhatIf) - no files or settings were changed.' -ForegroundColor Green
} else {
  Write-Step 'Verifying installation'
  $version = & $target --version
  Write-Host "    claude --version -> $version"
  Write-Host ''
  Write-Host 'Done. Developer next step: open a NEW terminal and run: claude  (then /login -> Cloud gateway)' -ForegroundColor Green
}
