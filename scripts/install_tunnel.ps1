# Exposes the dashboard over HTTPS with a Cloudflare quick tunnel (no account needed).
# Installs cloudflared with winget, registers a logon task that keeps the tunnel up, and
# prints the public https://....trycloudflare.com URL (also written to logs\tunnel-url.txt).
#   powershell -ExecutionPolicy Bypass -File scripts\install_tunnel.ps1
# Note: a quick tunnel gets a NEW random URL every time it restarts. For a fixed address,
# log in to Cloudflare and create a named tunnel on your own domain instead.
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$port = 8080
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
  $m = Select-String -Path $envFile -Pattern '^GT_PORT=(\d+)' | Select-Object -First 1
  if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
  Write-Host "Installing cloudflared with winget..."
  winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements | Out-Null
  $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [Environment]::GetEnvironmentVariable("PATH", "User")
}
$exe = (Get-Command cloudflared -ErrorAction Stop).Source
$logs = Join-Path $root "logs"; New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs "tunnel.log"
$bat = Join-Path $root "scripts\run_tunnel.bat"
@"
@echo off
cd /d "$root"
"$exe" tunnel --no-autoupdate --url http://localhost:$port --logfile "$log" --loglevel info
"@ | Set-Content -Encoding ASCII $bat

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Days 3650) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName "GoldTraderTunnel" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
if (Test-Path $log) { Remove-Item $log -Force }
Start-ScheduledTask -TaskName "GoldTraderTunnel"

Write-Host "Waiting for the tunnel URL..."
$url = $null
for ($i = 0; $i -lt 40 -and -not $url; $i++) {
  Start-Sleep -Seconds 1
  if (Test-Path $log) {
    $hit = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
    if ($hit) { $url = $hit.Matches[0].Value }
  }
}
if ($url) {
  $url | Set-Content (Join-Path $logs "tunnel-url.txt")
  Write-Host ""
  Write-Host "Dashboard URL: $url" -ForegroundColor Green
  Write-Host "Open it in Chrome on your phone, enter GT_TOKEN from .env, then menu -> Add to Home screen."
} else {
  Write-Host "No URL yet; check $log" -ForegroundColor Yellow
}
