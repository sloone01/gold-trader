# Restarts the Gold Trader server cleanly. Stopping the scheduled task only kills the cmd wrapper,
# so this also ends the python process that is still listening on the port.
#   powershell -ExecutionPolicy Bypass -File scripts\restart_server.ps1
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$port = 8080
$m = Select-String -Path (Join-Path $root ".env") -Pattern '^GT_PORT=(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }

Stop-ScheduledTask -TaskName GoldTrader -ErrorAction SilentlyContinue
$owners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($pid_ in $owners) {
  Write-Host "Stopping old server process $pid_"
  Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName GoldTrader
Start-Sleep -Seconds 6
try {
  $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/api/me" -TimeoutSec 5
  Write-Host "Server is up (HTTP $($r.StatusCode), no token needed)" -ForegroundColor Green
} catch {
  $code = $_.Exception.Response.StatusCode.value__
  if ($code -eq 401) { Write-Host "Server is up (token required)" -ForegroundColor Green }
  else { Write-Host "Server did not answer; see $root\logs\server.log" -ForegroundColor Yellow }
}
