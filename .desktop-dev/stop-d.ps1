# D:\pywork\DeepTutor\.desktop-dev\stop-d.ps1
# D 模式一键停机：关掉 launch-d.ps1 打开的 后端 / 前端 / 壳 三个窗口
#   powershell -ExecutionPolicy Bypass -File .\.desktop-dev\stop-d.ps1
#   ... -DryRun        只看会杀谁，不动手
#   ... -KeepWindows   杀进程但保留三个控制台窗口
[CmdletBinding()]
param([switch]$DryRun, [switch]$KeepWindows)

$ErrorActionPreference = 'Stop'
$Root         = 'D:\pywork\DeepTutor'
$BackendPort  = 8011
$FrontendPort = 3811
$SelfPid      = $PID

# 自身与整条祖先链都受保护：若在某个 'DT …' 窗口里运行本脚本，按窗口标题关宿主
# 会连带杀掉脚本自己和它的父终端。
function Get-ProtectedPids {
    $set = New-Object System.Collections.Generic.HashSet[int]
    [void]$set.Add($SelfPid)
    $cur = $SelfPid
    $guard = 0
    while ($cur -and $guard -lt 32) {
        $guard++
        $row = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -Property ParentProcessId -ErrorAction SilentlyContinue
        if (-not $row) { break }
        $parent = [int]$row.ParentProcessId
        if ($parent -le 0) { break }
        if (-not $set.Add($parent)) { break }
        $cur = $parent
    }
    return $set
}
$ProtectedPids = Get-ProtectedPids

function Get-ListenerPid {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        if ($conn) { return [int](($conn | Select-Object -First 1).OwningProcess) }
    } catch {
        $row = netstat -ano | Select-String -Pattern ":$Port\s+.*LISTENING" | Select-Object -First 1
        if ($row -and ($row.ToString().Trim() -match '(\d+)\s*$')) { return [int]$Matches[1] }
    }
    return $null
}

function Find-ByCmdline {
    # -Needles：任一命中即可。刻意用可执行名/模块名，而不是 'deeptutor'
    #（后者会命中路径里的仓库目录名 DeepTutor，导致误杀同目录下的其它进程）。
    param([string[]]$Needles, [int]$Port = 0)
    $hits = @()
    foreach ($p in (Get-CimInstance Win32_Process -Property ProcessId, Name, CommandLine)) {
        $cl = $p.CommandLine
        if (-not $cl) { continue }
        $matched = $false
        foreach ($n in $Needles) {
            if ($cl -like "*$n*") { $matched = $true; break }
        }
        if (-not $matched) { continue }
        if ($Port -gt 0 -and $cl -notlike "*$Port*") { continue }
        if ($ProtectedPids.Contains([int]$p.ProcessId)) { continue }
        $hits += [int]$p.ProcessId
    }
    return $hits
}

function Invoke-TreeKill {
    param([int]$ProcId, [string]$Label)
    if ($ProtectedPids.Contains($ProcId)) {
        Write-Host ("  ! {0}  pid={1}  属于本脚本自身/祖先链，跳过" -f $Label, $ProcId) -ForegroundColor DarkYellow
        return
    }
    $proc = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host ("  - {0}  pid={1}  已不在" -f $Label, $ProcId) -ForegroundColor DarkGray
        return
    }
    if ($DryRun) {
        Write-Host ("  ~ {0}  pid={1} ({2})  将 taskkill /T /F" -f $Label, $ProcId, $proc.ProcessName) -ForegroundColor Yellow
        return
    }
    $null = taskkill /PID $ProcId /T /F 2>&1
    Start-Sleep -Milliseconds 250
    if (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) {
        try { Stop-Process -Id $ProcId -Force -ErrorAction Stop } catch { }
    }
    Write-Host ("  + {0}  pid={1}  已终止" -f $Label, $ProcId) -ForegroundColor Green
}

Write-Host "`n== DeepTutor 本地开发（D 模式）停机 ==" -ForegroundColor Cyan
Write-Host ("   根目录 : {0}" -f $Root)
Write-Host ("   端口   : {0} 后端 / {1} 前端" -f $BackendPort, $FrontendPort)
if ($DryRun) { Write-Host "   模式   : DryRun（只看不动手）" -ForegroundColor Yellow }

# ---- 1. 壳 ----------------------------------------------------------------
$shellHits = @(Find-ByCmdline -Needles @('desktop.main'))
if ($shellHits.Count -eq 0) { Write-Host "  - 壳  未在运行" -ForegroundColor DarkGray }
foreach ($targetPid in $shellHits) { Invoke-TreeKill -ProcId $targetPid -Label '壳 desktop.main' }

# ---- 2. 后端监督进程（/T 会带走 --reload 的 worker，避免"杀了又活"） -------
$backendHits = @(Find-ByCmdline -Needles @('deeptutor.exe', 'deeptutor_cli.main') -Port $BackendPort)
if ($backendHits.Count -eq 0) { Write-Host "  - 后端 未在运行" -ForegroundColor DarkGray }
foreach ($targetPid in $backendHits) { Invoke-TreeKill -ProcId $targetPid -Label "后端 deeptutor serve :$BackendPort" }

