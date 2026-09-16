Get-Service -Name WeChatGateway -ErrorAction SilentlyContinue |
  Select-Object Name, Status, StartType
if (-not $?) { Write-Host "WeChatGateway service is not installed." }
