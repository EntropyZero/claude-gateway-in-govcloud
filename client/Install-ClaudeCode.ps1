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

.PARAMETER RequiredMinimumVersion
  Managed-settings floor; the CLI refuses to start below it. The Claude apps
  gateway requires 2.1.195+.

.EXAMPLE
  .\Install-ClaudeCode.ps1 -BinaryPath \\fileserver\software\claude\2.1.207\claude.exe `
      -Sha256 3f1c... -GatewayUrl https://claude-gateway.example.com -DisableUpdates
#>
[CmdletBinding(SupportsShouldProcess)]
param(
  [Parameter(Mandatory)][string]$BinaryPath,
  [string]$Sha256,
  [string]$GatewayUrl,
  [switch]$DisableUpdates,
  [string]$RequiredMinimumVersion = '2.1.195',
  [switch]$SkipSignatureCheck
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }

# --- 0. Preconditions -------------------------------------------------------
if (-not (Test-Path -LiteralPath $BinaryPath -PathType Leaf)) {
  throw "Binary not found: $BinaryPath"
}

# --- 1. Integrity -----------------------------------------------------------
if ($Sha256) {
  Write-Step 'Verifying SHA-256 against manifest value'
  $actual = (Get-FileHash -LiteralPath $BinaryPath -Algorithm SHA256).Hash.ToLower()
  if ($actual -ne $Sha256.ToLower()) {
    throw "SHA-256 mismatch. expected=$($Sha256.ToLower()) actual=$actual - refusing to install."
  }
  Write-Host "    checksum OK ($actual)"
}

if (-not $SkipSignatureCheck) {
  Write-Step 'Verifying Authenticode signature'
  $sig = Get-AuthenticodeSignature -LiteralPath $BinaryPath
  if ($sig.Status -ne 'Valid') {
    throw "Authenticode status is '$($sig.Status)' (expected Valid). Use -SkipSignatureCheck only if your endpoint agent strips signatures."
  }
  if ($sig.SignerCertificate.Subject -notmatch 'Anthropic') {
    throw "Unexpected signer: $($sig.SignerCertificate.Subject)"
  }
  Write-Host "    signed by: $($sig.SignerCertificate.Subject)"
}

# --- 2. Install to %USERPROFILE%\.local\bin ---------------------------------
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
Copy-Item -LiteralPath $BinaryPath -Destination $target -Force
Unblock-File -LiteralPath $target -ErrorAction SilentlyContinue

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

# --- 4. Managed settings (gateway login + update lockdown) ------------------
# Elevated:     %ProgramData%\ClaudeCode\managed-settings.json (tamper-resistant)
# Non-elevated: HKCU\SOFTWARE\Policies\ClaudeCode, REG_SZ value 'Settings'
#               holding single-line JSON — a per-user managed-settings source
#               Claude Code honors without elevation. forceLoginMethod /
#               forceLoginGatewayUrl / requiredMinimumVersion are managed-only
#               keys, so a plain user settings.json would NOT work here.
if ($GatewayUrl -or $DisableUpdates) {
  $settings = [ordered]@{}
  if ($GatewayUrl) {
    $settings['forceLoginMethod']     = 'gateway'
    $settings['forceLoginGatewayUrl'] = $GatewayUrl
  }
  if ($RequiredMinimumVersion) { $settings['requiredMinimumVersion'] = $RequiredMinimumVersion }
  if ($DisableUpdates) {
    # DISABLE_UPDATES blocks ALL update paths (background + manual
    # 'claude update' / 'claude install') - required for self-distributed
    # pinned versions. DISABLE_AUTOUPDATER (background check only) is
    # added as defense in depth. See code.claude.com/docs/en/setup.
    $settings['env'] = [ordered]@{ DISABLE_UPDATES = '1'; DISABLE_AUTOUPDATER = '1' }
  }

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
Write-Step 'Verifying installation'
$version = & $target --version
Write-Host "    claude --version -> $version"
Write-Host ''
Write-Host 'Done. Developer next step: open a NEW terminal and run: claude  (then /login -> Cloud gateway)' -ForegroundColor Green
