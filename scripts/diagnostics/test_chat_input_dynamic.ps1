<#
  test_chat_input_dynamic.ps1 v2 - ChatInputField dynamic validation
  P0.1: Open chat -> scan UIA tree -> diff -> find ChatInputField
#>

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class Win32 {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetClassName(IntPtr hWnd, StringBuilder cls, int count);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint procId);
    [DllImport("kernel32.dll")] public static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, uint dwProcessId);
    [DllImport("kernel32.dll")] public static extern bool ReadProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, int dwSize, out int lpNumberOfBytesRead);
    [DllImport("kernel32.dll")] public static extern bool WriteProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, int dwSize, out int lpNumberOfBytesWritten);
    [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr hObject);
    public const uint PROCESS_ALL_ACCESS = 0x1FFFFF;
}
"@

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$baseDir = "__ROOT__"
$evDir = "$baseDir\docs\evidence"
$logFile = "$evDir\chat_input_dynamic_log.txt"
"===== ChatInputField Dynamic Validation v2 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Out-File -Encoding utf8 $logFile

function Log($M) { $line = "[$(Get-Date -Format 'HH:mm:ss.fff')] $M"; Write-Host $line; Add-Content -Path $logFile -Value $line }

function Get-Foreground {
    $h = [Win32]::GetForegroundWindow()
    $sb=New-Object System.Text.StringBuilder 256; $cls=New-Object System.Text.StringBuilder 256
    $t=""; $c=""; [Win32]::GetWindowText($h,$sb,256)|Out-Null; $t=$sb.ToString().Trim()
    [Win32]::GetClassName($h,$cls,256)|Out-Null; $c=$cls.ToString()
    $p=0; [Win32]::GetWindowThreadProcessId($h,[ref]$p)|Out-Null
    $proc=(Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName
    return @{hwnd="0x$($h.ToString('X'))"; pid=$p; process=$proc; title=$t; _raw=$h; _pid=$p}
}

function WalkTree($elem, $depth=0, $maxDepth=5) {
    $result = @()
    try {
        $name = $elem.Current.Name
        $cls = $elem.Current.ClassName
        $ctrl = $elem.Current.ControlType.ProgrammaticName
        $en = $elem.Current.IsEnabled
        $aid = $elem.Current.AutomationId
        $result += [PSCustomObject]@{Name=$name; ClassName=$cls; ControlType=$ctrl; IsEnabled=$en; AutomationId=$aid; Depth=$depth}
        if ($depth -lt $maxDepth) {
            $children = $elem.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
            for ($i = 0; $i -lt $children.Count; $i++) {
                $result += WalkTree $children[$i] ($depth+1) $maxDepth
            }
        }
    } catch {}
    return $result
}

# ===== STEP 1: Find WeChat & Activate Gate =====
Log "=== STEP 1: Find WeChat ==="
$wp = Get-Process -Name "Weixin" -ErrorAction SilentlyContinue | Select-Object -First 1
$procId = $wp.Id
$hwnd = $wp.MainWindowHandle
Log ("PID={0} HWND=0x{1:X}" -f $procId, $hwnd)

$dllBase = 0L; $mods = $wp.Modules | Where-Object { $_.ModuleName -like "*Weixin.dll" }
foreach ($m in $mods) { $dllBase = $m.BaseAddress.ToInt64() }
Log ("Weixin.dll base=0x{0:X}" -f $dllBase)

$hProcess = [Win32]::OpenProcess([Win32]::PROCESS_ALL_ACCESS, $false, $procId)
$gateAddr = [IntPtr]::new($dllBase + 0xad19668)
$buf = New-Object byte[] 1; $br = 0
[Win32]::ReadProcessMemory($hProcess, $gateAddr, $buf, 1, [ref]$br)
$oldVal = $buf[0]
if ($oldVal -eq 0) { $buf[0]=1; $bw=0; [Win32]::WriteProcessMemory($hProcess, $gateAddr, $buf, 1, [ref]$bw); Log ("Gate activated: 0->1") }
else { Log ("Gate already active: {0}" -f $oldVal) }
[Win32]::CloseHandle($hProcess)

$weChatElem = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
Log ("UIA element: Name='{0}' Class='{1}'" -f $weChatElem.Current.Name, $weChatElem.Current.ClassName)

# ===== STEP 2: Record BEFORE =====
Log "`n=== STEP 2: Scan UIA tree BEFORE open_chat ==="
$fgBefore = Get-Foreground
Log ("Foreground BEFORE: {0} (pid={1})" -f $fgBefore.process, $fgBefore.pid)
$treeBefore = WalkTree $weChatElem 0 5
$mmuiBefore = $treeBefore | Where-Object { $_.ClassName -like "*mmui*" -or $_.ClassName -like "*MMUI*" }
$inputBefore = $treeBefore | Where-Object { $_.ClassName -like "*Input*" -or $_.ClassName -like "*Edit*" }
Log ("Tree: {0} nodes, {1} mmui, {2} input-like" -f $treeBefore.Count, $mmuiBefore.Count, $inputBefore.Count)
for ($i = 0; $i -lt $inputBefore.Count; $i++) {
    Log ("  Input: {0} name='{1}'" -f $inputBefore[$i].ClassName, $inputBefore[$i].Name)
}
$treeBefore | ConvertTo-Json -Depth 3 -Compress:$false | Out-File -Encoding utf8 "$evDir\tree_before_open_chat.json"
Log ("Saved: tree_before_open_chat.json")

# ===== STEP 3: Find and open chat =====
Log "`n=== STEP 3: Open chat ==="
$cellCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, "mmui::ChatSessionCell")
$cells = $weChatElem.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cellCond)
Log ("Found {0} ChatSessionCell" -f $cells.Count)

