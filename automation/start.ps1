param([string]$Root=$PSScriptRoot)
$ErrorActionPreference='Stop'
& (Join-Path $Root 'recovery-control.ps1') -Root $Root -Action Resume
exit $LASTEXITCODE
