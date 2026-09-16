param([switch]$DryRun)
if ($DryRun) { Write-Host "DRY RUN: would uninstall service WeChatGateway"; exit 0 }
Write-Error "Uninstall is intentionally not performed in this phase. Use WinSW uninstall only after explicit approval."
