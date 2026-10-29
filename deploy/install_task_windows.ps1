# 在 Windows 任务计划程序里注册每周二 16:30 运行的任务（中欧时间，全年都在美股交易时段内）。
# 用法：在项目目录的 PowerShell 里运行  powershell -ExecutionPolicy Bypass -File deploy\install_task_windows.ps1
$root = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$root\deploy\run_weekly.ps1`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday -At 16:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "ibkr-agent weekly" -Action $action -Trigger $trigger -Settings $settings -Force
