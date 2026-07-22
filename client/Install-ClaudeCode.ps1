<#
.SYNOPSIS
  Offline installer for Claude Code on Windows - no calls to claude.ai or
  downloads.claude.ai, and NO administrator rights required. Takes an
  already-downloaded claude.exe (mirrored from
  downloads.claude.ai/claude-code-releases/<version>/ and verified against the
  GPG-signed manifest), places it in %USERPROFILE%\.local\bin, and adds that
  directory to the user PATH.

.DESCRIPTION
  Everything this installer does is user-scope by design:

  - the binary installs to %USERPROFILE%\.local\bin (+ user PATH),
  - workstation configuration (telemetry attributes, update lockdown,
    enterprise CA trust) is written as an 'env' block in the USER settings
    file %USERPROFILE%\.claude\settings.json,
  - gateway sign-in is interactive: run 'claude', then /login, choose
    "Cloud gateway", and paste the gateway URL (printed at the end of the
    install). Central policy is applied by the GATEWAY after login via its
    /managed/settings endpoint.

  This script deliberately writes NO machine-wide or policy-source settings
  (no %ProgramFiles%\ClaudeCode\managed-settings.json, no
  HKx\SOFTWARE\Policies\ClaudeCode): on hardened fleets those locations
  require administrator rights, and enforcement belongs to an admin channel.
  If/when the organization wants FORCED gateway login
  (forceLoginMethod/forceLoginGatewayUrl/requiredMinimumVersion - keys Claude
  Code honors only from managed sources), push them via GPO/MDM as documented
  in docs/client-config.md. The two channels compose: this installer for the
  binary + user config, GPO for enforcement.

.PARAMETER BinaryPath
  Path to the downloaded claude.exe (win32-x64 build from the release bucket).

.PARAMETER Sha256
  Expected SHA-256 (platforms.'win32-x64'.checksum from the release's
  manifest.json). Install aborts on mismatch.

.PARAMETER GatewayUrl
  Your gateway URL (e.g. https://claude-gateway.example.com). Printed in the
  sign-in instructions at the end of the install ('claude' -> /login ->
  Cloud gateway -> paste this URL). Not written anywhere: the pre-fill/lock
  keys are managed-only and belong to the GPO/MDM channel
  (docs/client-config.md).

.PARAMETER DisableUpdates
  Adds DISABLE_UPDATES=1 and DISABLE_AUTOUPDATER=1 to the user settings env
  block. DISABLE_UPDATES blocks every update path - background checks AND
  manual 'claude update' / 'claude install' - which is what keeps users on
  the version you distribute. User-scope is a convenience, not enforcement:
  the mirror-only network path (downloads.claude.ai unreachable) is the real
  control, and the gateway can push the same lockdown centrally
  (MANAGED_CLI_GROUPS in deploy.env).

.PARAMETER CostCenter
  Optional cost-center tag stamped onto all telemetry this workstation
  emits (OTEL_RESOURCE_ATTRIBUTES). Shows up as the 'cost_center' label in
  the usage dashboard. No spaces or commas (use underscores).

.PARAMETER Team
  Optional team tag, same mechanism as CostCenter ('team' label in the
  dashboard). Telemetry itself is enabled centrally by the gateway (it
  pushes the OTLP env vars to every connected client) - these parameters
  only add the grouping attributes.

.PARAMETER SignerThumbprint
  Optional SHA-1 thumbprint of Anthropic's Authenticode signing certificate.
  When set, the signer must match it exactly (stronger than the default
  subject-name check). Read it once from a known-good binary:
  (Get-AuthenticodeSignature claude.exe).SignerCertificate.Thumbprint

.PARAMETER ExtraCaCertPath
  Optional path to a PEM bundle of your enterprise root/intermediate CAs.
  Written into the user settings env block as NODE_EXTRA_CA_CERTS - the
  precompiled claude.exe honors it, covering environments where the binary
  does not consult the Windows certificate store for the gateway's
  enterprise-CA TLS chain. Use a local path that exists on every laptop
  (deploy the PEM alongside the binary), not a UNC path.

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
  [string]$SignerThumbprint,
  [string]$ExtraCaCertPath,
  [switch]$SkipSignatureCheck
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }

# Assemble the user-scope env configuration (update lockdown + telemetry
# attributes + enterprise CA trust). Pure: returns an ordered hashtable, or
# $null when there is nothing to write. These are ordinary env vars, honored
# from the USER settings file - unlike forceLoginMethod /
# forceLoginGatewayUrl / requiredMinimumVersion, which Claude Code accepts
# only from managed sources (GPO/MDM - see docs/client-config.md) and which
# this installer therefore does not attempt. Kept as a function so it can be
# unit-tested (see tests/powershell/).
function Build-UserEnv {
  param(
    [switch]$DisableUpdates,
    [string]$CostCenter,
    [string]$Team,
    [string]$ExtraCaCertPath
  )
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
  if ($envBlock.Count -eq 0) { return $null }
  return $envBlock
}

# Merge an env map into %USERPROFILE%\.claude\settings.json (the user's own
# file - no elevation involved) and REPORT the outcome instead of throwing.
# Preserves every existing top-level key and every existing env key we are
# not setting; refuses to overwrite a settings.json it cannot parse (the
# user's file, possibly hand-edited - never clobber it). Returns a result
# object { Applied; Location; Error }.
function Write-UserSettings {
  param(
    [Parameter(Mandatory)] $EnvMap,
    [string]$SettingsPath
  )
  if (-not $SettingsPath) {
    $SettingsPath = Join-Path (Join-Path $env:USERPROFILE '.claude') 'settings.json'
  }
  # The .NET write below resolves relative paths against the PROCESS cwd,
  # not PowerShell's $PWD - anchor explicitly so they can't diverge.
  if (-not [System.IO.Path]::IsPathRooted($SettingsPath)) {
    $SettingsPath = Join-Path (Get-Location).Path $SettingsPath
  }
  try {
    $merged = [ordered]@{}
    if (Test-Path -LiteralPath $SettingsPath -PathType Leaf) {
      $raw = Get-Content -LiteralPath $SettingsPath -Raw -ErrorAction Stop
      if ($raw -and $raw.Trim()) {
        try {
          ($raw | ConvertFrom-Json -ErrorAction Stop).PSObject.Properties |
            ForEach-Object { $merged[$_.Name] = $_.Value }
        } catch {
          # Never destroy a file we cannot parse - report and let the user fix it.
          return [pscustomobject]@{ Applied = $false; Location = $SettingsPath;
            Error = "existing settings.json is not valid JSON - fix or remove it, then re-run ($($_.Exception.Message))" }
        }
      }
    }
    # Merge our env keys over the existing env object (if any), preserving
    # unrelated env vars the user or other tooling set.
    $envMerged = [ordered]@{}
    if ($merged.Contains('env') -and $merged['env']) {
      $merged['env'].PSObject.Properties | ForEach-Object { $envMerged[$_.Name] = $_.Value }
    }
    foreach ($k in $EnvMap.Keys) { $envMerged[$k] = $EnvMap[$k] }
    $merged['env'] = $envMerged

    if ($WhatIfPreference) {
      # The BOM-less .NET write below does not honor -WhatIf, so skip it
      # explicitly (mirrors what the file cmdlets would have done).
      return [pscustomobject]@{ Applied = $true; Location = $SettingsPath; Error = $null }
    }
    $dir = Split-Path -Parent $SettingsPath
    New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null
    # UTF-8 WITHOUT a BOM: on Windows PowerShell 5.1 'Set-Content -Encoding
    # UTF8' writes a BOM, and Claude Code's JSON reader REJECTS a BOM'd
    # settings.json (upstream: claude-code#9906, closed not-planned). The
    # portal's install.cmd runs this script under 5.1, so a BOM here would
    # silently break every install - and corrupt a previously-working file
    # claude itself wrote BOM-less.
    [System.IO.File]::WriteAllText(
      $SettingsPath,
      (($merged | ConvertTo-Json -Depth 8) + [Environment]::NewLine),
      (New-Object System.Text.UTF8Encoding($false)))
    return [pscustomobject]@{ Applied = $true; Location = $SettingsPath; Error = $null }
  } catch {
    $emsg = if ($_.Exception.Message) { $_.Exception.Message } else { "$_" }
    return [pscustomobject]@{ Applied = $false; Location = $SettingsPath; Error = $emsg }
  }
}

# Tests dot-source this file for the functions above without running the
# installer body.
if ($env:CLAUDE_INSTALLER_DOTSOURCE) { return }