# ---- 3. 前端 Next dev server ----------------------------------------------
$frontendHits = @(Find-ByCmdline -Needles @('next') -Port $FrontendPort)
if ($frontendHits.Count -eq 0) { Write-Host "  - 前端 未在运行" -ForegroundColor DarkGray }
foreach ($targetPid in $frontendHits) { Invoke-TreeKill -ProcId $targetPid -Label "前端 next dev :$FrontendPort" }

# ---- 4. 端口清扫（补 taskkill /T 够不到的 detach 孙进程） -----------------
foreach ($pair in @(, @($BackendPort, '后端')) + @(, @($FrontendPort, '前端'))) {
    $port = $pair[0]
    $name = $pair[1]
    $sweepPid = Get-ListenerPid -Port $port
    if ($sweepPid -and -not $ProtectedPids.Contains($sweepPid)) {
        Invoke-TreeKill -ProcId $sweepPid -Label "$name 端口清扫 :$port"
    }
}

# ---- 5. 关闭三个控制台窗口 ------------------------------------------------
if ($KeepWindows) {
    Write-Host "`n   -KeepWindows：保留控制台窗口（进程已停）" -ForegroundColor DarkGray
} else {
    Start-Sleep -Milliseconds 300
    # 首选按「命令行」匹配，且只认 launch-d.ps1 才会写出的完整标题字面串
    # WindowTitle='DT backend|web|shell。用宽泛的 'DT shell' 匹配命令行，会把任何
    # 「只是提到该字样」的进程（例如一条 grep 命令）误判成窗口。MainWindowTitle
    # 在无桌面/隐藏会话下为空，只能当兜底。
    $cmdNeedles   = @("WindowTitle='DT backend", "WindowTitle='DT web", "WindowTitle='DT shell")
    $titleNeedles = @('DT backend', 'DT web', 'DT shell')
    $hosts = @{}
    foreach ($p in (Get-CimInstance Win32_Process -Property ProcessId, Name, CommandLine)) {
        $cl = $p.CommandLine
        if (-not $cl) { continue }
        if ($p.Name -notlike 'powershell*' -and $p.Name -notlike 'pwsh*') { continue }
        $hit = $false
        foreach ($n in $cmdNeedles) { if ($cl -like "*$n*") { $hit = $true; break } }
        if (-not $hit) { continue }
        $label = 'DT window'
        if ($cl -match "WindowTitle='([^']*)'") { $label = $Matches[1] }
        $hosts[[int]$p.ProcessId] = $label
    }
    # 兜底：按窗口标题
    foreach ($proc in (Get-Process -ErrorAction SilentlyContinue)) {
        $t = $proc.MainWindowTitle
        if (-not $t) { continue }
        $matched = $null
        foreach ($n in $titleNeedles) { if ($t -like "*$n*") { $matched = $t; break } }
        if ($matched -and -not $hosts.ContainsKey($proc.Id)) { $hosts[$proc.Id] = $matched }
    }
    if ($hosts.Count -eq 0) {
        Write-Host "  - 未找到 DT 窗口（可能已随进程退出）" -ForegroundColor DarkGray
    }
    foreach ($key in @($hosts.Keys)) {
        Invoke-TreeKill -ProcId $key -Label ("窗口 [{0}]" -f $hosts[$key])
    }
}

# ---- 6. 校验 --------------------------------------------------------------
if (-not $DryRun) {
    Start-Sleep -Milliseconds 500
    $leftBackend  = Get-ListenerPid -Port $BackendPort
    $leftFrontend = Get-ListenerPid -Port $FrontendPort
    $leftover = @()
    $leftover += @(Find-ByCmdline -Needles @('desktop.main'))
    $leftover += @(Find-ByCmdline -Needles @('deeptutor.exe', 'deeptutor_cli.main') -Port $BackendPort)
    $leftover += @(Find-ByCmdline -Needles @('next') -Port $FrontendPort)

    Write-Host "`n== 结果 ==" -ForegroundColor Cyan
    if ($leftBackend) {
        Write-Host ("   !! :{0} 仍被 pid={1} 占用" -f $BackendPort, $leftBackend) -ForegroundColor Red
    } else {
        Write-Host ("   OK :{0} 已释放" -f $BackendPort) -ForegroundColor Green
    }
    if ($leftFrontend) {
        Write-Host ("   !! :{0} 仍被 pid={1} 占用" -f $FrontendPort, $leftFrontend) -ForegroundColor Red
    } else {
        Write-Host ("   OK :{0} 已释放" -f $FrontendPort) -ForegroundColor Green
    }
    if ($leftover.Count -gt 0) {
        Write-Host ("   !! 仍有 {0} 个相关进程存活：{1}" -f $leftover.Count, ($leftover -join ', ')) -ForegroundColor Red
    } else {
        Write-Host "   OK 无残留进程" -ForegroundColor Green
    }
}
