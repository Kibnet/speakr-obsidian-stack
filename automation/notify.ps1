param([ValidateSet('Outage','Recovered')][string]$Event)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$icon = [Windows.Forms.NotifyIcon]::new()
try {
    $icon.Icon = [Drawing.SystemIcons]::Information
    $icon.Visible = $true
    $body = if ($Event -eq 'Recovered') {'Сервисы снова доступны. Обработка очереди продолжается.'} else {'Обработка записей задерживается более 10 минут. Проверьте статус Speakr.'}
    $icon.ShowBalloonTip(8000, 'Speakr: обработка записей', $body, [Windows.Forms.ToolTipIcon]::Info)
    $deadline = [DateTime]::UtcNow.AddSeconds(9)
    do { [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 } while ([DateTime]::UtcNow -lt $deadline)
} finally {
    $icon.Dispose()
}
