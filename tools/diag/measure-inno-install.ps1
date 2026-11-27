<#5
.SYNOPSIS
    模块3 诊断脚本：Inno Setup 安装分阶段计时（只运行安装包 + 读取系统状态）。

.DESCRIPTION
    对 ThinkBuddySetup-*.exe 执行一次完整安装（默认静默），从两个通道采集耗时：
      A) 安装器日志（/LOG=）：解析时间戳锚点，以及 installer.iss 的 [Code] 段
         自己写的 TB-DEPLOY 分段计时行（extract / swap / warmup / total）。
      B) 进程级轮询（默认 300ms）：runtime.tar.xz 落盘时间点、
         {app}\runtime.new\python\python.exe 与 {app}\runtime\python\python.exe 出现时间点。
    输出：分阶段耗时表 + 最高耗时 3 个阶段 + JSON（供模块5 对照实验复用）。

    阶段划分（对应 installer.iss 结构）：
      1. 安装器启动 -> 安装流程开始
      2a. 准备 -> 首个 [Files] 文件落盘
      2b. [Files] 文件写入（ThinkBuddyDesktop.exe / 图标 / runtime.tar.xz）
      2c. runtime.tar.xz 单文件写入窗口
      3. ssPostInstall 部署（删旧 runtime + tar 解压 + 改名就位 + 预热导入）
      4. 收尾（[Run] / 卸载记录 / 图标缓存刷新）
      5. 墙钟总计

.PARAMETER SetupPath
    ThinkBuddySetup-*.exe 的完整路径（必填）。

.PARAMETER InstallDir
    安装目标目录，默认 %LOCALAPPDATA%\Programs\ThinkBuddy（对应 iss 的
    PrivilegesRequired=lowest + DefaultDirName={autopf}\ThinkBuddy）。

.PARAMETER CleanTarget
    实验前删除 InstallDir（全新安装基线）。默认不删（覆盖升级路径）。

.PARAMETER Visible
    交互安装（不带 /VERYSILENT），用于观察向导文案；计时请用默认静默模式。

.NOTES
    日志格式假设：Inno Setup 的 /LOG 每行为 '<yyyy-MM-dd HH:mm:ss.fff>   <message>'
    （本地 5 条样例已用同一正则验证通过）。若在特定 Inno 版本上正则解析到 0 条，
    脚本会打印警告并**整体退回进程级轮询**（仍是可用数据，只是分辨率降到 PollMs）。
    TB-DEPLOY 行完全由 installer.iss 自己 Log() 写出，格式见 installer.iss:515/598/629/642/643。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\diag\measure-inno-install.ps1 `
        -SetupPath "$env:USERPROFILE\Downloads\ThinkBuddySetup-dt1.6.14.10131621.exe" `
        -CleanTarget -OutJson "$env:TEMP\inno-install.json"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SetupPath,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\ThinkBuddy'),
    [string]$LogPath,
    [int]$TimeoutMinutes = 30,
    [int]$PollMs = 300,
    [switch]$Visible,
    [switch]$CleanTarget,
    [switch]$LeaveAppRunning,
    [string]$OutJson
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N1} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} B' -f $Bytes)
}

function Read-InnoLog {
    <# 解析 Inno Setup 日志：每行 '<yyyy-MM-dd HH:mm:ss.fff>   <message>'。 #>
    param([string]$Path)
    $entries = New-Object System.Collections.ArrayList
    if (-not (Test-Path -LiteralPath $Path)) { return $entries }
    foreach ($line in [System.IO.File]::ReadLines($Path, [System.Text.Encoding]::UTF8)) {
        $m = [regex]::Match($line, '^(?<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s{2,}(?<msg>.*)$')
        if ($m.Success) {
            [void]$entries.Add([pscustomobject]@{
                Time    = [datetime]::ParseExact($m.Groups['ts'].Value, 'yyyy-MM-dd HH:mm:ss.fff',
                            [System.Globalization.CultureInfo]::InvariantCulture)
                Message = $m.Groups['msg'].Value
            })
        }
    }
    return $entries
}

function Get-FirstMatch {
    param($Entries, [string]$Pattern)
    foreach ($e in $Entries) { if ($e.Message -match $Pattern) { return $e } }
    return $null
}

function Get-LastMatch {
    param($Entries, [string]$Pattern)
    $hit = $null
    foreach ($e in $Entries) { if ($e.Message -match $Pattern) { $hit = $e } }
    return $hit
}

