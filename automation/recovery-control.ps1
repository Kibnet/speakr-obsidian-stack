param([ValidateSet('Pause','Resume','Status')][string]$Action = 'Status', [string]$Root = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $Root 'config.json') -Raw | ConvertFrom-Json
if ($Action -eq 'Status') {
    foreach ($file in @('status.json','watchdog-status.json','maintenance.json')) {
        $path = Join-Path $Root $file
        if (Test-Path -LiteralPath $path) { Get-Content -Encoding UTF8 -LiteralPath $path -Raw }
    }
    exit 0
}
$path = Join-Path $Root 'maintenance.json'
if ($Action -eq 'Resume') {
    $installer=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'install-state.json') | ConvertFrom-Json
    if ($installer.phase -ne 'installed' -or $installer.upgradePending) { throw 'Complete owned runtime required; interrupted upgrade remains paused' }
    foreach ($entry in $installer.files.PSObject.Properties) {
        $file=Join-Path $Root $entry.Name
        if (!(Test-Path -LiteralPath $file) -or (Get-FileHash -LiteralPath $file).Hash -ne $entry.Value) { throw 'Owned runtime code drift; remains paused' }
    }
    if ((Get-FileHash -LiteralPath (Join-Path $Root 'config.json')).Hash -ne $installer.configHash) { throw 'Owned config drift; remains paused' }
    $observed = & (Join-Path $Root 'runtime-control.ps1') -Action Inspect -Root $Root -BridgeTask $cfg.bridge_task -OllamaTask $cfg.ollama_task | ConvertFrom-Json
    if (!$observed.bridge.trusted -or ($cfg.managed_ollama -and !$observed.ollama_task.trusted)) { throw 'Owned task action differs; remains paused' }
    $task=Get-ScheduledTask -TaskName $cfg.watchdog_task -ErrorAction SilentlyContinue
    if (!$task -or @($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -ne $cfg.pythonw -or $task.Actions[0].Arguments -ne ('"'+(Join-Path $Root 'watchdog.py')+'"') -or $task.Actions[0].WorkingDirectory -ne $Root) { throw 'Watchdog action differs; remains paused' }
}
$tmp = $path + '.' + [guid]::NewGuid().ToString('N')
@{paused=($Action -eq 'Pause');updated=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()} | ConvertTo-Json | Set-Content -LiteralPath $tmp -Encoding UTF8
Move-Item -LiteralPath $tmp -Destination $path -Force
# Neither operation changes discovery pause or task Enabled flags.
if ($Action -eq 'Resume') {
    foreach ($component in @('bridge','ollama')) {
        if ($component -eq 'ollama' -and !$cfg.managed_ollama) { continue }
        if ($component -eq 'bridge' -and !$observed.bridge.enabled) { continue }
        if ($component -eq 'ollama' -and !$observed.ollama_task.enabled) { continue }
        & (Join-Path $Root 'runtime-control.ps1') -Action Start -Root $Root -BridgeTask $cfg.bridge_task -OllamaTask $cfg.ollama_task -Component $component | Out-Null
    }
    $task=Get-ScheduledTask -TaskName $cfg.watchdog_task -ErrorAction SilentlyContinue
    if (!$task -or @($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -ne $cfg.pythonw -or $task.Actions[0].Arguments -ne ('"'+(Join-Path $Root 'watchdog.py')+'"') -or $task.Actions[0].WorkingDirectory -ne $Root) { throw 'Watchdog task action differs' }
    if ($task.State -eq 'Ready') { Start-ScheduledTask -TaskName $cfg.watchdog_task }
}
