param([string]$Root=$PSScriptRoot)
$ErrorActionPreference='Stop'
$migration=Join-Path $Root 'adoption-state.json'
if ((Test-Path -LiteralPath $migration) -and (Get-Content -Raw -Encoding UTF8 -LiteralPath $migration | ConvertFrom-Json).phase -ne 'installed') { throw 'Partial adoption; launcher refused' }
$maintenance=Join-Path $Root 'maintenance.json'
if ((Test-Path -LiteralPath $maintenance) -and (Get-Content -Raw -Encoding UTF8 -LiteralPath $maintenance | ConvertFrom-Json).paused) { exit 0 }
$cfg = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'config.json') | ConvertFrom-Json
if (!(Test-Path -LiteralPath $cfg.ollama)) { throw 'Configured Ollama executable missing' }
if (!$cfg.managed_ollama) { throw 'Ollama is externally managed' }
$listeners=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq $cfg.llm_port)
if ($listeners) {
    foreach ($listener in $listeners) {
        $owner=Get-Process -Id $listener.OwningProcess -ErrorAction Stop
        if ($owner.Path -ne $cfg.ollama -or $listener.LocalAddress -ne '127.0.0.1') { throw 'Ollama endpoint belongs to another process' }
    }
    exit 0
}
$env:OLLAMA_HOST='127.0.0.1:'+[string]$cfg.llm_port
$env:OLLAMA_MODELS=$cfg.ollama_models
$env:OLLAMA_NO_CLOUD='1'
if ($cfg.ollama_noprune) { $env:OLLAMA_NOPRUNE='1' } else { Remove-Item Env:OLLAMA_NOPRUNE -ErrorAction SilentlyContinue }
$env:OLLAMA_NUM_PARALLEL='1'
$env:OLLAMA_MAX_LOADED_MODELS='1'
$env:OLLAMA_CONTEXT_LENGTH='16384'
$env:OLLAMA_KEEP_ALIVE='60s'
foreach ($property in $cfg.ollama_tuning.PSObject.Properties) {
    if ($property.Name -notin @('OLLAMA_FLASH_ATTENTION','OLLAMA_KV_CACHE_TYPE','OLLAMA_GPU_OVERHEAD','OLLAMA_LLM_LIBRARY','OLLAMA_VULKAN','CUDA_VISIBLE_DEVICES')) { throw 'Unsupported tuning variable' }
    [Environment]::SetEnvironmentVariable($property.Name,[string]$property.Value,'Process')
}
foreach ($name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY')) { Remove-Item -LiteralPath ('Env:\'+$name) -ErrorAction SilentlyContinue }
$env:NO_PROXY=if ($cfg.ollama_no_proxy) {$cfg.ollama_no_proxy} else {'localhost,127.0.0.1,::1'}
New-Item -ItemType Directory -Path (Join-Path $Root 'logs') -Force | Out-Null
$log=Join-Path $Root 'logs\ollama.log'
if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 10MB) { Move-Item -LiteralPath $log -Destination ($log+'.1') -Force }
# Normal native stderr is a log stream, not a terminating PowerShell 5 ErrorRecord.
$ErrorActionPreference='Continue'
& $cfg.ollama serve 2>&1 | Out-File -LiteralPath $log -Append -Encoding utf8
exit $LASTEXITCODE
