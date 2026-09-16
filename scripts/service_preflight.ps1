Set-StrictMode -Version Latest
$ErrorActionPreference = "SilentlyContinue"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$winsw = Join-Path $root "service\WinSW.exe"
$xml = Join-Path $root "service\WeChatGateway.xml"
$portInfo = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8010 -State Listen
$owner = $null
if ($portInfo) {
  $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($portInfo.OwningProcess)"
}

$health = $null
$status = $null
try { $health = Invoke-RestMethod "http://127.0.0.1:8010/health" -TimeoutSec 3 } catch {}
try { $status = Invoke-RestMethod "http://127.0.0.1:8010/status" -TimeoutSec 3 } catch {}

$wechat = Get-Process Weixin -ErrorAction SilentlyContinue
$openclaw = Test-Path "__USERPROFILE__\.openclaw\openclaw.json"
$winswValid = $false
if (Test-Path $winsw) {
  $winswValid = ((Get-Item $winsw).Length -gt 100KB)
}

$gatewayOwner = if ($owner) { $owner.Name } else { $null }
$gatewayPathsValid = [bool]($owner -and $owner.CommandLine -match [regex]::Escape($root))
$serviceXmlValid = Test-Path $xml
$portSafe = [bool](!$portInfo -or $gatewayPathsValid)
$ready = [bool]($winswValid -and $serviceXmlValid -and $portSafe -and $openclaw)

[ordered]@{
  generated_at = (Get-Date).ToUniversalTime().ToString("o")
  winsw_present = (Test-Path $winsw)
  winsw_path = $winsw
  winsw_valid = $winswValid
  port_8010 = [ordered]@{
    occupied = [bool]$portInfo
    pid = if ($portInfo) { $portInfo.OwningProcess } else { $null }
    process = $gatewayOwner
    executable = if ($owner) { $owner.ExecutablePath } else { $null }
    command_line = if ($owner) { $owner.CommandLine } else { $null }
    safe_to_proceed = $portSafe
  }
  gateway = [ordered]@{
    running = [bool]$health
    health = $health
    status_endpoint = [bool]$status
    owner_matches_current_repo = $gatewayPathsValid
  }
  gateway_paths_valid = $gatewayPathsValid
  service_xml_valid = $serviceXmlValid
  wechat_process = [bool]$wechat
  openclaw_detected = $openclaw
  openclaw_ws_verified = $false
  ready_for_install = $ready
  result = if ($ready) { "READY_FOR_SERVICE_INSTALL" } else { "BLOCKED" }
} | ConvertTo-Json -Depth 8
