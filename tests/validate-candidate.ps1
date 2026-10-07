param([string]$Root=(Join-Path $env:TEMP ('stack-'+[guid]::NewGuid().ToString('N'))),[string]$Python='python.exe')
$ErrorActionPreference='Stop'
$package=Split-Path $PSScriptRoot -Parent
$Python=(Get-Command $Python).Source
$resolved=[IO.Path]::GetFullPath($Root)
if (Test-Path -LiteralPath $resolved) { throw 'Fresh isolated fixture root required' }
& $Python (Join-Path $PSScriptRoot 'fixture.py') $resolved | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Fixture creation failed' }
$target=Join-Path $resolved 'runtime'
$bridge=Join-Path $target 'automation'
$cfg=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $bridge 'config.json') | ConvertFrom-Json
$install=Join-Path $package 'scripts\install.ps1'
$control=Join-Path $package 'automation\recovery-control.ps1'
$tasks=@($cfg.bridge_task,$cfg.watchdog_task)
try {
    # Existing task collision must preserve it and create no code/tasks.
    $action=New-ScheduledTaskAction -Execute $cfg.pythonw -Argument 'unowned'
    $principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive
    Register-ScheduledTask -TaskName $cfg.bridge_task -Action $action -Principal $principal | Out-Null
    $refused=$false
    try { & $install -TargetRoot $target -Candidate -Python $Python | Out-Null } catch { $refused=$_.Exception.Message -match 'collision' }
    if (!$refused -or (Test-Path -LiteralPath (Join-Path $bridge 'bridge.py'))) { throw 'Task collision was not fail-closed' }
    Unregister-ScheduledTask -TaskName $cfg.bridge_task -Confirm:$false
    foreach ($failure in @('BeforeCopy','MiddleCopy','AfterCopy','AfterConfigWrite','AfterBaseline','AfterBridgeTask','AfterWatchdogTask')) {
        $refused=$false
        try { & $install -TargetRoot $target -Candidate -Python $Python -InjectFailure $failure | Out-Null }
        catch { $refused=$_.Exception.Message -match 'Injected'; if (!$refused) { throw } }
        if (!$refused) { throw ('Injection failed: '+$failure) }
        foreach ($name in $tasks) { if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw 'Partial rollback left task' } }
        if (Test-Path -LiteralPath (Join-Path $bridge 'bridge.py')) { throw 'Partial rollback left code' }
    }
    # Separate process dies between config replacement and stack manifest read-back.
    & powershell.exe -NoProfile -File $install -TargetRoot $target -Candidate -Python $Python -InjectFailure CrashAfterConfigWrite
    if ($LASTEXITCODE -ne 77) { throw 'Expected candidate crash checkpoint missing' }
    & $install -TargetRoot $target -Candidate -Python $Python -Action Rollback | Out-Null
    if (Test-Path -LiteralPath (Join-Path $bridge 'bridge.py')) { throw 'Crash recovery left code' }
    & $install -TargetRoot $target -Candidate -Python $Python | Out-Null
    & $Python (Join-Path $PSScriptRoot 'verify_baseline.py') --root $bridge
    if ($LASTEXITCODE -ne 0) { throw 'Old source was not baselined without jobs' }
    # An interrupted upgrade must not clear maintenance or launch any task.
    $journal=Join-Path $bridge 'install-state.json'
    $journalBytes=[IO.File]::ReadAllBytes($journal)
    $pending=Get-Content -Raw -Encoding UTF8 -LiteralPath $journal | ConvertFrom-Json
    $pending | Add-Member -NotePropertyName upgradePending -NotePropertyValue $true -Force
    $pending | ConvertTo-Json -Depth 30 | Set-Content -Encoding UTF8 -LiteralPath $journal
    $pausedBytes=[Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $bridge 'maintenance.json')))
    $refused=$false
    try { & $control -Root $bridge -Action Resume | Out-Null } catch { $refused=$_.Exception.Message -match 'interrupted upgrade' }
    if (!$refused -or $pausedBytes -ne [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $bridge 'maintenance.json'))) -or (Test-Path -LiteralPath (Join-Path $bridge 'heartbeat.json'))) { throw 'Pending upgrade resumed runtime' }
    [IO.File]::WriteAllBytes($journal,$journalBytes)
    & $control -Root $bridge -Action Resume | Out-Null
    $deadline=[DateTime]::UtcNow.AddSeconds(30)
    do { Start-Sleep -Milliseconds 250 } while (!(Test-Path -LiteralPath (Join-Path $bridge 'heartbeat.json')) -and [DateTime]::UtcNow -lt $deadline)
    $h=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $bridge 'heartbeat.json') | ConvertFrom-Json
    if ([string](Get-Process -Id $h.pid).StartTime.ToFileTimeUtc() -ne [string]$h.start_marker) { throw 'Native heartbeat identity differs' }
    $refused=$false
    try { & (Join-Path $bridge 'runtime-control.ps1') -Action StopVerified -Root $bridge -BridgeTask $cfg.bridge_task -OllamaTask $cfg.ollama_task -Component bridge -ProcessId $h.pid -StartMarker $h.start_marker | Out-Null } catch { $refused=$true }
    if (!$refused -or !(Get-Process -Id $h.pid -ErrorAction SilentlyContinue)) { throw 'Fresh heartbeat did not protect worker' }
    & $control -Root $bridge -Action Pause | Out-Null
    Stop-ScheduledTask -TaskName $cfg.bridge_task
    $deadline=[DateTime]::UtcNow.AddSeconds(30)
    do { Start-Sleep -Milliseconds 250; $stopped=Get-ScheduledTask -TaskName $cfg.bridge_task } while ($stopped.State -eq 'Running' -and [DateTime]::UtcNow -lt $deadline)
    if ($stopped.State -eq 'Running') { throw 'Own fixture task did not stop' }
    & $control -Root $bridge -Action Resume | Out-Null
    $deadline=[DateTime]::UtcNow.AddSeconds(30)
    do { Start-Sleep -Milliseconds 250; $latest=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $bridge 'heartbeat.json') | ConvertFrom-Json } while ($latest.pid -eq $h.pid -and [DateTime]::UtcNow -lt $deadline)
    if ($latest.pid -eq $h.pid) { throw 'Resume did not restart stopped own worker' }
    & $control -Root $bridge -Action Pause | Out-Null
    $xml=Export-ScheduledTask -TaskName $cfg.watchdog_task
    if ($xml -notmatch 'LogonTrigger' -or $xml -notmatch '<Interval>PT1M</Interval>') { throw 'Autostart triggers incomplete' }
    $refused=$false
    try { & $install -TargetRoot $target -Candidate -Python $Python | Out-Null } catch { $refused=$_.Exception.Message -match 'Already owned' }
    if (!$refused) { throw 'Repeated Install did not refuse' }
    $config=Join-Path $bridge 'config.json'
    $bytes=[IO.File]::ReadAllBytes($config)
    Add-Content -Encoding UTF8 -LiteralPath $config -Value ' '
    $refused=$false
    try { & $install -TargetRoot $target -Candidate -Python $Python -Action Rollback | Out-Null } catch { $refused=$_.Exception.Message -match 'drift|conflict' }
    if (!$refused -or !(Test-Path -LiteralPath (Join-Path $bridge 'bridge.py'))) { throw 'Config drift caused partial rollback' }
    [IO.File]::WriteAllBytes($config,$bytes)
    # Upgrade fixture with one changed + one new module; commit failure must restore originals.
    & $control -Root $bridge -Action Pause | Out-Null
    $newPackage=Join-Path $resolved 'package-upgrade'
    New-Item -ItemType Directory -Path $newPackage | Out-Null
    foreach ($dir in @('automation','scripts','config')) { Copy-Item -LiteralPath (Join-Path $package $dir) -Destination $newPackage -Recurse }
    Add-Content -LiteralPath (Join-Path $newPackage 'automation\bridge.py') -Encoding UTF8 -Value '# synthetic upgrade fixture'
    'VALUE = 1' | Set-Content -LiteralPath (Join-Path $newPackage 'automation\fixture_module.py') -Encoding UTF8
    $upgradeInstaller=Join-Path $newPackage 'scripts\install.ps1'
    $originalBridgeHash=(Get-FileHash -LiteralPath (Join-Path $bridge 'bridge.py')).Hash
    Disable-ScheduledTask -TaskName $cfg.bridge_task | Out-Null
    $refused=$false
    try { & $upgradeInstaller -TargetRoot $target -Candidate -Python $Python -Action Upgrade -InjectFailure UpgradeCommit | Out-Null }
    catch { $refused=$_.Exception.Message -match 'Injected'; if (!$refused) { throw } }
    if (!$refused -or (Get-FileHash -LiteralPath (Join-Path $bridge 'bridge.py')).Hash -ne $originalBridgeHash -or (Test-Path -LiteralPath (Join-Path $bridge 'fixture_module.py'))) { throw 'Upgrade failed to restore original code' }
    & $upgradeInstaller -TargetRoot $target -Candidate -Python $Python -Action Upgrade | Out-Null
    if ((Get-ScheduledTask -TaskName $cfg.bridge_task).State -ne 'Disabled') { throw 'Upgrade resurrected disabled task' }
    if (!(Test-Path -LiteralPath (Join-Path $bridge 'fixture_module.py'))) { throw 'Upgrade did not install new module' }
    Enable-ScheduledTask -TaskName $cfg.bridge_task | Out-Null
    & $install -TargetRoot $target -Candidate -Python $Python -Action Rollback | Out-Null
    foreach ($name in $tasks) { if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw 'Rollback left own task' } }
    if (!(Test-Path -LiteralPath (Join-Path $bridge 'state.sqlite3'))) { throw 'Rollback removed queue progress' }
    @{status='PASS';collision=$true;partialRollback=$true;crashRecovery=$true;upgradeFailureRollback=$true;upgradePreservesDisabled=$true;baselineOldSource=$true;resume=$true;heartbeatIdentity=$true;freshHeartbeatProtected=$true;configDriftProtected=$true;logonAndMinute=$true;queuePreserved=$true;liveChanged=$false} | ConvertTo-Json
} finally {
    foreach ($name in $tasks) {
        $task=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task) { Stop-ScheduledTask -TaskName $name; Unregister-ScheduledTask -TaskName $name -Confirm:$false }
    }
}
