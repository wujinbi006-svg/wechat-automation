Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$port = 8010
$existing = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
  $owner = Get-Process -Id $existing.OwningProcess -ErrorAction SilentlyContinue
  $name = if ($owner) { $owner.ProcessName } else { "unknown" }
  Write-Error "Gateway port $port is already in use by PID $($existing.OwningProcess) ($name). Stop the intended process or choose another port."
}
python -m uvicorn api:app --host 127.0.0.1 --port 8010
