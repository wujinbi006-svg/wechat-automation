# Remove the wxwal Session-1 stack task and stop any running copies.
#
# Matches processes by SCRIPT PATH, never by process name, so unrelated python
# processes are never touched.

$ErrorActionPreference = "Continue"
$TaskName = "wxwal-hook-daemon"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Host "stopping and removing '$TaskName'"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "task removed"
} else {
    Write-Host "task '$TaskName' not registered"
}

# The stack launcher, plus its two children. Stopping the launcher alone leaves
# the children running, which would keep the pipe owned and block a restart.
foreach ($marker in @('run_hook_stack\.py', 'wal_sidecar\.py', 'daemon\.py')) {
    $mine = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match $marker -and $_.CommandLine -match 'wxwal|hook' }
    foreach ($proc in $mine) {
        Write-Host "stopping $($proc.ProcessId) [$marker]"
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Note: the hook already loaded inside WeChat cannot be unloaded without"
Write-Host "restarting WeChat. The injector will not re-add it if the stack is stopped."