$targetCell = $null
for ($i = 0; $i -lt $cells.Count; $i++) {
    $name = $cells[$i].Current.Name
    if ($name -like "*Yang*" -or $name -like "*yang*" -or $name -like "*Yanhan*") {
        $targetCell = $cells[$i]; Log ("Found target: [{0}] name='{1}'" -f $i, $name.Substring(0, [Math]::Min(30, $name.Length))); break
    }
}
if (-not $targetCell -and $cells.Count -gt 1) {
    $targetCell = $cells[1]
    $name = $targetCell.Current.Name
    Log ("Using cell[1]: '{0}'" -f $name.Substring(0, [Math]::Min(30, $name.Length)))
} elseif (-not $targetCell -and $cells.Count -gt 0) {
    $targetCell = $cells[0]
    $name = $targetCell.Current.Name
    Log ("Using cell[0]: '{0}'" -f $name.Substring(0, [Math]::Min(30, $name.Length)))
}

try {
    $selPat = $targetCell.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
    $selPat.Select()
    Log ("SelectionItemPattern.Select() called")
} catch {
    Log ("SelectionItemPattern failed: {0}" -f $_.Exception.Message)
    try {
        $invPat = $targetCell.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $invPat.Invoke()
        Log ("InvokePattern.Invoke() called")
    } catch { Log ("InvokePattern also failed: {0}" -f $_.Exception.Message) }
}

Start-Sleep -Seconds 2
$fgAfter = Get-Foreground
Log ("Foreground AFTER: {0} (pid={1})" -f $fgAfter.process, $fgAfter.pid)
Log ("FG changed: {0} (wechat={1})" -f ($fgBefore._raw -ne $fgAfter._raw), ($fgAfter._pid -eq $procId))

