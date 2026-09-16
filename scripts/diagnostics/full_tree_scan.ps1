Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$wp = Get-Process -Name "Weixin" -ErrorAction SilentlyContinue | Select-Object -First 1
$elem = [System.Windows.Automation.AutomationElement]::FromHandle($wp.MainWindowHandle)
$all = $elem.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
Write-Host ("Total descendants: {0}" -f $all.Count)
$classes = @{}
for ($i = 0; $i -lt $all.Count; $i++) {
    $cls = $all[$i].Current.ClassName
    if (-not $classes.ContainsKey($cls)) { $classes[$cls] = 0 }
    $classes[$cls]++
}
$classes.GetEnumerator() | Sort-Object Name | ForEach-Object { Write-Host ("  {0}: {1}" -f $_.Key, $_.Value) }