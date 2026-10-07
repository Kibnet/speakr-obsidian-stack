param(
    [Parameter(Mandatory)][string]$Root,
    [ValidateSet('Inspect','Apply','Rollback','Verify')][string]$Action='Inspect',
    [string]$Plan,
    [string]$Python='python.exe',
    [switch]$Candidate,
    [ValidateSet('None','AfterBegin','AfterPause','AfterWithdraw','AfterGuards','AfterHolds','MiddleCopy','AfterTasks','AfterCommit','RollbackAfterPause','RollbackAfterRestore')][string]$InjectFailure='None'
)
$ErrorActionPreference='Stop'
$Root=[IO.Path]::GetFullPath($Root).TrimEnd('\')
$Python=(Get-Command $Python).Source
$helper=Join-Path $PSScriptRoot 'adoption.py'
if (!$Candidate -and $InjectFailure -ne 'None') { throw 'Failure injection is candidate-only' }
if (!$Plan) { throw 'Private plan path required (contains machine config/task XML); keep outside public Git' }
$Plan=[IO.Path]::GetFullPath($Plan)
function Put-Json([string]$Path,$Value) {
    $temp=$Path+'.'+[guid]::NewGuid().ToString('N')
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 35))
    $f=[IO.File]::Open($temp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {$f.Write($bytes,0,$bytes.Length); $f.Flush($true)} finally {$f.Dispose()}
    if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temp,$Path,($temp+'.old')); Remove-Item -LiteralPath ($temp+'.old') } else { [IO.File]::Move($temp,$Path) }
}
function Invoke-Helper([string]$Mode,[string[]]$Extra=@()) {
    $output=& $Python $helper $Mode --root $Root @Extra
    if ($LASTEXITCODE -ne 0) { throw ('Adoption '+$Mode+' failed; remains safely paused if transition began') }
    return ($output | ConvertFrom-Json)
}
function Snapshot($Cfg) {
    $scheduler=New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $tasks=@{}
    foreach ($name in @($Cfg.bridge_task,$Cfg.watchdog_task,$Cfg.ollama_task)) {
        $task=$scheduler.GetFolder('\').GetTask($name)
        $d=$task.Definition
        $actions=@(foreach ($a in $d.Actions) {
            @{execute=$a.Path;arguments=$a.Arguments;working_directory=$a.WorkingDirectory}
        })
        $account=[Security.Principal.NTAccount]::new($d.Principal.UserId)
        $sid=if ($d.Principal.UserId -match '^S-1-') {$d.Principal.UserId} else {$account.Translate([Security.Principal.SecurityIdentifier]).Value}
        $tasks[$name]=@{xml=$task.Xml;enabled=[bool]$task.Enabled;sid=$sid;actions=$actions}
    }
    return @{sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;tasks=$tasks}
}
function Crash([string]$Boundary) {
    if ($InjectFailure -eq $Boundary) { [Environment]::Exit(97) }
}
function Set-TaskXml([string]$Name,[string]$Xml) {
    $scheduler=New-Object -ComObject 'Schedule.Service'; $scheduler.Connect()
    $d=$scheduler.NewTask(0); $d.XmlText=$Xml
    # TASK_UPDATE, interactive token; updating a definition must not stop its running instance.
    $scheduler.GetFolder('\').RegisterTaskDefinition($Name,$d,4,$d.Principal.UserId,$null,3,$null) | Out-Null
}
function Canonical-Task([string]$Xml,[bool]$IgnoreEnabled=$true) {
    $scheduler=New-Object -ComObject 'Schedule.Service'; $scheduler.Connect()
    $d=$scheduler.NewTask(0); $d.XmlText=$Xml
    if ($IgnoreEnabled) {$d.Settings.Enabled=$true}
    [xml]$doc=$d.XmlText
    $doc.Task.Principals.Principal.RemoveAttribute('id')
    $doc.Task.Actions.RemoveAttribute('Context')
    return $doc.OuterXml
}
function Update-TaskActions($Cfg,$Original) {
    $scheduler=New-Object -ComObject 'Schedule.Service'; $scheduler.Connect()
    foreach ($name in @($Cfg.bridge_task,$Cfg.watchdog_task,$Cfg.ollama_task)) {
        $d=$scheduler.NewTask(0); $d.XmlText=$Original.tasks.$name.xml
        $a=$d.Actions.Item(1)
        if ($name -eq $Cfg.ollama_task) {
            $a.Path=(Get-Command powershell.exe).Source
            $a.Arguments='-NoProfile -WindowStyle Hidden -File "'+$Cfg.ollama_launcher+'" -Root "'+$Root+'"'
        } else {
            $a.Path=$Cfg.pythonw
            $a.Arguments='"'+(Join-Path $Root $(if ($name -eq $Cfg.bridge_task) {'bridge.py'} else {'watchdog.py'}))+'"'+$(if ($name -eq $Cfg.bridge_task) {' --loop'} else {''})
        }
        $a.WorkingDirectory=$Root
        # Bridge/watchdog remain disabled until committed config has been verified.
        if ($name -ne $Cfg.ollama_task) {$d.Settings.Enabled=$false}
        $journalPath=Join-Path $Root 'adoption-state.json'
        $journal=Get-Content -Raw -Encoding UTF8 -LiteralPath $journalPath | ConvertFrom-Json
        if (!$journal.taskIntents) {$journal | Add-Member -NotePropertyName taskIntents -NotePropertyValue ([pscustomobject]@{})}
        $journal.taskIntents | Add-Member -NotePropertyName $name -NotePropertyValue $d.XmlText -Force
        Put-Json $journalPath $journal
        Set-TaskXml $name $d.XmlText
    }
}
function Restore-Flags($Original,$Cfg) {
    foreach ($name in @($Cfg.bridge_task,$Cfg.watchdog_task,$Cfg.ollama_task)) {
        if ($Original.tasks.$name.enabled) { Enable-ScheduledTask -TaskName $name | Out-Null }
        else { Disable-ScheduledTask -TaskName $name | Out-Null }
    }
}
function Wait-OldProcesses([int]$TimeoutSeconds=45) {
    $until=[DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $queryFailed=$false
    do {
        $escaped=[regex]::Escape($Root+'\')
        try {
            $processes=@(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='powershell.exe' OR Name='pwsh.exe'" -OperationTimeoutSec 10 -ErrorAction Stop | Where-Object {
                $_.CommandLine -and $_.CommandLine -match ($escaped+'[^"\s]+\.(pyw?|ps1)(?:["\s]|$)')
            })
            $queryFailed=$false
            if (!$processes) { return }
        } catch { $queryFailed=$true }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $until)
    if ($queryFailed) {throw 'Legacy process inspection unavailable; safely paused, no DB/code migration'}
    throw 'Old runtime process still active; no forced termination, no DB/code migration'
}
$nativeFile=$Plan+'.native.json'
if ($Action -eq 'Inspect') {
    $cfg=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'config.json') | ConvertFrom-Json
    Put-Json $nativeFile (Snapshot $cfg)
    try { Invoke-Helper inspect @('--plan',$Plan,'--native',$nativeFile) | ConvertTo-Json } finally {Remove-Item -LiteralPath $nativeFile -ErrorAction SilentlyContinue}
    exit 0
}
if ($Action -eq 'Verify') { Invoke-Helper verify | ConvertTo-Json; exit 0 }
if ($Action -eq 'Apply' -and (Test-Path -LiteralPath (Join-Path $Root 'adoption-state.json'))) {
    $existing=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'adoption-state.json') | ConvertFrom-Json
    if ($existing.phase -eq 'installed') {
        Invoke-Helper verify | Out-Null
        $owned=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'install-state.json') | ConvertFrom-Json
        foreach ($entry in $owned.files.PSObject.Properties) {
            if ((Get-FileHash -LiteralPath (Join-Path (Split-Path $PSScriptRoot -Parent) ('automation\'+$entry.Name))).Hash -ne $entry.Value) {throw 'Different package; use Upgrade'}
        }
        @{status='owned_version_verified';changed=$false} | ConvertTo-Json
        exit 0
    }
}
if ($Action -eq 'Rollback') {
    $state=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'adoption-state.json') | ConvertFrom-Json
    $saved=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $state.backup 'plan.json') | ConvertFrom-Json
    $cfg=$saved.new_config
    Invoke-Helper rollback-check | Out-Null
    $current=Snapshot $cfg
    foreach ($entry in $saved.native.tasks.PSObject.Properties) {
        if ($state.phase -eq 'installed' -and (Canonical-Task $current.tasks[$entry.Name].xml $false) -ne (Canonical-Task $state.tasks.$($entry.Name).xml $false)) {throw 'Installed task drift; rollback refused before effects'}
        if ($state.phase -eq 'rollback_preparing') {
            $precise=Canonical-Task $current.tasks[$entry.Name].xml $false
            if ($precise -ne (Canonical-Task $state.rollbackTasks.$($entry.Name) $false) -and $precise -ne (Canonical-Task $state.rollbackOriginalTasks.$($entry.Name) $false)) {throw 'Rollback task drift; replay refused before effects'}
        }
        $actual=Canonical-Task $current.tasks[$entry.Name].xml
        if ($actual -ne (Canonical-Task $entry.Value.xml) -and (!$state.taskIntents.$($entry.Name) -or $actual -ne (Canonical-Task $state.taskIntents.$($entry.Name)))) {throw 'Foreign task drift; rollback refused before effects'}
    }
    if ($state.phase -ne 'rolling_back' -and $state.phase -ne 'rollback_preparing') {
        $state | Add-Member -NotePropertyName rollbackOriginalTasks -NotePropertyValue @{} -Force
        $state | Add-Member -NotePropertyName rollbackTasks -NotePropertyValue @{} -Force
        $scheduler=New-Object -ComObject 'Schedule.Service'; $scheduler.Connect()
        foreach ($entry in $saved.native.tasks.PSObject.Properties) {
            $state.rollbackOriginalTasks[$entry.Name]=$current.tasks[$entry.Name].xml
            $d=$scheduler.NewTask(0); $d.XmlText=$current.tasks[$entry.Name].xml
            if ($entry.Name -ne $cfg.ollama_task) {$d.Settings.Enabled=$false}
            $state.rollbackTasks[$entry.Name]=$d.XmlText
        }
        $state | Add-Member -NotePropertyName rollbackFrom -NotePropertyValue $state.phase -Force
        $state.phase='rollback_preparing'
        Put-Json (Join-Path $Root 'adoption-state.json') $state
    }
    Put-Json (Join-Path $Root 'maintenance.json') @{paused=$true}
    Put-Json (Join-Path $Root 'stop.json') @{at=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0}
    foreach ($name in @($cfg.bridge_task,$cfg.watchdog_task)) { Disable-ScheduledTask -TaskName $name | Out-Null }
    Crash RollbackAfterPause
    Wait-OldProcesses
    Invoke-Helper rollback | Out-Null
    foreach ($entry in $saved.native.tasks.PSObject.Properties) {Set-TaskXml $entry.Name $entry.Value.xml}
    Crash RollbackAfterRestore
    if ($saved.maintenance) {[IO.File]::WriteAllBytes((Join-Path $Root 'maintenance.json'),[Convert]::FromBase64String($saved.maintenance))}
    else {Remove-Item -LiteralPath (Join-Path $Root 'maintenance.json') -ErrorAction SilentlyContinue}
    Invoke-Helper release-old | Out-Null
    @{status='rolled_back';queueRewound=$false;startExplicit=$true} | ConvertTo-Json
    exit 0
}
$planData=Get-Content -Raw -Encoding UTF8 -LiteralPath $Plan | ConvertFrom-Json
if ([IO.Path]::GetFullPath($planData.root) -ne $Root) {throw 'Plan root mismatch'}
Invoke-Helper validate @('--plan',$Plan) | Out-Null
$current=Snapshot $planData.new_config
foreach ($entry in $planData.native.tasks.PSObject.Properties) {
    if ($current.tasks[$entry.Name].xml -ne $entry.Value.xml) {throw 'Task drift since Inspect'}
}
$state=Invoke-Helper begin @('--plan',$Plan)
Crash AfterBegin
$handles=@()
try {
    Put-Json (Join-Path $Root 'maintenance.json') @{paused=$true}
    Put-Json (Join-Path $Root 'stop.json') @{at=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0}
    foreach ($name in @($planData.new_config.bridge_task,$planData.new_config.watchdog_task)) {Disable-ScheduledTask -TaskName $name | Out-Null}
    foreach ($entry in $planData.originals.PSObject.Properties) {
        $handles+= [IO.File]::Open((Join-Path $Root $entry.Name),[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Delete)
    }
    Invoke-Helper installer-guard | Out-Null
    Crash AfterPause
    # Persistent common-input barrier precedes every runtime code swap; the no-config installer is already guarded.
    [IO.File]::Move((Join-Path $Root 'config.json'),(Join-Path $state.backup 'config.withdrawn.json'))
    Crash AfterWithdraw
    Wait-OldProcesses
    Invoke-Helper guards | Out-Null
    Crash AfterGuards
    foreach ($h in $handles) {$h.Dispose()}; $handles=@()
    $extra=if ($InjectFailure -in @('AfterHolds','MiddleCopy')) {@('--fail',$InjectFailure)} else {@()}
    Invoke-Helper stage $extra | Out-Null
    Update-TaskActions $planData.new_config $planData.native
    Crash AfterTasks
    # Capture final expected enabled flags without launching any process.
    Restore-Flags $planData.native $planData.new_config
    Put-Json $nativeFile (Snapshot $planData.new_config)
    Invoke-Helper commit @('--native',$nativeFile) | Out-Null
    Crash AfterCommit
    # Adoption is intentionally committed while paused. Explicit Resume validates ownership.
    @{status='installed_paused';backup=$state.backup;commit=$planData.commit;resumeRequired=$true} | ConvertTo-Json
} finally {
    foreach ($h in $handles) {$h.Dispose()}
    Remove-Item -LiteralPath $nativeFile -ErrorAction SilentlyContinue
}
