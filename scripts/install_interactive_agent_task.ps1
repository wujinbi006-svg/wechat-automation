param([string]$User = $env:USERNAME)
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\start_interactive_agent.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "WeChatGateway Interactive Agent" -Action $action -Trigger $trigger -Settings $settings -User $User -RunLevel Highest -Force
