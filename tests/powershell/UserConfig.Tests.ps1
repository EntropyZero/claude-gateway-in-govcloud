# Pester tests for the user-scope configuration in Install-ClaudeCode.ps1.
# Dot-sources the installer (guarded by CLAUDE_INSTALLER_DOTSOURCE so the
# install body does not run) and exercises Build-UserEnv (pure) and
# Write-UserSettings (real filesystem via $TestDrive - the same code path a
# Windows install takes, since it is plain JSON file I/O, not registry).

BeforeAll {
  $env:CLAUDE_INSTALLER_DOTSOURCE = '1'
  . "$PSScriptRoot/../../client/Install-ClaudeCode.ps1"
}

Describe 'Build-UserEnv' {

  It 'returns $null when nothing is requested' {
    Build-UserEnv | Should -BeNullOrEmpty
  }

  It 'sets both update-lockdown vars together' {
    $e = Build-UserEnv -DisableUpdates
    $e['DISABLE_UPDATES']     | Should -Be '1'
    $e['DISABLE_AUTOUPDATER'] | Should -Be '1'
  }

  It 'builds OTEL_RESOURCE_ATTRIBUTES from cost center and team' {
    $e = Build-UserEnv -CostCenter 'CC-42' -Team 'platform'
    $e['OTEL_RESOURCE_ATTRIBUTES'] | Should -Be 'cost_center=CC-42,team=platform'
  }

  It 'includes only the attribute that was provided' {
    $e = Build-UserEnv -Team 'platform'
    $e['OTEL_RESOURCE_ATTRIBUTES'] | Should -Be 'team=platform'
  }

  It 'maps ExtraCaCertPath to NODE_EXTRA_CA_CERTS' {
    $e = Build-UserEnv -ExtraCaCertPath 'C:\certs\corp-ca.pem'
    $e['NODE_EXTRA_CA_CERTS'] | Should -Be 'C:\certs\corp-ca.pem'
  }

  It 'never emits managed-only keys (forceLogin*/requiredMinimumVersion)' {
    # Those keys are honored only from managed sources (GPO/MDM) - the
    # installer must not pretend to set them. Regression guard for the
    # no-admin redesign.
    $e = Build-UserEnv -DisableUpdates -Team 'plat' -ExtraCaCertPath 'C:\ca.pem'
    @($e.Keys) | Where-Object { $_ -match 'forceLogin|requiredMinimumVersion' } |
      Should -BeNullOrEmpty
  }
}

