param(
    [string]$OutputPath = "windows-v0522-git-write-acceptance.log"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$replacementRunner = Join-Path $PSScriptRoot "run_v0522_windows_git_write_acceptance.ps1"
if (-not (Test-Path -LiteralPath $replacementRunner -PathType Leaf)) {
    throw "The repaired v0.5.2.2 acceptance runner is missing: $replacementRunner"
}

Write-Warning "run_v0521_windows_git_write_acceptance.ps1 is retained only as a compatibility entry point. Running the repaired v0.5.2.2 gate."
& $replacementRunner -OutputPath $OutputPath