# ===== STEP 4: Scan AFTER with polling =====
Log "`n=== STEP 4: Scan UIA tree AFTER open_chat ==="
$treeAfter = $null
$inputAfter = $null
for ($poll = 1; $poll -le 5; $poll++) {
    $treeAfter = WalkTree $weChatElem 0 5
    $mmuiAfter = $treeAfter | Where-Object { $_.ClassName -like "*mmui*" -or $_.ClassName -like "*MMUI*" }
    $inputAfter = $treeAfter | Where-Object { $_.ClassName -like "*Input*" -or $_.ClassName -like "*Edit*" }
    Log ("  Poll {0}: {1} nodes, {2} mmui, {3} input-like" -f $poll, $treeAfter.Count, $mmuiAfter.Count, $inputAfter.Count)
    if ($inputAfter.Count -gt $inputBefore.Count) { Log ("  -> New input controls found!"); break }
    if ($poll -lt 5) { Start-Sleep -Milliseconds 500 }
}

$treeAfter | ConvertTo-Json -Depth 3 -Compress:$false | Out-File -Encoding utf8 "$evDir\tree_after_open_chat.json"
Log ("Saved: tree_after_open_chat.json")

# ===== STEP 5: Diff =====
Log "`n=== STEP 5: Tree diff ==="
$beforeKeys = @{}
for ($i = 0; $i -lt $treeBefore.Count; $i++) { $key = $treeBefore[$i].ClassName + "|" + $treeBefore[$i].Name; $beforeKeys[$key] = $true }
$afterKeys = @{}
for ($i = 0; $i -lt $treeAfter.Count; $i++) { $key = $treeAfter[$i].ClassName + "|" + $treeAfter[$i].Name; $afterKeys[$key] = $true }

$newNodes = @()
for ($i = 0; $i -lt $treeAfter.Count; $i++) {
    $key = $treeAfter[$i].ClassName + "|" + $treeAfter[$i].Name
    if (-not $beforeKeys.ContainsKey($key)) { $newNodes += $treeAfter[$i] }
}

Log ("New nodes: {0}" -f $newNodes.Count)
for ($i = 0; $i -lt $newNodes.Count; $i++) {
    Log ("  + {0} name='{1}' aid='{2}'" -f $newNodes[$i].ClassName, $newNodes[$i].Name, $newNodes[$i].AutomationId)
}

# Check specifically for ChatInputField
$chatInputFields = $treeAfter | Where-Object { $_.ClassName -like "*ChatInput*" }
Log ("`nChatInputField nodes: {0}" -f $chatInputFields.Count)
for ($i = 0; $i -lt $chatInputFields.Count; $i++) {
    Log ("  {0} name='{1}' aid='{2}'" -f $chatInputFields[$i].ClassName, $chatInputFields[$i].Name, $chatInputFields[$i].AutomationId)
}

# Check all XValidatorTextEdit
$textEdits = $treeAfter | Where-Object { $_.ClassName -eq "mmui::XValidatorTextEdit" }
Log ("`nXValidatorTextEdit nodes: {0}" -f $textEdits.Count)
for ($i = 0; $i -lt $textEdits.Count; $i++) {
    Log ("  name='{0}' aid='{1}'" -f $textEdits[$i].Name, $textEdits[$i].AutomationId)
}

# ===== STEP 6: Identify chat input =====
Log "`n=== STEP 6: Identify chat input field ==="
$realChatInput = $null
if ($chatInputFields.Count -gt 0) { $realChatInput = $chatInputFields[0] }
else {
    $nonSearchEdits = $textEdits | Where-Object { $_.Name -ne "search" -and $_.Name -ne "搜索" }
    if ($nonSearchEdits.Count -gt 0) { $realChatInput = $nonSearchEdits[0] }
    else {
        $newEdits = $newNodes | Where-Object { $_.ClassName -eq "mmui::XValidatorTextEdit" }
        if ($newEdits.Count -gt 0) { $realChatInput = $newEdits[0] }
    }
}