Describe 'Write-UserSettings' {

  It 'creates settings.json with the env block when none exists' {
    $path = Join-Path $TestDrive 'fresh/settings.json'
    $r = Write-UserSettings -EnvMap ([ordered]@{ DISABLE_UPDATES = '1' }) -SettingsPath $path
    $r.Applied | Should -BeTrue
    (Get-Content -Raw $path | ConvertFrom-Json).env.DISABLE_UPDATES | Should -Be '1'
  }

  It 'preserves existing top-level keys and unrelated env vars on merge' {
    $path = Join-Path $TestDrive 'merge/settings.json'
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    '{"model":"opus","env":{"FOO":"bar"},"permissions":{"allow":["Bash(ls:*)"]}}' |
      Set-Content -LiteralPath $path
    $r = Write-UserSettings -EnvMap ([ordered]@{ DISABLE_UPDATES = '1' }) -SettingsPath $path
    $r.Applied | Should -BeTrue
    $round = Get-Content -Raw $path | ConvertFrom-Json
    $round.model                 | Should -Be 'opus'
    $round.permissions.allow[0]  | Should -Be 'Bash(ls:*)'
    $round.env.FOO               | Should -Be 'bar'
    $round.env.DISABLE_UPDATES   | Should -Be '1'
  }

  It 'overwrites only the env keys it sets' {
    $path = Join-Path $TestDrive 'overwrite/settings.json'
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    '{"env":{"DISABLE_UPDATES":"0","KEEP":"me"}}' | Set-Content -LiteralPath $path
    Write-UserSettings -EnvMap ([ordered]@{ DISABLE_UPDATES = '1' }) -SettingsPath $path | Out-Null
    $round = Get-Content -Raw $path | ConvertFrom-Json
    $round.env.DISABLE_UPDATES | Should -Be '1'
    $round.env.KEEP            | Should -Be 'me'
  }

  It 'refuses to clobber an unparseable settings.json (Applied=$false, file intact)' {
    $path = Join-Path $TestDrive 'corrupt/settings.json'
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    '{ this is not json' | Set-Content -LiteralPath $path
    $r = Write-UserSettings -EnvMap ([ordered]@{ DISABLE_UPDATES = '1' }) -SettingsPath $path
    $r.Applied | Should -BeFalse
    $r.Error   | Should -Match 'not valid JSON'
    Get-Content -Raw $path | Should -Match 'this is not json'   # untouched
  }

  It 'does NOT throw and reports Applied=$false when the write is denied' {
    # A DIRECTORY where the file must go - deterministic stand-in for a
    # denied write. The install must degrade, not abort.
    $path = Join-Path $TestDrive 'blocked/settings.json'
    New-Item -ItemType Directory -Force -Path $path | Out-Null
    $r = Write-UserSettings -EnvMap ([ordered]@{ DISABLE_UPDATES = '1' }) -SettingsPath $path
    $r.Applied | Should -BeFalse
    $r.Error   | Should -Not -BeNullOrEmpty
  }

  It 'writes UTF-8 WITHOUT a BOM (claude.exe rejects BOM-d settings.json)' {
    # Windows PowerShell 5.1's Set-Content -Encoding UTF8 writes a BOM, which
    # Claude Code's JSON reader refuses (upstream claude-code#9906). The
    # installer must therefore use an explicit BOM-less writer - assert the
    # first bytes of the produced file are the JSON brace, not EF BB BF.
    $path = Join-Path $TestDrive 'bom/settings.json'
    Write-UserSettings -EnvMap ([ordered]@{ DISABLE_UPDATES = '1' }) -SettingsPath $path | Out-Null
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $bytes[0] | Should -Not -Be 0xEF
    $bytes[0] | Should -Be ([byte][char]'{')
  }

  It 'treats an empty existing file as fresh' {
    $path = Join-Path $TestDrive 'empty/settings.json'
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    '' | Set-Content -LiteralPath $path
    $r = Write-UserSettings -EnvMap ([ordered]@{ FOO = 'bar' }) -SettingsPath $path
    $r.Applied | Should -BeTrue
    (Get-Content -Raw $path | ConvertFrom-Json).env.FOO | Should -Be 'bar'
  }
}

