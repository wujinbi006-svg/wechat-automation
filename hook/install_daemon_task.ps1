# Register the wxwal Session-1 stack to run at user logon.
#
# One task runs run_hook_stack.py, which keeps two children alive:
#   - daemon.py      : injects the hook into WeChat, re-injects after a restart
#   - wal_sidecar.py : owns the hook pipe and serves events on 127.0.0.1:18011
#
# Both MUST run in Session 1: the hook is injected into WeChat there, and the
# hook's named pipe cannot be connected from Session 0 where the Gateway
# service lives (Win32 error 5, verified). Restart-on-failure covers the stack
# dying; the stack itself covers its children dying.

$ErrorActionPreference = "Stop"

$Root     = Split-Path -Parent $PSScriptRoot
$Stack    = Join-Path $PSScriptRoot "run_hook_stack.py"
$TaskName = "wxwal-hook-daemon"

# 解释器按以下顺序确定，避免写死本机路径：
#   1) 环境变量 PYTHON
#   2) 运行本脚本所用的解释器（若通过 python 调用 powershell 则会继承）
#   3) py launcher / PATH 中的 python
$Python = $env:PYTHON
if (-not $Python) {
    foreach ($cand in @("py", "python", "python3")) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) {
            if ($cand -eq "py") {
                $resolved = & py -3 -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $resolved) { $Python = $resolved.Trim(); break }
            } else {
                $Python = $cmd.Source; break
            }
        }
    }
}
if (-not $Python) {
    throw "Python not found. Install Python 3.11+ or set `$env:PYTHON to python.exe"
}

if (-not (Test-Path $Stack))  { throw "stack launcher not found: $Stack" }
if (-not (Test-Path $Python)) { throw "python not found: $Python" }
Write-Host "python : $Python"

# A logon task must not be started while another copy is already running,
# otherwise two stacks race to inject the same DLL and to own the one pipe.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "task '$TaskName' already exists; replacing"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$Stack`"" `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# Interactive logon type: the hook must run in the same session as WeChat.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Keeps the wxwal hook injected into WeChat and serves WAL events on 127.0.0.1:18011 for the Gateway." | Out-Null

Write-Host "registered: $TaskName"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize
