param([string]$Root=$PSScriptRoot)
$ErrorActionPreference='Stop'
$maintenance=Join-Path $Root 'maintenance.json'
if ((Test-Path -LiteralPath $maintenance) -and (Get-Content -Raw -Encoding UTF8 -LiteralPath $maintenance | ConvertFrom-Json).paused) { exit 0 }
$cfg = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'config.json') | ConvertFrom-Json
if (!(Test-Path -LiteralPath $cfg.ollama)) { throw 'Configured Ollama executable missing' }
if (!$cfg.managed_ollama) { throw 'Ollama is externally managed' }
$env:OLLAMA_HOST='127.0.0.1:'+[string]$cfg.llm_port
$env:OLLAMA_MODELS=$cfg.ollama_models
$env:OLLAMA_NO_CLOUD='1'
$env:OLLAMA_NUM_PARALLEL='1'
$env:OLLAMA_MAX_LOADED_MODELS='1'
$env:OLLAMA_CONTEXT_LENGTH='16384'
$env:OLLAMA_KEEP_ALIVE='60s'
foreach ($property in $cfg.ollama_tuning.PSObject.Properties) {
    if ($property.Name -notin @('OLLAMA_FLASH_ATTENTION','OLLAMA_KV_CACHE_TYPE','OLLAMA_GPU_OVERHEAD','OLLAMA_LLM_LIBRARY','OLLAMA_VULKAN','CUDA_VISIBLE_DEVICES')) { throw 'Unsupported tuning variable' }
    [Environment]::SetEnvironmentVariable($property.Name,[string]$property.Value,'Process')
}
foreach ($name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY')) { Remove-Item -LiteralPath ('Env:\'+$name) -ErrorAction SilentlyContinue }
$env:NO_PROXY='localhost,127.0.0.1,::1'
New-Item -ItemType Directory -Path (Join-Path $Root 'logs') -Force | Out-Null
$log=Join-Path $Root 'logs\ollama.log'
if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 10MB) { Move-Item -LiteralPath $log -Destination ($log+'.1') -Force }
& $cfg.ollama serve *>> $log
exit $LASTEXITCODE
