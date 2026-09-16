<#
  database_inventory.ps1 - WeChat 4.x Database Discovery
  P1.2: Find, examine, and document WeChat's database files
#>

$baseDir = "__ROOT__"
$evDir = "$baseDir\docs\evidence"
$logFile = "$evDir\database_inventory_log.txt"
"===== Database Inventory $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" | Out-File -Encoding utf8 $logFile

function Log($M) { $line = "[$(Get-Date -Format 'HH:mm:ss.fff')] $M"; Write-Host $line; Add-Content -Path $logFile -Value $line }

# ===== STEP 1: Find WeChat process to get user path =====
Log "=== STEP 1: Find WeChat process ==="
$wp = Get-Process -Name "Weixin" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $wp) { Log "ERROR: WeChat not running"; exit }
$procId = $wp.Id
Log ("PID={0} Path={1}" -f $procId, $wp.Path)

# ===== STEP 2: Find WeChat user data directory =====
Log "`n=== STEP 2: Find WeChat user data directory ==="
$wechatDataPaths = @(
    "$env:USERPROFILE\Documents\WeChat Files",
    "$env:USERPROFILE\AppData\Local\WeChat",
    "$env:USERPROFILE\AppData\Roaming\Tencent\WeChat",
    "$env:USERPROFILE\AppData\Local\Tencent\WeChat"
)

$foundDataDir = $null
foreach ($dp in $wechatDataPaths) {
    if (Test-Path $dp) {
        Log ("Found: {0}" -f $dp)
        if ($foundDataDir -eq $null) { $foundDataDir = $dp }
        # List contents
        $items = Get-ChildItem -Path $dp -Depth 0 -ErrorAction SilentlyContinue
        foreach ($item in $items) {
            Log ("  {0} {1} (size={2})" -f $(if ($item.PSIsContainer) { "[DIR]"} else { "[FILE]"}), $item.Name, $item.Length)
        }
    }
}

# ===== STEP 3: Find WeChat Files / user directories =====
Log "`n=== STEP 3: Find WeChat Files user directories ==="
$wechatFilesPath = "$env:USERPROFILE\Documents\WeChat Files"
if (Test-Path $wechatFilesPath) {
    $userDirs = Get-ChildItem -Path $wechatFilesPath -Directory -ErrorAction SilentlyContinue
    Log ("Found {0} user directories:" -f $userDirs.Count)
    foreach ($ud in $userDirs) {
        Log ("  [USER] {0} (lastWrite={1})" -f $ud.Name, $ud.LastWriteTime)
        # Check for database files
        $dbFiles = Get-ChildItem -Path $ud.FullName -Recurse -Include "*.db", "*.db-wal", "*.db-shm", "*.sqlite" -ErrorAction SilentlyContinue -Depth 2
        if ($dbFiles.Count -gt 0) {
            Log ("    Database files:")
            foreach ($df in $dbFiles) {
                Log ("      {0} size={1:N0} modified={2}" -f $df.FullName, $df.Length, $df.LastWriteTime)
            }
        }
        # Check for common subdirectories
        $commonDirs = @("Msg", "MicroMsg", "Config", "File", "Image", "Video", "Voice", "Avatar", "Emotion", "FTS")
        foreach ($cd in $commonDirs) {
            $subPath = Join-Path $ud.FullName $cd
            if (Test-Path $subPath) {
                $subItems = Get-ChildItem -Path $subPath -Depth 0 -ErrorAction SilentlyContinue
                Log ("    [DIR] {0} ({1} items)" -f $cd, $subItems.Count)
            }
        }
    }
}

# ===== STEP 4: Check for SQLCipher magic bytes =====
Log "`n=== STEP 4: Check database file headers ==="
$wechatFilesPath = "$env:USERPROFILE\Documents\WeChat Files"
if (Test-Path $wechatFilesPath) {
    $userDirs = Get-ChildItem -Path $wechatFilesPath -Directory -ErrorAction SilentlyContinue
    foreach ($ud in $userDirs) {
        $dbFiles = Get-ChildItem -Path $ud.FullName -Recurse -Include "*.db", "*.sqlite" -ErrorAction SilentlyContinue -Depth 3
        foreach ($df in $dbFiles) {
            try {
                $fs = [System.IO.File]::OpenRead($df.FullName)
                $header = New-Object byte[] 16
                $fs.Read($header, 0, 16) | Out-Null
                $fs.Close()
                $hex = ($header[0..15] | ForEach-Object { $_.ToString("X2") }) -join " "
                $isSQLite = ($header[0] -eq 0x53 -and $header[1] -eq 0x51)  # "SQ"
                $isSQLCipher = ($header[0] -eq 0x53 -and $header[1] -eq 0x51 -and $header[2] -eq 0x4C)  # "SQL"
                Log ("  {0}: header={1} SQLite={2} SQLCipher={3}" -f $df.Name, $hex, $isSQLite, $isSQLCipher)
            } catch {
                Log ("  {0}: Cannot read (locked?)" -f $df.Name)
            }
        }
    }
}

# ===== STEP 5: Check if WeChat holds database file handles =====
Log "`n=== STEP 5: Check WeChat process handles ==="
# This requires admin privileges, so we note it
Log ("WeChat PID={0} - checking file handles (requires admin)" -f $procId)
try {
    # Alternative: check if SQLite3.dll is loaded in WeChat
    $mods = $wp.Modules | Where-Object { $_.ModuleName -like "*sqlite*" -or $_.ModuleName -like "*SQLite*" }
    if ($mods.Count -gt 0) {
        foreach ($m in $mods) {
            Log ("  SQLite module: {0} base=0x{1:X}" -f $m.ModuleName, $m.BaseAddress.ToInt64())
        }
    } else {
        Log ("  No SQLite module found in WeChat process")
    }
} catch { Log ("  Cannot check modules: {0}" -f $_.Exception.Message) }

# ===== STEP 6: Check for FTS (Full Text Search) databases =====
Log "`n=== STEP 6: Check for FTS databases ==="
$wechatFilesPath = "$env:USERPROFILE\Documents\WeChat Files"
if (Test-Path $wechatFilesPath) {
    $userDirs = Get-ChildItem -Path $wechatFilesPath -Directory -ErrorAction SilentlyContinue
    foreach ($ud in $userDirs) {
        $ftsDirs = Get-ChildItem -Path $ud.FullName -Recurse -Directory -Include "*FTS*", "*fts*" -ErrorAction SilentlyContinue -Depth 3
        foreach ($fd in $ftsDirs) {
            Log ("  FTS directory: {0}" -f $fd.FullName)
            $ftsFiles = Get-ChildItem -Path $fd.FullName -File -ErrorAction SilentlyContinue
            foreach ($ff in $ftsFiles) {
                Log ("    {0} size={1:N0}" -f $ff.Name, $ff.Length)
            }
        }
    }
}

# ===== SUMMARY =====
Log "`n`n================================================"
Log "CHECKPOINT 3: DATABASE DISCOVERY"
Log "================================================"
Log ("WeChat PID: {0}" -f $procId)
Log ("WeChat Path: {0}" -f $wp.Path)
if ($foundDataDir) { Log ("Data directory: {0}" -f $foundDataDir) }
else { Log ("Data directory: NOT FOUND") }
Log ("`nFull log: $logFile")