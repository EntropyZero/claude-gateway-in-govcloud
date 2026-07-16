# Runs the Pester suite and exits non-zero on failure (for CI gating).
Import-Module Pester -MinimumVersion 5.0.0
$c = New-PesterConfiguration
$c.Run.Path = "$PSScriptRoot/powershell"
$c.Run.Exit = $true
$c.Output.Verbosity = 'Detailed'
Invoke-Pester -Configuration $c
