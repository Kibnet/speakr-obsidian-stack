param([string]$Root=(Join-Path $env:TEMP ('ollama-query-'+[guid]::NewGuid().ToString('N'))))
$ErrorActionPreference='Stop'
if (Test-Path -LiteralPath $Root) {throw 'Fresh isolated fixture required'}
New-Item -ItemType Directory -Path $Root | Out-Null
$package=Split-Path $PSScriptRoot -Parent
$exe=Join-Path $Root 'fixture.exe'
$marker=Join-Path $Root 'served.txt'
$source='using System;using System.IO;class Fixture {static void Main(){File.WriteAllText(@"'+$marker+'","served");Console.Error.WriteLine("normal native stderr");}}'
$src=Join-Path $Root 'fixture.cs'; $source | Set-Content -LiteralPath $src -Encoding UTF8
& (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe') /nologo /target:exe ('/out:'+$exe) $src
if ($LASTEXITCODE -ne 0) {throw 'Fixture compilation failed'}
$cfg=@{ollama=$exe;managed_ollama=$true;llm_port=19999;ollama_models=$Root;ollama_launcher=(Join-Path $Root 'start-ollama.ps1');ollama_tuning=@{}}
$cfg | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root 'config.json') -Encoding UTF8
$global:fixtureOllamaQuery='error';$global:fixtureOllamaStarted=$false
function Get-NetTCPConnection {
 if ($global:fixtureOllamaQuery -eq 'error') {throw 'fixture query unavailable'}
 if ($global:fixtureOllamaQuery -eq 'present') {[pscustomobject]@{LocalPort=19999;LocalAddress='127.0.0.1';OwningProcess=123}}
}
function Get-Process {[pscustomobject]@{Path=$exe}}
function Get-ScheduledTask {
 [pscustomobject]@{State='Ready';Actions=@([pscustomobject]@{Execute=(Get-Command powershell.exe).Source;Arguments=('-NoProfile -WindowStyle Hidden -File "'+$cfg.ollama_launcher+'" -Root "'+$Root+'"');WorkingDirectory=$Root})}
}
function Start-ScheduledTask {$global:fixtureOllamaStarted=$true}
foreach ($kind in @('launcher','control')) {
 foreach ($mode in @('error','present','empty')) {
  $global:fixtureOllamaQuery=$mode; $global:fixtureOllamaStarted=$false; $refused=$false
  try {
   if ($kind -eq 'launcher') {& (Join-Path $package 'automation\start-ollama.ps1') -Root $Root}
   else {& (Join-Path $package 'automation\runtime-control.ps1') -Root $Root -Action Start -Component ollama}
  } catch {if ($_.Exception.Message -notmatch 'fixture query unavailable') {throw}; $refused=$true}
  if (($mode -eq 'error') -ne $refused) {throw ('Unknown listener state did not refuse: '+$kind+'/'+$mode)}
  $launched=if ($kind -eq 'launcher') {Test-Path -LiteralPath $marker} else {$global:fixtureOllamaStarted}
  if ($launched -ne ($mode -eq 'empty')) {throw 'Listener presence/absence launch contract failed'}
 }
}
if ((Get-Content -Raw -LiteralPath (Join-Path $Root 'logs\ollama.log')) -notmatch 'normal native stderr') {throw 'PS5 native stderr lost'}
'PASS: launcher/control confirmed-empty, present, query-error and PS5 stderr'
