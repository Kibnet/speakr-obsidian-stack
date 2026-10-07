param([Parameter(Mandatory)][string]$LegacyRoot,[string]$Base=(Join-Path $env:TEMP ('adopt-fixture-'+[guid]::NewGuid().ToString('N'))),[string]$Python='python.exe',[int]$Port=19385,[switch]$BootstrapServer,
 [ValidateSet('AfterBegin','AfterPause','AfterWithdraw','AfterGuards','AfterHolds','MiddleCopy','AfterTasks','AfterCommit')][string[]]$Boundaries=@('AfterBegin','AfterPause','AfterWithdraw','AfterGuards','AfterHolds','MiddleCopy','AfterTasks','AfterCommit'))
$ErrorActionPreference='Stop'
$Python=(Get-Command $Python).Source
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {throw 'Fixture port already in use; choose another -Port'}
$package=Split-Path $PSScriptRoot -Parent
$adopt=Join-Path $package 'scripts\adopt.ps1'
New-Item -ItemType Directory -Path $Base | Out-Null
$source=@'
using System; using System.Net; using System.Text;
class Fixture {
 static void Main(string[] args) {
  Console.Error.WriteLine("fixture native stderr is an ordinary startup log");
  int port=args.Length>1?int.Parse(args[1]):int.Parse(Environment.GetEnvironmentVariable("OLLAMA_HOST").Split(':')[1]);var listener=new System.Net.Sockets.TcpListener(IPAddress.Loopback,port);listener.Start();
  while(true) {using(var c=listener.AcceptTcpClient()) {var s=c.GetStream();byte[] request=new byte[16384];int n=s.Read(request,0,request.Length);string path=Encoding.UTF8.GetString(request,0,n).Split(' ')[1];string result=path=="/api/tags"?"{\"models\":[{\"name\":\"fixture-model:latest\"}]}":"{\"status\":\"ok\"}";byte[] b=Encoding.UTF8.GetBytes(result);byte[] header=Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "+b.Length+"\r\nConnection: close\r\n\r\n");s.Write(header,0,header.Length);s.Write(b,0,b.Length);}}
 }
}
'@
$src=Join-Path $Base 'fixture.cs'; $source | Set-Content -LiteralPath $src -Encoding UTF8
$compiler=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compiler /nologo /target:winexe ('/out:'+(Join-Path $Base 'ollama.exe')) $src
if ($LASTEXITCODE -ne 0) {throw 'Fixture compiler failed'}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'adoption_docker_stub.py') -Destination (Join-Path $Base 'adoption_docker_stub.py')
('@echo off'+"`r`n"+'@"'+$Python+'" "%~dp0adoption_docker_stub.py" %*') | Set-Content -LiteralPath (Join-Path $Base 'docker.cmd') -Encoding ASCII
$cfg=& $Python (Join-Path $PSScriptRoot 'adoption_fixture.py') create --base $Base --legacy $LegacyRoot --port $Port | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {throw 'Fixture creation failed'}
$root=Join-Path $Base 'runtime\automation';$plan=Join-Path $Base 'plan.json'
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach ($pair in @(@($cfg.bridge_task,'bridge.py',' --loop'),@($cfg.watchdog_task,'watchdog.py',''))) {
 $a=New-ScheduledTaskAction -Execute $cfg.pythonw -Argument ('"'+(Join-Path $root $pair[1])+'"'+$pair[2]) -WorkingDirectory $root
 Register-ScheduledTask -TaskName $pair[0] -Action $a -Principal $principal -Settings $settings | Out-Null
}
$oldLauncher=Join-Path $Base 'runtime\llm\start-ollama.ps1'
$a=New-ScheduledTaskAction -Execute (Get-Command powershell.exe).Source -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$oldLauncher+'"') -WorkingDirectory (Split-Path $oldLauncher)
Register-ScheduledTask -TaskName $cfg.ollama_task -Action $a -Principal $principal -Settings $settings | Out-Null
$ollamaPid=$null
function Run-Native([string[]]$Arguments) {
 & powershell.exe -NoProfile -File $adopt -Root $root -Plan $plan -Python $Python @Arguments
 return $LASTEXITCODE
}
try {
 if ($BootstrapServer) {
  # Diagnostic fixture setup: start the synthetic GUI-subsystem server directly,
  # then restore the known legacy definition while its task instance is running.
  # Cold scheduled PowerShell startup remains a separate acceptance boundary.
  $scheduler=New-Object -ComObject 'Schedule.Service';$scheduler.Connect()
  $registered=$scheduler.GetFolder('\').GetTask($cfg.ollama_task)
  $legacyXml=$registered.Xml
  $definition=$scheduler.NewTask(0);$definition.XmlText=$legacyXml
  $definition.Actions.Item(1).Path=Join-Path $Base 'ollama.exe'
  $definition.Actions.Item(1).Arguments='serve '+$Port
  $scheduler.GetFolder('\').RegisterTaskDefinition($cfg.ollama_task,$definition,4,$definition.Principal.UserId,$null,3,$null) | Out-Null
 }
 # Start the old launcher even though automation is paused; verify definition update preserves its process.
 Start-ScheduledTask -TaskName $cfg.ollama_task
 $limit=[DateTime]::UtcNow.AddSeconds(180)
 do {Start-Sleep -Milliseconds 200;$listener=Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue} while (!$listener -and [DateTime]::UtcNow -lt $limit)
 if (!$listener) {Get-ScheduledTaskInfo -TaskName $cfg.ollama_task | Format-List | Out-String | Write-Output; throw 'Fixture old Ollama failed'}
 $ollamaPid=$listener[0].OwningProcess
 if ($BootstrapServer) {
  $definition=$scheduler.NewTask(0);$definition.XmlText=$legacyXml
  $scheduler.GetFolder('\').RegisterTaskDefinition($cfg.ollama_task,$definition,4,$definition.Principal.UserId,$null,3,$null) | Out-Null
  if ($scheduler.GetFolder('\').GetTask($cfg.ollama_task).State -ne 4) {throw 'Bootstrap definition update stopped task instance'}
 }
 foreach ($failure in $Boundaries) {
  Write-Output ('Checking Apply crash boundary: '+$failure)
  & $adopt -Root $root -Plan $plan -Python $Python -Action Inspect | Out-Null
  if ($failure -eq 'AfterWithdraw') {Start-ScheduledTask -TaskName $cfg.bridge_task; Start-Sleep -Milliseconds 500}
  $exit=Run-Native @('-Action','Apply','-Candidate','-InjectFailure',$failure)
  if (@($exit)[-1] -eq 0) {throw ('Crash injection failed '+$failure)}
  $checkpoint=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $root 'adoption-state.json') | ConvertFrom-Json
  if ($failure -in @('AfterGuards','AfterHolds','MiddleCopy','AfterTasks','AfterCommit') -and $checkpoint.phase -notin @('guarding','staging','installed')) {throw ('Injection did not reach required boundary '+$failure)}
  if ($failure -eq 'AfterCommit' -and $checkpoint.phase -ne 'installed') {throw 'Commit checkpoint not reached'}
  if ($failure -notin @('AfterBegin','AfterPause','AfterCommit')) {
   $ErrorActionPreference='Continue'
   & $Python (Join-Path $root 'bridge.py') --once 2>$null
   $ErrorActionPreference='Stop'
   if ($LASTEXITCODE -eq 0) {throw 'Partial old/new Bridge launch was not refused'}
  }
  & $Python (Join-Path $PSScriptRoot 'adoption_fixture.py') check --base $Base
  if ($LASTEXITCODE -ne 0) {throw 'Crash changed data'}
  & $adopt -Root $root -Plan $plan -Python $Python -Action Rollback | Out-Null
  if ((Get-NetTCPConnection -LocalPort $Port -State Listen).OwningProcess -ne $ollamaPid) {throw 'Rollback stopped Ollama'}
 }
 foreach ($rollbackFailure in @('RollbackAfterPause','RollbackAfterRestore')) {
  Write-Output ('Checking Rollback replay boundary: '+$rollbackFailure)
  & $adopt -Root $root -Plan $plan -Python $Python -Action Inspect | Out-Null
  & $adopt -Root $root -Plan $plan -Python $Python -Action Apply | Out-Null
  $exit=Run-Native @('-Action','Rollback','-Candidate','-InjectFailure',$rollbackFailure)
  if (@($exit)[-1] -eq 0) {throw ('Rollback crash injection failed '+$rollbackFailure)}
  & $adopt -Root $root -Plan $plan -Python $Python -Action Rollback | Out-Null
  & $Python (Join-Path $PSScriptRoot 'adoption_fixture.py') check --base $Base
  if ($LASTEXITCODE -ne 0) {throw 'Rollback replay changed data'}
 }
 & $adopt -Root $root -Plan $plan -Python $Python -Action Inspect | Out-Null
 & $adopt -Root $root -Plan $plan -Python $Python -Action Apply | Out-Null
 & $adopt -Root $root -Plan $plan -Python $Python -Action Verify
 if ((Get-NetTCPConnection -LocalPort $Port -State Listen).OwningProcess -ne $ollamaPid) {throw 'Task update restarted Ollama'}
 # Resume validates owned actions; the running old server is reused. Paused original is kept for rollback checks.
 $resumeAt=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0
 & (Join-Path $root 'recovery-control.ps1') -Root $root -Action Resume | Out-Null
 $deadline=[DateTime]::UtcNow.AddSeconds(180)
 do {
  Start-Sleep -Milliseconds 250
  $h=$null
  if (Test-Path -LiteralPath (Join-Path $root 'heartbeat.json')) {$h=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $root 'heartbeat.json') | ConvertFrom-Json}
 } while ((!$h -or $h.updated -lt $resumeAt -or $h.quiesced) -and [DateTime]::UtcNow -lt $deadline)
 if (!$h -or $h.updated -lt $resumeAt -or $h.quiesced) {throw 'Adopted Bridge did not produce fresh active heartbeat'}
 & $Python (Join-Path $PSScriptRoot 'adoption_fixture.py') check --base $Base
 if ($LASTEXITCODE -ne 0) {throw 'Resume changed protected historical data'}
 # Upgrade a copied package with one harmless observable module-byte change.
 $candidate=Join-Path $Base 'next-package'; New-Item -ItemType Directory -Path $candidate | Out-Null
 foreach ($dir in @('scripts','automation')) {Copy-Item -LiteralPath (Join-Path $package $dir) -Destination (Join-Path $candidate $dir) -Recurse}
 Add-Content -LiteralPath (Join-Path $candidate 'automation\baseline.py') -Value '# native upgrade fixture'
 & (Join-Path $candidate 'scripts\install.ps1') -TargetRoot (Join-Path $Base 'runtime') -Action Upgrade -Python $Python
 & (Join-Path $root 'recovery-control.ps1') -Root $root -Action Resume | Out-Null
 if ((Get-NetTCPConnection -LocalPort $Port -State Listen).OwningProcess -ne $ollamaPid) {throw 'Upgrade restarted Ollama'}
 Write-Output ('PASS: native legacy boundaries='+($Boundaries -join ',')+'; rollback replay/holds/tasks/PS5 stderr/Resume/adopted Upgrade')
} finally {
 # Only uniquely named synthetic tasks/processes belong to this fixture.
 foreach ($name in @($cfg.bridge_task,$cfg.watchdog_task,$cfg.ollama_task)) {Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue;Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue}
 if ($ollamaPid) {$p=Get-Process -Id $ollamaPid -ErrorAction SilentlyContinue;if ($p -and $p.Path -eq (Join-Path $Base 'ollama.exe')) {Stop-Process -Id $ollamaPid}}
}