Describe 'Set-OnboardingComplete' {

  It 'creates .claude.json with hasCompletedOnboarding true (boolean) when none exists' {
    $path = Join-Path $TestDrive 'fresh-state/.claude.json'
    $r = Set-OnboardingComplete -StatePath $path
    $r.Applied | Should -BeTrue
    $round = Get-Content -Raw $path | ConvertFrom-Json
    $round.hasCompletedOnboarding | Should -BeOfType [bool]
    $round.hasCompletedOnboarding | Should -BeTrue
  }

  It 'preserves existing keys and flips false to true' {
    $path = Join-Path $TestDrive 'flip/.claude.json'
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    '{"hasCompletedOnboarding":false,"numStartups":3,"projects":{"C:\\x":{"history":["a"]}}}' |
      Set-Content -LiteralPath $path
    $r = Set-OnboardingComplete -StatePath $path
    $r.Applied | Should -BeTrue
    $round = Get-Content -Raw $path | ConvertFrom-Json
    $round.hasCompletedOnboarding   | Should -BeTrue
    $round.numStartups              | Should -Be 3
    $round.projects.'C:\x'.history[0] | Should -Be 'a'
  }

  It 'leaves an already-true file byte-identical (no rewrite)' {
    $path = Join-Path $TestDrive 'noop/.claude.json'
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    # Distinctive formatting ConvertTo-Json would normalize - proves no rewrite.
    $original = '{"hasCompletedOnboarding": true,   "keep":"me"}'
    [System.IO.File]::WriteAllText($path, $original,
      (New-Object System.Text.UTF8Encoding($false)))
    $r = Set-OnboardingComplete -StatePath $path
    $r.Applied | Should -BeTrue
    [System.IO.File]::ReadAllText($path) | Should -Be $original
  }

  It 'refuses a non-object .claude.json (Applied=$false, file intact)' {
    # Without the object guard, the ARRAY's .NET properties (Length, Rank,
    # SyncRoot...) would be serialized over the file. The bash twin refuses
    # the same way.
    $path = Join-Path $TestDrive 'array-state/.claude.json'
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    '[1,2]' | Set-Content -LiteralPath $path
    $r = Set-OnboardingComplete -StatePath $path
    $r.Applied | Should -BeFalse
    $r.Error   | Should -Match 'not a JSON object'
    (Get-Content -Raw $path).Trim() | Should -Be '[1,2]'   # untouched
  }

  It 'rewrites a truthy-but-not-boolean flag ("true"/1) to boolean true' {
    # PowerShell's loose -eq would treat these as already-set and skip the
    # rewrite - but the client's startup gate needs boolean true, so a
    # hand-edited file with "true" must be fixed, not skipped.
    foreach ($bad in '"true"', '1') {
      $path = Join-Path $TestDrive "truthy-$($bad -replace '\W','')/.claude.json"
      New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
      "{`"hasCompletedOnboarding`":$bad}" | Set-Content -LiteralPath $path
      $r = Set-OnboardingComplete -StatePath $path
      $r.Applied | Should -BeTrue
      $round = Get-Content -Raw $path | ConvertFrom-Json
      $round.hasCompletedOnboarding | Should -BeOfType [bool]
      $round.hasCompletedOnboarding | Should -BeTrue
    }
  }

  It 'refuses to clobber an unparseable .claude.json (Applied=$false, file intact)' {
    $path = Join-Path $TestDrive 'corrupt-state/.claude.json'
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    '{ this is not json' | Set-Content -LiteralPath $path
    $r = Set-OnboardingComplete -StatePath $path
    $r.Applied | Should -BeFalse
    $r.Error   | Should -Match 'not valid JSON'
    Get-Content -Raw $path | Should -Match 'this is not json'   # untouched
  }

  It 'does NOT throw and reports Applied=$false when the write is denied' {
    # A DIRECTORY where the file must go - deterministic stand-in for a
    # denied write. The install must degrade, not abort.
    $path = Join-Path $TestDrive 'blocked-state/.claude.json'
    New-Item -ItemType Directory -Force -Path $path | Out-Null
    $r = Set-OnboardingComplete -StatePath $path
    $r.Applied | Should -BeFalse
    $r.Error   | Should -Not -BeNullOrEmpty
    Test-Path -LiteralPath "$path.tmp" | Should -BeFalse   # temp cleaned up
  }

  It 'writes UTF-8 WITHOUT a BOM (claude.exe rejects BOM-d JSON)' {
    $path = Join-Path $TestDrive 'bom-state/.claude.json'
    Set-OnboardingComplete -StatePath $path | Out-Null
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $bytes[0] | Should -Not -Be 0xEF
    $bytes[0] | Should -Be ([byte][char]'{')
  }

  It 'treats an empty existing file as fresh' {
    $path = Join-Path $TestDrive 'empty-state/.claude.json'
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    '' | Set-Content -LiteralPath $path
    $r = Set-OnboardingComplete -StatePath $path
    $r.Applied | Should -BeTrue
    (Get-Content -Raw $path | ConvertFrom-Json).hasCompletedOnboarding | Should -BeTrue
  }
}

Describe 'Installer parameter validation' {

  It 'rejects a CostCenter containing a comma' {
    $env:CLAUDE_INSTALLER_DOTSOURCE = $null
    { & "$PSScriptRoot/../../client/Install-ClaudeCode.ps1" -CostCenter 'a,b' } |
      Should -Throw
    $env:CLAUDE_INSTALLER_DOTSOURCE = '1'
  }
}
