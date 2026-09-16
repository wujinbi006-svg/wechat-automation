Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$outDir = Join-Path $PSScriptRoot "..\..\docs\evidence"
$outDir = [IO.Path]::GetFullPath($outDir)
New-Item -ItemType Directory -Force $outDir | Out-Null
$proc = Get-Process Weixin | Where-Object {$_.MainWindowHandle -ne 0} | Select-Object -First 1
if (-not $proc) { throw "未找到带主窗口的 Weixin 进程" }
$root = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
$interesting = "ChatSessionList|XTableView|ChatSessionCell|ChatMessagePage|MessageView|RecyclerListView|ChatInputField"
function Snapshot($path) {
  $nodes = New-Object System.Collections.Generic.List[object]
  $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  foreach ($e in $all) {
    $cls = [string]$e.Current.ClassName
    if ($cls -notmatch $interesting) { continue }
    $r = $e.Current.BoundingRectangle
    $nodes.Add([ordered]@{
      name=[string]$e.Current.Name; class_name=$cls; automation_id=[string]$e.Current.AutomationId
      control_type=[string]$e.Current.ControlType.ProgrammaticName
      runtime_id=(([int[]]$e.GetRuntimeId()) -join ".")
      rect=@($r.X,$r.Y,$r.Width,$r.Height)
      patterns=@(
        [System.Windows.Automation.InvokePattern]::Pattern,
        [System.Windows.Automation.SelectionItemPattern]::Pattern,
        [System.Windows.Automation.ValuePattern]::Pattern
      ) | Where-Object { try { $null -ne $e.GetCurrentPattern($_) } catch { $false } } |
        ForEach-Object { $_.ProgrammaticName }
    }) | Out-Null
  }
  $nodes | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $path
}
$events = New-Object System.Collections.Generic.List[object]
$handler = [System.Windows.Automation.AutomationEventHandler]{
  param($sender,$args)
  try {
    $e = [System.Windows.Automation.AutomationElement]$sender
    $events.Add([ordered]@{timestamp=(Get-Date).ToString("o"); event_name="AutomationEvent"; event_id=$args.EventId.Id; name=$e.Current.Name; class_name=$e.Current.ClassName; automation_id=$e.Current.AutomationId; control_type=$e.Current.ControlType.ProgrammaticName}) | Out-Null
  } catch {}
}
[System.Windows.Automation.Automation]::AddAutomationEventHandler([System.Windows.Automation.InvokePattern]::InvokedEvent, $root, [System.Windows.Automation.TreeScope]::Descendants, $handler)
[System.Windows.Automation.Automation]::AddAutomationEventHandler([System.Windows.Automation.SelectionItemPattern]::ElementSelectedEvent, $root, [System.Windows.Automation.TreeScope]::Descendants, $handler)
Snapshot (Join-Path $outDir "navigation_before.json")
Write-Host "LISTENING. Manually click once in WeChat: Friend B -> File Transfer Assistant. Then press Enter here."
[Console]::ReadLine() | Out-Null
Snapshot (Join-Path $outDir "navigation_after.json")
[System.Windows.Automation.Automation]::RemoveAutomationEventHandler([System.Windows.Automation.InvokePattern]::InvokedEvent, $root, $handler)
[System.Windows.Automation.Automation]::RemoveAutomationEventHandler([System.Windows.Automation.SelectionItemPattern]::ElementSelectedEvent, $root, $handler)
$events | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 (Join-Path $outDir "navigation_events_observed.json")
Write-Host ("Saved {0} events." -f $events.Count)