# ---------------------------------------------------------------- 前置检查 ---- #
if (-not (Test-Path -LiteralPath $SetupPath)) { throw "找不到安装包: $SetupPath" }
$SetupPath = (Resolve-Path -LiteralPath $SetupPath).Path
$setupItem = Get-Item -LiteralPath $SetupPath

if (-not $LogPath) {
    $diagDir = Join-Path $env:TEMP 'tbdiag'
    if (-not (Test-Path -LiteralPath $diagDir)) { New-Item -ItemType Directory -Path $diagDir -Force | Out-Null }
    $LogPath = Join-Path $diagDir ('install-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
}
$logDir = Split-Path -Parent $LogPath
if ($logDir -and -not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

Write-Host '============================================================'
Write-Host ' ThinkBuddy 诊断 · 模块3 Inno 安装分阶段计时'
Write-Host (' 主机  : ' + $env:COMPUTERNAME + '   时间: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Host (' 安装包: ' + $SetupPath)
Write-Host (' 体积  : ' + (Format-Size $setupItem.Length) + '  (' + $setupItem.Length + ' bytes)')
Write-Host (' 目标  : ' + $InstallDir)
Write-Host (' 日志  : ' + $LogPath)
if ($Visible) { Write-Host ' 模式  : 交互安装（向导可见）' } else { Write-Host ' 模式  : 静默安装 /VERYSILENT' }
Write-Host '============================================================'

$defender = $null
try {
    $d = Get-MpComputerStatus -ErrorAction Stop
    $defender = [pscustomobject]@{
        realtime  = $d.RealTimeProtectionEnabled
        antivirus = $d.AntivirusEnabled
        mode      = [string]$d.AMRunningMode
    }
    Write-Host (' Defender 实时防护: ' + $defender.realtime + '   运行模式: ' + $defender.mode)
} catch {
    Write-Host ' Defender 状态读取失败（非管理员或被第三方杀软接管）。'
}
try {
    $excl = (Get-MpPreference -ErrorAction Stop).ExclusionPath
    Write-Host (' Defender 排除项数量: ' + @($excl).Count)
} catch {
    Write-Host ' Defender 排除项需管理员权限查看。'
}

if ($CleanTarget -and (Test-Path -LiteralPath $InstallDir)) {
    Write-Host (' [CleanTarget] 删除既有安装目录: ' + $InstallDir) -ForegroundColor Yellow
    Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
} elseif (Test-Path -LiteralPath $InstallDir) {
    Write-Host ' [注意] 目标目录已存在 —— 本次数值的是一条「覆盖升级」路径。' -ForegroundColor Yellow
    Write-Host '        需要全新安装基线请加 -CleanTarget。' -ForegroundColor Yellow
}
if (Test-Path -LiteralPath $LogPath) { Remove-Item -LiteralPath $LogPath -Force -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------- 启动安装 ---- #
$argList = @()
if (-not $Visible) {
    $argList += '/VERYSILENT'
    $argList += '/SUPPRESSMSGBOXES'
    $argList += '/NORESTART'
    $argList += '/SP-'
}
$argList += ('/LOG="' + $LogPath + '"')
$argList += ('/DIR="' + $InstallDir + '"')
# 不建桌面图标 / 不写开机自启：让两台机器的对照实验副作用一致。
$argList += '/MERGETASKS="!desktopicon,!autostart"'

Write-Host ''
Write-Host (' 启动: ' + $setupItem.Name + ' ' + ($argList -join ' ')) -ForegroundColor DarkGray

$archivePath = Join-Path $InstallDir 'runtime.tar.xz'
$stagingExe  = Join-Path $InstallDir 'runtime.new\python\python.exe'
$targetExe   = Join-Path $InstallDir 'runtime\python\python.exe'

$events = New-Object System.Collections.ArrayList
function Add-Event {
    param([string]$Name, [datetime]$At, [string]$Note = '')
    [void]$events.Add([pscustomobject]@{ Name = $Name; Time = $At; Note = $Note })
}

$mpFirst = $null; $mpLast = $null; $mpPeakWS = 0
$archiveFirstSeen = $null; $archiveLastChange = $null; $archiveLastSize = -1
$stagingSeen = $null; $targetSeen = $null

$proc = Start-Process -FilePath $SetupPath -ArgumentList $argList -PassThru
$startedAt = Get-Date
Add-Event -Name 'setup_start' -At $startedAt
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$deadline = $startedAt.AddMinutes($TimeoutMinutes)
$timedOut = $false

while (-not $proc.HasExited) {
    $now = Get-Date
    if ($now -gt $deadline) { $timedOut = $true; break }

    # Defender 引擎（MsMpEng）CPU / 内存占用采样
    $mp = Get-Process -Name 'MsMpEng' -ErrorAction SilentlyContinue
    if ($mp) {
        $cpu = $mp.TotalProcessorTime.TotalSeconds
        if ($null -eq $mpFirst) { $mpFirst = $cpu } else { $mpLast = $cpu }
        if ($mp.WorkingSet64 -gt $mpPeakWS) { $mpPeakWS = $mp.WorkingSet64 }
    }

    # runtime.tar.xz 落盘 + 体积稳定点
    if (Test-Path -LiteralPath $archivePath) {
        $item = Get-Item -LiteralPath $archivePath -ErrorAction SilentlyContinue
        $len = -1
        if ($item) { $len = $item.Length }
        if ($null -eq $archiveFirstSeen) {
            $archiveFirstSeen = Get-Date
            Add-Event -Name 'archive_first_seen' -At $archiveFirstSeen -Note (Format-Size $len)
        }
        if ($len -ne $archiveLastSize) { $archiveLastSize = $len; $archiveLastChange = Get-Date }
    }

    if ($null -eq $stagingSeen -and (Test-Path -LiteralPath $stagingExe)) {
        $stagingSeen = Get-Date
        Add-Event -Name 'runtime_new_ready' -At $stagingSeen
    }
    if ($null -eq $targetSeen -and (Test-Path -LiteralPath $targetExe)) {
        $targetSeen = Get-Date
        Add-Event -Name 'runtime_target_ready' -At $targetSeen
    }

    Start-Sleep -Milliseconds $PollMs
}

$proc.Refresh()
if ($timedOut) {
    Write-Host (' [超时] 超过 ' + $TimeoutMinutes + ' 分钟仍未结束，强制结束安装器进程。') -ForegroundColor Red
    try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
}
$sw.Stop()
$endedAt = Get-Date
Add-Event -Name 'setup_exit' -At $endedAt
$exitCode = $null
try { $exitCode = $proc.ExitCode } catch { }
$wallSeconds = [math]::Round($sw.Elapsed.TotalSeconds, 2)

Write-Host ''
Write-Host (' 安装器退出码: ' + $exitCode + '   墙钟总计: ' + $wallSeconds + ' s')
if ($exitCode -eq 8) {
    Write-Host ' 退出码 8 = installer.iss 自定义的「runtime 部署失败」（解压/校验/改名失败）。' -ForegroundColor Red
}


# ------------------------------------------------------------ 日志分阶段分析 ---- #
$entries = Read-InnoLog -Path $LogPath
$phases = New-Object System.Collections.ArrayList
function Add-Phase {
    param([string]$Name, $Seconds, [string]$Source)
    if ($null -eq $Seconds) { return }
    if ([double]$Seconds -lt 0) { return }
    [void]$phases.Add([pscustomobject]@{
        Phase  = $Name
        Seconds = [math]::Round([double]$Seconds, 2)
        Source  = $Source
    })
}

if ($entries.Count -eq 0) {
    Write-Host ''
    Write-Host (' [警告] 未解析到安装日志（' + $LogPath + '），回退使用进程级轮询数据。') -ForegroundColor Yellow
}

$logStart    = $null
if ($entries.Count -gt 0) { $logStart = $entries[0] }
$mProcess    = Get-FirstMatch -Entries $entries -Pattern '^Starting the installation process'
$firstDest   = Get-FirstMatch -Entries $entries -Pattern '^Dest filename:'
$lastDest    = Get-LastMatch  -Entries $entries -Pattern '^Dest filename:'
$archiveDest = Get-FirstMatch -Entries $entries -Pattern '^Dest filename: .*runtime\.tar\.xz'
$deployEnter = Get-FirstMatch -Entries $entries -Pattern '^TB-DEPLOY enter'
$deployDone  = Get-FirstMatch -Entries $entries -Pattern '^TB-DEPLOY done'
$succeeded   = Get-FirstMatch -Entries $entries -Pattern '^Installation process succeeded'
$uninstSave  = Get-FirstMatch -Entries $entries -Pattern '^Saving uninstall information'

$tb = @{}
foreach ($e in $entries) {
    $m = [regex]::Match($e.Message, '^TB-DEPLOY timing (?<k>extract|swap|warmup|total) (?<v>\d+)ms$')
    if ($m.Success) { $tb[$m.Groups['k'].Value] = [double]$m.Groups['v'].Value / 1000.0 }
}

if ($logStart -and $mProcess) {
    Add-Phase '1. 安装器启动/准备' (($mProcess.Time - $logStart.Time).TotalSeconds) 'install.log'
}
if ($mProcess -and $firstDest) {
    Add-Phase '2a. 准备 -> 首个文件落盘' (($firstDest.Time - $mProcess.Time).TotalSeconds) 'install.log'
}
$fileWriteEnd = $deployEnter
if (-not $fileWriteEnd) { $fileWriteEnd = $lastDest }
if ($firstDest -and $fileWriteEnd) {
    Add-Phase '2b. [Files] 文件写入' (($fileWriteEnd.Time - $firstDest.Time).TotalSeconds) 'install.log'
}
if ($archiveDest -and $fileWriteEnd -and ($fileWriteEnd.Time -ge $archiveDest.Time)) {
    Add-Phase '2c. runtime.tar.xz 写入' (($fileWriteEnd.Time - $archiveDest.Time).TotalSeconds) 'install.log'
}

if ($tb.ContainsKey('extract')) { Add-Phase '3a. tar 解压 runtime.tar.xz' $tb['extract'] 'TB-DEPLOY timing' }
if ($tb.ContainsKey('swap'))    { Add-Phase '3b. 删旧 runtime + 改名就位' $tb['swap'] 'TB-DEPLOY timing' }
# 2026-11 起预热改为异步派发（ewNoWait）：此数值是「派发耗时」≈0，
# 不再是「等待导入完成」的耗时；真实预热在安装结束后后台继续。
if ($tb.ContainsKey('warmup'))  { Add-Phase '3c. 预热派发（异步，不阻塞）' $tb['warmup'] 'TB-DEPLOY timing' }
if ($tb.ContainsKey('total')) {
    Add-Phase '3. 部署小计（解压+就位+预热）' $tb['total'] 'TB-DEPLOY timing'
} elseif ($deployEnter -and $deployDone) {
    Add-Phase '3. 部署小计（解压+就位+预热）' (($deployDone.Time - $deployEnter.Time).TotalSeconds) 'install.log'
}

$deployEndTime = $null
if ($deployDone) { $deployEndTime = $deployDone.Time }
elseif ($deployEnter -and $tb.ContainsKey('total')) { $deployEndTime = $deployEnter.Time.AddSeconds($tb['total']) }
if ($deployEndTime -and $succeeded) {
    Add-Phase '4. 收尾（[Run]/卸载记录/图标刷新）' (($succeeded.Time - $deployEndTime).TotalSeconds) 'install.log'
}
if ($uninstSave -and $succeeded) {
    Add-Phase '4b. 卸载信息保存 -> 安装成功' (($succeeded.Time - $uninstSave.Time).TotalSeconds) 'install.log'
}

Add-Phase '5. 墙钟总计（进程级 Stopwatch）' $wallSeconds 'Stopwatch'
if ($logStart -and $succeeded) {
    Add-Phase '5b. 日志覆盖区间（首行 -> 成功）' (($succeeded.Time - $logStart.Time).TotalSeconds) 'install.log'
}


# ------------------------------------------------------- 进程级轮询交叉验证 ---- #
Write-Host ''
Write-Host ('=== 进程级轮询事件（分辨率 ' + $PollMs + ' ms）===') -ForegroundColor Cyan
if ($events.Count -gt 0) {
    $t0 = $events[0].Time
    foreach ($ev in $events) {
        Write-Host ('  +{0,7:N2}s  {1,-20} {2}' -f (($ev.Time - $t0).TotalSeconds), $ev.Name, $ev.Note)
    }
} else {
    Write-Host '  (无事件)'
}
if ($archiveFirstSeen -and $archiveLastChange) {
    Write-Host ('  runtime.tar.xz 落盘 -> 体积稳定: ' +
        [math]::Round(($archiveLastChange - $archiveFirstSeen).TotalSeconds, 2) + ' s' +
        '（0 表示拷贝快于轮询间隔，以 install.log 为准）')
}
if ($stagingSeen -and $targetSeen) {
    Write-Host ('  runtime.new 就绪 -> runtime 就位: ' +
        [math]::Round(($targetSeen - $stagingSeen).TotalSeconds, 2) + ' s')
}

# ------------------------------------------------------------- 安装后目录统计 ---- #
Write-Host ''
Write-Host '=== 安装结果目录统计 ===' -ForegroundColor Cyan
$installFiles = 0
$installBytes = 0
if (Test-Path -LiteralPath $InstallDir) {
    $agg = Get-ChildItem -LiteralPath $InstallDir -Recurse -File -Force -ErrorAction SilentlyContinue |
           Measure-Object -Property Length -Sum
    $installFiles = $agg.Count
    $installBytes = [int64]$agg.Sum
    Write-Host ('  ' + $InstallDir)
    Write-Host ('  files=' + $installFiles + '   size=' + (Format-Size $installBytes))
} else {
    Write-Host '  [缺失] 安装目录不存在 —— 安装未成功。' -ForegroundColor Red
}

# --------------------------------------------------------------- 输出汇总表 ---- #
Write-Host ''
Write-Host '=== 分阶段耗时（降序）===' -ForegroundColor Cyan
$sorted = @($phases | Sort-Object -Property Seconds -Descending)
$sorted | Format-Table -AutoSize Phase, Seconds, Source | Out-String -Width 200 | Write-Host

Write-Host '=== 耗时最高 3 个阶段 ===' -ForegroundColor Yellow
$top3 = @($sorted | Select-Object -First 3)
$rank = 1
foreach ($p in $top3) {
    Write-Host ('  #' + $rank + '  ' + $p.Phase + ' : ' + $p.Seconds + ' s   [' + $p.Source + ']')
    $rank++
}
if ($sorted.Count -gt 0 -and $wallSeconds -gt 0) {
    Write-Host ('  最大单阶段占墙钟比例: ' + [math]::Round(100.0 * $sorted[0].Seconds / $wallSeconds, 1) + '%')
}

Write-Host ''
Write-Host '=== Defender 安装期占用 ===' -ForegroundColor Cyan
if ($null -ne $mpFirst) {
    $cpuDelta = 0
    if ($null -ne $mpLast) { $cpuDelta = $mpLast - $mpFirst }
    $pct = 0
    if ($wallSeconds -gt 0) { $pct = [math]::Round(100.0 * $cpuDelta / $wallSeconds, 1) }
    Write-Host ('  MsMpEng CPU 时间增量: ' + [math]::Round($cpuDelta, 2) + ' s  （占墙钟 ' + $pct + '% 单核）')
    Write-Host ('  MsMpEng 峰值工作集  : ' + (Format-Size $mpPeakWS))
} else {
    Write-Host '  未采集到 MsMpEng 进程（Defender 关闭或被第三方杀软接管）。'
}

if (-not $LeaveAppRunning) {
    Get-Process -Name 'ThinkBuddyDesktop' -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host (' 结束安装后拉起的应用进程: ' + $_.Id)
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
}

if ($OutJson) {
    $archiveSeenOffset = $null
    if ($archiveFirstSeen -and $events.Count -gt 0) {
        $archiveSeenOffset = [math]::Round(($archiveFirstSeen - $events[0].Time).TotalSeconds, 2)
    }
    $cpuSeconds = $null
    if (($null -ne $mpFirst) -and ($null -ne $mpLast)) { $cpuSeconds = [math]::Round($mpLast - $mpFirst, 2) }

    $payload = [ordered]@{
        generated_at            = (Get-Date -Format 'o')
        host                    = $env:COMPUTERNAME
        setup                   = $SetupPath
        setup_bytes             = $setupItem.Length
        install_dir             = $InstallDir
        log_path                = $LogPath
        mode                    = $(if ($Visible) { 'interactive' } else { 'silent' })
        scenario                = $(if ($CleanTarget) { 'fresh' } else { 'overwrite' })
        exit_code               = $exitCode
        timed_out               = $timedOut
        wall_seconds            = $wallSeconds
        archive_first_seen_s    = $archiveSeenOffset
        install_files           = $installFiles
        install_bytes           = $installBytes
        defender                = $defender
        defender_cpu_seconds    = $cpuSeconds
        tb_deploy               = $tb
        phases                  = @($sorted)
    }
    $payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutJson -Encoding UTF8
    Write-Host ''
    Write-Host ('JSON 已写出: ' + $OutJson) -ForegroundColor Green
}

