param(
    [Parameter(Mandatory)][string]$TargetRoot,
    [ValidateSet('Install','Rollback','Upgrade')][string]$Action = 'Install',
    [string]$Python = 'python.exe',
    [switch]$Candidate,
    [ValidateSet('None','BeforeCopy','MiddleCopy','AfterCopy','AfterConfigWrite','CrashAfterConfigWrite','UpgradeCommit','AfterBaseline','AfterBridgeTask','AfterWatchdogTask')][string]$InjectFailure = 'None'
)
$ErrorActionPreference = 'Stop'
$package = Split-Path $PSScriptRoot -Parent
$target = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\')
$root = Join-Path $target 'automation'
$stackManifestPath = Join-Path $target 'stack-install.json'
$installPath = Join-Path $root 'install-state.json'
$cfgPath = Join-Path $root 'config.json'
$cfg = Get-Content -Raw -Encoding UTF8 -LiteralPath $cfgPath | ConvertFrom-Json
$Python = (Get-Command $Python -ErrorAction Stop).Source
$preManifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $stackManifestPath | ConvertFrom-Json
if ($preManifest.mode -eq 'adopted') {
    if ($Action -ne 'Upgrade') { throw 'Adopted runtime: use scripts/adopt.ps1 Rollback; fresh Install/Rollback refused' }
    & $Python (Join-Path $PSScriptRoot 'adoption.py') verify --root $root | Out-Null
} else { & $Python (Join-Path $PSScriptRoot 'runtime_guard.py') $target | Out-Null }
if ($LASTEXITCODE -ne 0) { throw 'Canonical runtime guard failed' }
if (!$Candidate -and $InjectFailure -ne 'None') { throw 'Failure injection is candidate-only' }
if ($target -eq [IO.Path]::GetPathRoot($target).TrimEnd('\')) { throw 'Drive root refused' }
function Put-Json([string]$Path, $Value) {
    $temp = $Path + '.' + [guid]::NewGuid().ToString('N')
    $Value | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 -LiteralPath $temp
    if (Test-Path -LiteralPath $Path) {
        [IO.File]::Replace($temp,$Path,($temp+'.replaced'))
        Remove-Item -LiteralPath ($temp+'.replaced')
    }
    else { [IO.File]::Move($temp,$Path) }
}
function Task-Equivalent([string]$Left,[string]$Right) {
    function Canonical([string]$Text) {
        $scheduler = New-Object -ComObject 'Schedule.Service'
        $scheduler.Connect()
        $definition = $scheduler.NewTask(0)
        $definition.XmlText = $Text
        [xml]$doc = $definition.XmlText
        $doc.Task.Principals.Principal.RemoveAttribute('id')
        $doc.Task.Actions.RemoveAttribute('Context')
        return $doc.OuterXml
    }
    (Canonical $Left) -eq (Canonical $Right)
}
function Copy-Atomic([string]$Source,[string]$Destination) {
    $tmp=$Destination+'.install-'+[guid]::NewGuid().ToString('N')
    try {
        Copy-Item -LiteralPath $Source -Destination $tmp
        if ((Get-FileHash -LiteralPath $tmp).Hash -ne (Get-FileHash -LiteralPath $Source).Hash) { throw 'Staged copy mismatch' }
        if ($InjectFailure -eq 'MiddleCopy') { throw 'Injected mid-copy failure' }
        if (Test-Path -LiteralPath $Destination) {
            [IO.File]::Replace($tmp,$Destination,($tmp+'.replaced'))
            Remove-Item -LiteralPath ($tmp+'.replaced')
        } else { [IO.File]::Move($tmp,$Destination) }
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp }
    }
}
function Set-DockerAutostart([string]$Path,[bool]$Value,[bool]$Expected) {
    $before=[IO.File]::ReadAllBytes($Path)
    $fresh=[Text.Encoding]::UTF8.GetString($before).TrimStart([char]0xfeff) | ConvertFrom-Json
    if ([bool]$fresh.autoStart -ne $Expected) { throw 'Docker AutoStart changed concurrently' }
    $fresh.autoStart=$Value
    $tmp=$Path+'.stack-'+[guid]::NewGuid().ToString('N')
    try {
        $fresh | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 -LiteralPath $tmp
        if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($Path)) -ne [Convert]::ToBase64String($before)) { throw 'Docker settings changed concurrently' }
        [IO.File]::Replace($tmp,$Path,($tmp+'.replaced'))
        Remove-Item -LiteralPath ($tmp+'.replaced')
    } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp } }
}
function Own-Check($Manifest) {
    if ($Manifest.mode -eq 'adopted') {
        foreach ($entry in $Manifest.external_files.PSObject.Properties) {
            if (!(Test-Path -LiteralPath $entry.Name) -or (Get-FileHash -LiteralPath $entry.Name).Hash.ToLowerInvariant() -ne $entry.Value) { throw 'Adopted external input drift' }
        }
    }
    foreach ($entry in $Manifest.files.PSObject.Properties) {
        $path = Join-Path $target $entry.Name
        if (!(Test-Path -LiteralPath $path) -or (Get-FileHash -LiteralPath $path).Hash.ToLowerInvariant() -ne $entry.Value) {
            throw ('Owned file drift: ' + $entry.Name)
        }
    }
}
function Task-Xml([string]$Name,[string]$Exe,[string]$Arguments,[bool]$Minute) {
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $d = $scheduler.NewTask(0)
    $d.RegistrationInfo.URI = '\' + $Name
    $d.RegistrationInfo.Description = 'Local transcription stack; explicit maintenance pause has priority.'
    $d.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $d.Principal.LogonType = 3
    $d.Settings.MultipleInstances = 2
    $d.Settings.ExecutionTimeLimit = if ($Minute) {'PT45S'} else {'PT0S'}
    $d.Settings.DisallowStartIfOnBatteries = $false
    $d.Settings.StopIfGoingOnBatteries = $false
    $d.Settings.StartWhenAvailable = $true
    $t = $d.Triggers.Create(9)
    $t.UserId = $d.Principal.UserId
    if ($Minute) {
        $t = $d.Triggers.Create(1)
        $t.StartBoundary = (Get-Date).AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
        $t.Repetition.Interval = 'PT1M'
    }
    $a = $d.Actions.Create(0)
    $a.Path = $Exe
    $a.Arguments = $Arguments
    $a.WorkingDirectory = $root
    $d.XmlText
}
function Stop-Own-Worker {
    Put-Json (Join-Path $root 'maintenance.json') @{paused=$true}
    Put-Json (Join-Path $root 'stop.json') @{at=([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0)}
    $limit = [DateTime]::UtcNow.AddSeconds(45)
    do {
        $task = Get-ScheduledTask -TaskName $cfg.bridge_task -ErrorAction SilentlyContinue
        $alive = $false
        $hp = Join-Path $root 'heartbeat.json'
        if (Test-Path -LiteralPath $hp) {
            $h = Get-Content -Raw -Encoding UTF8 -LiteralPath $hp | ConvertFrom-Json
            $proc = Get-Process -Id $h.pid -ErrorAction SilentlyContinue
            $alive = $proc -and ([string]$proc.StartTime.ToFileTimeUtc() -eq [string]$h.start_marker)
        }
        if ((!$task -or $task.State -ne 'Running') -and !$alive) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $limit)
    throw 'Worker is busy; paused, no forced stop or code change'
}
function Rollback($state,[bool]$Partial) {
    # Check every owned resource before the first effect; queue and notes are never restored/deleted.
    if ($state.target -ne $target) { throw 'Rollback target mismatch' }
    $expectedCfg = if ($state.configHash) {$state.configHash} else {$state.originalConfigHash}
    $actualCfg=(Get-FileHash -LiteralPath $cfgPath).Hash
    if ($actualCfg -ne $expectedCfg -and !($Partial -and $actualCfg -eq $state.originalConfigHash)) { throw 'Rollback config conflict' }
    foreach ($entry in $state.files.PSObject.Properties) {
        $path = Join-Path $root $entry.Name
        if (Test-Path -LiteralPath $path) {
            if ((Get-FileHash -LiteralPath $path).Hash -ne $entry.Value) { throw ('Rollback file conflict: '+$entry.Name) }
        } elseif (!$Partial) { throw ('Rollback missing own file: '+$entry.Name) }
    }
    foreach ($entry in $state.tasks.PSObject.Properties) {
        $task = Get-ScheduledTask -TaskName $entry.Name -ErrorAction SilentlyContinue
        if ($task -and !(Task-Equivalent (Export-ScheduledTask -TaskName $entry.Name) $entry.Value)) { throw ('Rollback task conflict: '+$entry.Name) }
        if (!$task -and !$Partial) { throw 'Rollback task missing' }
    }
    if ($state.dockerIntent) {
        if (Test-Path -LiteralPath (Join-Path $root 'docker-startup.json')) {
            & (Join-Path $root 'docker-startup.ps1') -Action VerifyRollback -BackupPath $root -AllowOriginal:$Partial | Out-Null
        }
        $settings = Get-Content -Raw -Encoding UTF8 -LiteralPath $state.dockerSettingsPath | ConvertFrom-Json
        if ($settings.autoStart -ne $true -and !($Partial -and !$state.dockerSettingsApplied -and $settings.autoStart -eq $state.dockerOriginal)) { throw 'Docker autostart drift' }
        $rollbackAutoExpected=[bool]$settings.autoStart
    }
    Stop-Own-Worker
    foreach ($entry in $state.tasks.PSObject.Properties) {
        $task = Get-ScheduledTask -TaskName $entry.Name -ErrorAction SilentlyContinue
        if ($task) { Stop-ScheduledTask -TaskName $entry.Name; Unregister-ScheduledTask -TaskName $entry.Name -Confirm:$false }
    }
    if ($state.dockerIntent) {
        if (Test-Path -LiteralPath (Join-Path $root 'docker-startup.json')) {
            & (Join-Path $root 'docker-startup.ps1') -Action Rollback -BackupPath $root -AllowOriginal:$Partial | Out-Null
        }
        Set-DockerAutostart $state.dockerSettingsPath $state.dockerOriginal $rollbackAutoExpected
    }
    foreach ($entry in $state.files.PSObject.Properties) {
        $path = Join-Path $root $entry.Name
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
    }
    $restore=$cfgPath+'.restore-'+[guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllBytes($restore,[Convert]::FromBase64String($state.originalConfig))
    if ((Get-FileHash -LiteralPath $restore).Hash -ne $state.originalConfigHash) { throw 'Original config hash mismatch' }
    [IO.File]::Replace($restore,$cfgPath,($restore+'.replaced'))
    Remove-Item -LiteralPath ($restore+'.replaced')
    $sm = Get-Content -Raw -Encoding UTF8 -LiteralPath $stackManifestPath | ConvertFrom-Json
    $sm.files.'automation\config.json' = (Get-FileHash -LiteralPath $cfgPath).Hash.ToLowerInvariant()
    Put-Json $stackManifestPath $sm
    $state.phase = 'rolled_back'
    Put-Json $installPath $state
    @{status='rolled_back';queueRestored=$false;ownTasksRemoved=$true} | ConvertTo-Json
}
$sm = Get-Content -Raw -Encoding UTF8 -LiteralPath $stackManifestPath | ConvertFrom-Json
if ($sm.phase -ne 'installed' -or !$sm.files.'automation\config.json' -or ($sm.mode -ne 'adopted' -and (!$sm.files.'stack\.env' -or !$sm.files.'stack\compose.yaml'))) { throw 'Preparation incomplete; normal controls refused' }
if ([IO.Path]::GetFullPath($sm.target_root).TrimEnd('\') -ne $target) { throw 'Stack ownership mismatch' }
if ($Action -eq 'Rollback') {
    # Config may be in the exact journaled planned state before stack manifest read-back.
    foreach ($entry in $sm.files.PSObject.Properties) {
        if ($entry.Name -eq 'automation\config.json') { continue }
        $p=Join-Path $target $entry.Name
        if (!(Test-Path -LiteralPath $p) -or (Get-FileHash -LiteralPath $p).Hash.ToLowerInvariant() -ne $entry.Value) { throw 'Stack file drift' }
    }
    $state = Get-Content -Raw -Encoding UTF8 -LiteralPath $installPath | ConvertFrom-Json
    if ($state.phase -eq 'rolled_back') { throw 'Already rolled back' }
    Rollback $state ($state.phase -ne 'installed')
    exit 0
}
Own-Check $sm
if ($Action -eq 'Upgrade') {
    $state = Get-Content -Raw -Encoding UTF8 -LiteralPath $installPath | ConvertFrom-Json
    if ($state.phase -ne 'installed') { throw 'Only an installed owned runtime can be upgraded' }
    if ($state.upgradePending) { throw 'Interrupted upgrade: follow docs/operations.md before proceeding' }
    foreach ($entry in $state.files.PSObject.Properties) {
        if ((Get-FileHash -LiteralPath (Join-Path $root $entry.Name)).Hash -ne $entry.Value) { throw 'Runtime source drift' }
        if (!(Test-Path -LiteralPath (Join-Path $package ('automation\'+$entry.Name)))) { throw 'Removing runtime modules requires explicit migration' }
    }
    foreach ($entry in $state.tasks.PSObject.Properties) {
        $t = Get-ScheduledTask -TaskName $entry.Name -ErrorAction SilentlyContinue
        if (!$t) { throw 'Owned task missing' }
        # Disabled state is an operator input, not a reason to resurrect a task.
        $scheduler=New-Object -ComObject 'Schedule.Service'
        $scheduler.Connect()
        $expected=$scheduler.NewTask(0)
        $expected.XmlText=$entry.Value
        $actual=$scheduler.NewTask(0)
        $actual.XmlText=Export-ScheduledTask -TaskName $entry.Name
        $expected.Settings.Enabled=$actual.Settings.Enabled
        if (!(Task-Equivalent $actual.XmlText $expected.XmlText)) { throw 'Owned task drift' }
    }
    $incoming=@{}
    foreach ($p in Get-ChildItem -LiteralPath (Join-Path $package 'automation') -File) {
        if ($p.Extension -notin @('.py','.pyw','.ps1')) { continue }
        if ($p.Name -notin @($state.files.PSObject.Properties.Name) -and (Test-Path -LiteralPath (Join-Path $root $p.Name))) { throw 'New module collision' }
        $incoming[$p.Name]=(Get-FileHash -LiteralPath $p.FullName).Hash
    }
    $same=@($incoming.Keys | Where-Object { $incoming[$_] -ne $state.files.$_ }).Count -eq 0
    if ($same) { @{status='owned_version_verified';changed=$false} | ConvertTo-Json; exit 0 }
    $backup=Join-Path $root ('backups\upgrade-'+[guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $backup | Out-Null
    foreach ($entry in $state.files.PSObject.Properties) { Copy-Item -LiteralPath (Join-Path $root $entry.Name) -Destination $backup }
    Copy-Item -LiteralPath $installPath -Destination (Join-Path $backup 'install-state.json')
    $maintenance=Join-Path $root 'maintenance.json'
    $maintenanceBytes=[IO.File]::ReadAllBytes($maintenance)
    Stop-Own-Worker
    & $Python (Join-Path $root 'backup-state.py') --root $root --destination (Join-Path $backup 'state.sqlite3')
    if ($LASTEXITCODE -ne 0) { throw 'Queue backup failed; code unchanged, safely paused' }
    $state | Add-Member -NotePropertyName upgradePending -NotePropertyValue @{backup=$backup;incoming=$incoming} -Force
    Put-Json $installPath $state
    $originalFiles=$state.files
    try {
        foreach ($name in $incoming.Keys) { Copy-Atomic (Join-Path $package ('automation\'+$name)) (Join-Path $root $name) }
        $state.files=($incoming | ConvertTo-Json | ConvertFrom-Json)
        $state.upgradePending=$null
        if ($InjectFailure -eq 'UpgradeCommit') { throw 'Injected upgrade-journal failure' }
        Put-Json $installPath $state
    } catch {
        $failure=$_
        $InjectFailure='None'
        # Roll back code only; never rewind queue/publications to the backup.
        foreach ($entry in $originalFiles.PSObject.Properties) { Copy-Atomic (Join-Path $backup $entry.Name) (Join-Path $root $entry.Name) }
        foreach ($name in $incoming.Keys) {
            if ($name -notin @($originalFiles.PSObject.Properties.Name) -and (Test-Path -LiteralPath (Join-Path $root $name))) { Remove-Item -LiteralPath (Join-Path $root $name) }
        }
        Copy-Item -LiteralPath (Join-Path $backup 'install-state.json') -Destination $installPath -Force
        [IO.File]::WriteAllBytes($maintenance,$maintenanceBytes)
        throw $failure
    }
    @{status='upgraded_paused';backup=$backup;queueRestored=$false;tasksModified=$false} | ConvertTo-Json
    exit 0
}
if (Test-Path -LiteralPath $installPath) {
    $previous = Get-Content -Raw -Encoding UTF8 -LiteralPath $installPath | ConvertFrom-Json
    if ($previous.phase -ne 'rolled_back') { throw 'Already owned; Install refused, use explicit Upgrade or Rollback' }
}
foreach ($name in @($cfg.bridge_task,$cfg.watchdog_task,$cfg.ollama_task)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw ('Task collision: '+$name) }
}
if (!$Candidate -and (!$cfg.runtime_manifest -or $sm.up_phase -ne 'ready')) { throw 'Run stack up/start and complete identities before install' }
if (!$Candidate) {
    & $Python (Join-Path $PSScriptRoot 'readiness.py') $root | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Configured service/API readiness failed; bridge not installed' }
}
if (!(Test-Path -LiteralPath $cfg.pythonw)) { throw 'pythonw missing' }
$files = @{}
foreach ($path in Get-ChildItem -LiteralPath (Join-Path $package 'automation') -File) {
    if ($path.Extension -notin @('.py','.pyw','.ps1')) { continue }
    if (Test-Path -LiteralPath (Join-Path $root $path.Name)) { throw ('Unowned file collision: '+$path.Name) }
    $files[$path.Name] = (Get-FileHash -LiteralPath $path.FullName).Hash
}
$tasks = @{}
$tasks[$cfg.bridge_task] = Task-Xml $cfg.bridge_task $cfg.pythonw ('"'+(Join-Path $root 'bridge.py')+'" --loop') $false
$tasks[$cfg.watchdog_task] = Task-Xml $cfg.watchdog_task $cfg.pythonw ('"'+(Join-Path $root 'watchdog.py')+'"') $true
if ($cfg.managed_ollama -and !$Candidate) {
    $tasks[$cfg.ollama_task] = Task-Xml $cfg.ollama_task (Get-Command powershell.exe).Source ('-NoProfile -WindowStyle Hidden -File "'+(Join-Path $root 'start-ollama.ps1')+'" -Root "'+$root+'"') $false
}
$state = @{phase='prepared';target=$target;files=$files;tasks=$tasks;originalConfig=[Convert]::ToBase64String([IO.File]::ReadAllBytes($cfgPath));originalConfigHash=(Get-FileHash -LiteralPath $cfgPath).Hash}
Put-Json $installPath $state
try {
    if ($InjectFailure -eq 'BeforeCopy') { throw 'Injected before-copy failure' }
    foreach ($file in $files.Keys) { Copy-Atomic (Join-Path $package ('automation\'+$file)) (Join-Path $root $file) }
    if ($InjectFailure -eq 'AfterCopy') { throw 'Injected after-copy failure' }
    if ($Candidate) { $cfg.supervision_candidate = $true }
    $plannedConfig=Join-Path $root 'config.planned.json'
    Put-Json $plannedConfig $cfg
    $state.configHash = (Get-FileHash -LiteralPath $plannedConfig).Hash
    Put-Json $installPath $state
    [IO.File]::Replace($plannedConfig,$cfgPath,($plannedConfig+'.original'))
    Remove-Item -LiteralPath ($plannedConfig+'.original')
    if ($InjectFailure -eq 'AfterConfigWrite') { throw 'Injected after-config-write failure' }
    if ($InjectFailure -eq 'CrashAfterConfigWrite') { [Environment]::Exit(77) }
    $sm.files.'automation\config.json' = $state.configHash.ToLowerInvariant()
    Put-Json $stackManifestPath $sm
    Put-Json $installPath $state
    # Baseline directly scans before maintenance. It never submits or polls.
    & $Python (Join-Path $root 'baseline.py') --root $root
    if ($LASTEXITCODE -ne 0) { throw 'Initial source baseline failed' }
    if ($InjectFailure -eq 'AfterBaseline') { throw 'Injected after-baseline failure' }
    Put-Json (Join-Path $root 'maintenance.json') @{paused=$true}
    foreach ($name in @($tasks.Keys)) {
        Register-ScheduledTask -TaskName $name -Xml $tasks[$name] | Out-Null
        $state.tasks[$name] = Export-ScheduledTask -TaskName $name
        Put-Json $installPath $state
        if (($name -eq $cfg.bridge_task -and $InjectFailure -eq 'AfterBridgeTask') -or
            ($name -eq $cfg.watchdog_task -and $InjectFailure -eq 'AfterWatchdogTask')) { throw 'Injected after-task failure' }
    }
    if ($cfg.auto_start_docker -and !$Candidate) {
        $settingsPath = Join-Path $env:APPDATA 'Docker\settings-store.json'
        $settings = Get-Content -Raw -Encoding UTF8 -LiteralPath $settingsPath | ConvertFrom-Json
        $state.dockerSettingsPath=$settingsPath
        $state.dockerOriginal=[bool]$settings.autoStart
        $state.dockerIntent=$true
        Put-Json $installPath $state
        & (Join-Path $root 'docker-startup.ps1') -Action Enable -BackupPath $root | Out-Null
        Set-DockerAutostart $settingsPath $true $state.dockerOriginal
        $state.dockerSettingsApplied=$true
        Put-Json $installPath $state
    }
    $state.phase='installed'
    Put-Json $installPath $state
    @{status='installed_paused';baselineOnly=$true;tasks=@($tasks.Keys)} | ConvertTo-Json
} catch {
    $failure=$_
    $partial = Get-Content -Raw -Encoding UTF8 -LiteralPath $installPath | ConvertFrom-Json
    Rollback $partial $true | Out-Null
    throw $failure
}
