$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match "interactive_agent_watchdog\.py" -and $_.CommandLine -match [regex]::Escape($root) -and $_.ProcessId -ne $PID }
if ($existing) {
  exit 0
}
python scripts\interactive_agent_watchdog.py
