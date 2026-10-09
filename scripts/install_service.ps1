# Registers a Task Scheduler job that starts the server at logon and restarts it if it dies.
# MT5 is a desktop app, so the task runs in the logged-in session (the VPS must stay logged in
# with the MT5 terminal open, like it does now). Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$bat = Join-Path $root "scripts\run_server.bat"
if (-not (Test-Path (Join-Path $root ".env"))) {
  Write-Host "Create $root\.env first (copy .env.example and set GT_TOKEN)." -ForegroundColor Yellow
  exit 1
}
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Days 3650) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName "GoldTrader" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "GoldTrader"
Write-Host "Task 'GoldTrader' installed and started. Logs: $root\logs\server.log"
Write-Host "Stop:   Stop-ScheduledTask -TaskName GoldTrader"
Write-Host "Remove: Unregister-ScheduledTask -TaskName GoldTrader -Confirm:`$false"
