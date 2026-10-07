$ErrorActionPreference='Stop'
$Root='C:\SyntheticRuntime\automation'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts\adopt.ps1'),[ref]$tokens,[ref]$errors)
if ($errors) {throw 'Adoption script parse errors'}
$definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Wait-OldProcesses'},$true)
. ([scriptblock]::Create($definition.Extent.Text))
$global:drainMode='transient';$global:drainCalls=0
function Get-CimInstance {
 $global:drainCalls++
 if ($global:drainMode -eq 'unavailable' -or ($global:drainMode -eq 'transient' -and $global:drainCalls -eq 1)) {throw 'Synthetic provider unavailable'}
 if ($global:drainMode -eq 'active' -and $global:drainCalls -eq 1) {[pscustomobject]@{CommandLine='pythonw.exe "C:\SyntheticRuntime\automation\bridge.py" --loop'}}
}
foreach ($mode in @('transient','active')) {
 $global:drainMode=$mode;$global:drainCalls=0
 Wait-OldProcesses -TimeoutSeconds 2
 if ($global:drainCalls -ne 2) {throw 'Drain did not retry unknown/wait for active writer'}
}
$global:drainMode='unavailable';$global:drainCalls=0;$refused=$false
try {Wait-OldProcesses -TimeoutSeconds 1} catch {$refused=$_.Exception.Message -match 'inspection unavailable'}
if (!$refused) {throw 'Persistent unknown process state did not refuse'}
'PASS: transient CIM retry, active writer drain, persistent unknown refusal'
