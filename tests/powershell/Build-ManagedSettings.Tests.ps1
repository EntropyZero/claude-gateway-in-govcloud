# Pester tests for the managed-settings assembly in Install-ClaudeCode.ps1.
# Dot-sources the installer (guarded by CLAUDE_INSTALLER_DOTSOURCE so the
# install body does not run) and exercises Build-ManagedSettings.

BeforeAll {
  $env:CLAUDE_INSTALLER_DOTSOURCE = '1'
  . "$PSScriptRoot/../../client/Install-ClaudeCode.ps1"
}

Describe 'Build-ManagedSettings' {

  It 'returns $null when nothing is requested' {
    Build-ManagedSettings | Should -BeNullOrEmpty
  }

  It 'writes gateway login keys when a URL is given' {
    $s = Build-ManagedSettings -GatewayUrl 'https://gw.example.com' -RequiredMinimumVersion '2.1.195'
    $s['forceLoginMethod']       | Should -Be 'gateway'
    $s['forceLoginGatewayUrl']   | Should -Be 'https://gw.example.com'
    $s['requiredMinimumVersion'] | Should -Be '2.1.195'
  }

  It 'sets both update-lockdown vars together' {
    $s = Build-ManagedSettings -GatewayUrl 'https://gw' -DisableUpdates
    $s['env']['DISABLE_UPDATES']     | Should -Be '1'
    $s['env']['DISABLE_AUTOUPDATER'] | Should -Be '1'
  }

  It 'does not add an env block when only a gateway URL is set' {
    $s = Build-ManagedSettings -GatewayUrl 'https://gw'
    $s.Contains('env') | Should -BeFalse
  }

  It 'builds OTEL_RESOURCE_ATTRIBUTES from cost center and team' {
    $s = Build-ManagedSettings -CostCenter 'CC-42' -Team 'platform'
    $s['env']['OTEL_RESOURCE_ATTRIBUTES'] | Should -Be 'cost_center=CC-42,team=platform'
  }

  It 'includes only the attribute that was provided' {
    $s = Build-ManagedSettings -Team 'platform'
    $s['env']['OTEL_RESOURCE_ATTRIBUTES'] | Should -Be 'team=platform'
  }

  It 'maps ExtraCaCertPath to NODE_EXTRA_CA_CERTS' {
    $s = Build-ManagedSettings -ExtraCaCertPath 'C:\certs\corp-ca.pem'
    $s['env']['NODE_EXTRA_CA_CERTS'] | Should -Be 'C:\certs\corp-ca.pem'
  }

  It 'produces valid JSON with the expected nesting' {
    $s = Build-ManagedSettings -GatewayUrl 'https://gw' -DisableUpdates -Team 'plat' -ExtraCaCertPath 'C:\ca.pem'
    $json = $s | ConvertTo-Json -Depth 4
    $round = $json | ConvertFrom-Json
    $round.forceLoginMethod          | Should -Be 'gateway'
    $round.env.DISABLE_UPDATES       | Should -Be '1'
    $round.env.NODE_EXTRA_CA_CERTS   | Should -Be 'C:\ca.pem'
    $round.env.OTEL_RESOURCE_ATTRIBUTES | Should -Be 'team=plat'
  }

  It 'ExtraCaCertPath alone still triggers a settings object (regression)' {
    # -ExtraCaCertPath must be in the "anything to write?" guard.
    Build-ManagedSettings -ExtraCaCertPath 'C:\ca.pem' | Should -Not -BeNullOrEmpty
  }
}

Describe 'Write-ManagedSettings' {
  # The user (HKCU\SOFTWARE\Policies) branch is Windows-only - there is no
  # registry provider on Linux CI to write or mock reliably. Its try/catch
  # shape is identical to the machine branch tested here, which uses the
  # filesystem and IS exercisable cross-platform, so these cover the behavior
  # change: a successful write reports Applied, and a DENIED write degrades to
  # Applied=$false WITHOUT throwing (the hardened-fleet fix).

  It 'writes machine-wide settings to %ProgramFiles% and reports Applied on success' {
    $env:ProgramFiles = Join-Path $TestDrive 'pf-ok'
    $r = Write-ManagedSettings -Settings ([ordered]@{ forceLoginMethod = 'gateway' }) -Elevated
    $r.Applied | Should -BeTrue
    $r.Scope   | Should -Be 'machine'
    $r.Location | Should -Match 'ClaudeCode'   # under %ProgramFiles%\ClaudeCode
    Test-Path $r.Location | Should -BeTrue
    (Get-Content -Raw $r.Location | ConvertFrom-Json).forceLoginMethod | Should -Be 'gateway'
  }

  It 'does NOT throw and reports Applied=$false when the store write is denied' {
    # Put a DIRECTORY where the settings file must go, so the write cannot
    # create the file - the deterministic stand-in for the hardened-machine
    # "Access is denied". The install must survive this, not abort.
    $env:ProgramFiles = Join-Path $TestDrive 'pf-blocked'
    $blockPath = Join-Path (Join-Path $env:ProgramFiles 'ClaudeCode') 'managed-settings.json'
    New-Item -ItemType Directory -Force -Path $blockPath | Out-Null
    # Capture directly: if it threw (the bug this fixes), the test fails here.
    $r = Write-ManagedSettings -Settings ([ordered]@{ forceLoginMethod = 'gateway' }) -Elevated
    $r.Applied | Should -BeFalse
    $r.Error   | Should -Not -BeNullOrEmpty
  }
}

Describe 'Installer parameter validation' {

  It 'rejects a CostCenter containing a comma' {
    $env:CLAUDE_INSTALLER_DOTSOURCE = $null
    { & "$PSScriptRoot/../../client/Install-ClaudeCode.ps1" -SettingsOnly -CostCenter 'a,b' } |
      Should -Throw
    $env:CLAUDE_INSTALLER_DOTSOURCE = '1'
  }
}