if ($realChatInput) {
    Log ("ChatInputField identified: Name='{0}' Class='{1}'" -f $realChatInput.Name, $realChatInput.ClassName)
    $realChatInput | ConvertTo-Json -Depth 3 | Out-File -Encoding utf8 "$evDir\chat_input_field.json"
    Log ("Saved: chat_input_field.json")
} else {
    Log ("ChatInputField NOT identified. Chat input may not be exposed via UIA.")
}

# ===== STEP 7: Pattern audit =====
Log "`n=== STEP 7: Pattern audit ==="
$patternTarget = $null
$editCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, "mmui::XValidatorTextEdit")
$patternTarget = $weChatElem.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $editCond)
if ($patternTarget) {
    Log ("Auditing patterns on: Name='{0}' Class='{1}'" -f $patternTarget.Current.Name, $patternTarget.Current.ClassName)
    $patterns = @{}
    try {
        $supported = $patternTarget.GetSupportedPatterns()
        for ($i = 0; $i -lt $supported.Count; $i++) { $patterns[$supported[$i].ProgrammaticName] = "Supported" }
    } catch {}
    
    $patternChecks = @(
        @{N="ValuePattern"; I=[System.Windows.Automation.ValuePattern]::Pattern},
        @{N="TextPattern"; I=[System.Windows.Automation.TextPattern]::Pattern},
        @{N="InvokePattern"; I=[System.Windows.Automation.InvokePattern]::Pattern},
        @{N="SelectionItemPattern"; I=[System.Windows.Automation.SelectionItemPattern]::Pattern},
        @{N="ScrollPattern"; I=[System.Windows.Automation.ScrollPattern]::Pattern},
        @{N="RangeValuePattern"; I=[System.Windows.Automation.RangeValuePattern]::Pattern}
    )
    for ($i = 0; $i -lt $patternChecks.Length; $i++) {
        try {
            $obj = $patternTarget.GetCurrentPattern($patternChecks[$i].I)
            $patterns[$patternChecks[$i].N] = "AVAILABLE"
            Log ("  {0,-25} = AVAILABLE" -f $patternChecks[$i].N)
        } catch {
            if (-not $patterns.ContainsKey($patternChecks[$i].N)) { $patterns[$patternChecks[$i].N] = "UNAVAILABLE" }
            Log ("  {0,-25} = UNAVAILABLE" -f $patternChecks[$i].N)
        }
    }
    $patterns | ConvertTo-Json -Depth 2 | Out-File -Encoding utf8 "$evDir\chat_input_patterns.json"
    Log ("Saved: chat_input_patterns.json")
}

# ===== SUMMARY =====
Log "`n`n================================================"
Log "CHECKPOINT 1 SUMMARY"
Log "================================================"
if ($chatInputFields.Count -gt 0) { Log ("ChatInputField dynamic: YES - mmui::ChatInputField found") }
else { Log ("ChatInputField dynamic: NO - Using XValidatorTextEdit") }
Log ("ChatInputField ClassName: {0}" -f $(if ($realChatInput) { $realChatInput.ClassName } else { "NOT_FOUND" }))
Log ("ChatInputField Name: {0}" -f $(if ($realChatInput) { $realChatInput.Name } else { "NOT_FOUND" }))
Log ("Patterns: saved to chat_input_patterns.json")
Log ("Tree BEFORE: {0} nodes, {1} mmui" -f $treeBefore.Count, $mmuiBefore.Count)
Log ("Tree AFTER:  {0} nodes, {1} mmui" -f $treeAfter.Count, $mmuiAfter.Count)
Log ("New nodes:   {0}" -f $newNodes.Count)
Log ("FG changed: {0} (wechat_pid={1})" -f ($fgBefore._raw -ne $fgAfter._raw), ($fgAfter._pid -eq $procId))
Log ("`nEvidence files:")
Log ("  $evDir\tree_before_open_chat.json")
Log ("  $evDir\tree_after_open_chat.json")
Log ("  $evDir\chat_input_field.json")
Log ("  $evDir\chat_input_patterns.json")