# --- 0. Preconditions -------------------------------------------------------
# A SYSTEM-context run (Intune/SCCM device push) would install the binary
# into SYSTEM's own profile and PATH - developers would never get claude.exe.
# There is no settings mode to run as SYSTEM either: machine-wide policy is
# GPO/MDM's job (docs/client-config.md), not this installer's.
$isSystem = [Security.Principal.WindowsIdentity]::GetCurrent().IsSystem
if ($isSystem) {
  throw ('Running as SYSTEM. Deploy the binary in USER context (Intune "user" install ' +
         'behavior, or the download portal); push forced-login/managed policy via GPO/MDM ' +
         'instead - see docs/client-config.md.')
}

if (-not $BinaryPath) { throw 'BinaryPath is required.' }
if (-not (Test-Path -LiteralPath $BinaryPath -PathType Leaf)) {
  throw "Binary not found: $BinaryPath"
}
if ($ExtraCaCertPath -and -not (Test-Path -LiteralPath $ExtraCaCertPath -PathType Leaf)) {
  throw "ExtraCaCertPath not found: $ExtraCaCertPath"
}

if ($WhatIfPreference) {
  # Under -WhatIf the file cmdlets below are suppressed, so the staged copy
  # would never exist and verification would die on a missing file. Describe
  # the would-be actions instead and fall through to the settings phase
  # (whose file cmdlets honor -WhatIf natively).
  Write-Step ("WhatIf: would stage {0} to TEMP, verify SHA-256/Authenticode, install to {1}, and add that directory to the user PATH" -f `
    $BinaryPath, (Join-Path $env:USERPROFILE '.local\bin\claude.exe'))
} else {

# Elevated interactive runs (UAC with helpdesk/admin credentials) install
# into the ELEVATED account's profile - the developer never sees claude.exe.
# Elevation buys nothing here: everything is user-scope by design.
$elevatedNow = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
               ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($elevatedNow) {
  Write-Warning ("Running elevated: the binary installs into THIS account's profile ({0}) and user PATH. " -f $env:USERPROFILE)
  Write-Warning 'If these are not the developer''s credentials, run non-elevated as the developer.'
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

}  # end not-WhatIf

# --- 4. User-scope configuration (env block in settings.json) ---------------
# Ordinary env vars only - honored from the user's own settings file, no
# elevation, no policy keys. Enforcement (forced gateway login, version
# floor) is deliberately NOT attempted here; it belongs to GPO/MDM
# (docs/client-config.md) and to the gateway's own /managed/settings push.
$userEnv = Build-UserEnv -DisableUpdates:$DisableUpdates `
  -CostCenter $CostCenter -Team $Team -ExtraCaCertPath $ExtraCaCertPath
$script:SettingsResult = $null
if ($userEnv) {
  Write-Step 'Writing user configuration (%USERPROFILE%\.claude\settings.json env block)'
  $script:SettingsResult = Write-UserSettings -EnvMap $userEnv
  if ($script:SettingsResult.Applied) {
    Write-Host "    user settings updated: $($script:SettingsResult.Location)"
  } else {
    # Non-fatal: the binary is already installed and the gateway pushes
    # central config after login anyway.
    Write-Warning "User settings were NOT updated ($($script:SettingsResult.Location))."
    Write-Warning "  Reason: $($script:SettingsResult.Error)"
  }
}

# --- 5. Smoke test + sign-in instructions -----------------------------------
$settingsFailed = ($userEnv -and -not $script:SettingsResult.Applied)
if ($WhatIfPreference) {
  Write-Host 'Done (WhatIf) - no files or settings were changed.' -ForegroundColor Green
} else {
  Write-Step 'Verifying installation'
  $version = & $target --version
  Write-Host "    claude --version -> $version"
  Write-Host ''
  if ($settingsFailed) {
    Write-Host 'Binary installed; the user settings update failed (see warning above).' -ForegroundColor Yellow
  }
  Write-Host 'Done. Sign in to the gateway (one time):' -ForegroundColor Green
  Write-Host '  1. Open a NEW terminal and run:  claude'
  Write-Host '  2. Run /login and choose:  Cloud gateway'
  if ($GatewayUrl) {
    Write-Host "  3. Paste the gateway URL:  $GatewayUrl"
  } else {
    Write-Host '  3. Paste your gateway URL (ask your platform team).'
  }
  Write-Host '  Confirm the TLS fingerprint your IT team published when prompted.'
}
