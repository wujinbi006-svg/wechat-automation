param([switch]$DryRun)
$root = Split-Path $PSScriptRoot -Parent
$config = Join-Path $root "service\WeChatGateway.xml"
if (-not (Test-Path $config)) { throw "WinSW config missing: $config" }
if ($DryRun) { Write-Host "DRY RUN: validated $config"; exit 0 }
$winswSource = Join-Path $root "service\WinSW.exe"
if (-not (Test-Path $winswSource)) { throw "WinSW.exe missing: $winswSource" }
$winsw = Join-Path $root "service\WeChatGateway.exe"
if (-not (Test-Path $winsw)) {
  Copy-Item -LiteralPath $winswSource -Destination $winsw
}
& $winsw install
& $winsw start
