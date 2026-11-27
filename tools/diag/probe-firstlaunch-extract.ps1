<#
.SYNOPSIS
    模块6 诊断脚本：验证 Windows 端首次启动是否把内嵌 runtime.zip 自解压到 %LOCALAPPDATA%。

.DESCRIPTION
    macOS 侧没有安装器，.app 内嵌 runtime.zip，首启由 desktop/runtime.py 的
    extract_bundled_runtime() 解压到 ~/Library/Application Support/ThinkBuddy/runtime。
    该函数**没有平台分支**，Windows 走同一段代码。本脚本在真实安装实例上验证：
      1) 静态判定：exe 旁 / exe 内(_MEI) / 托管目录 三处是否存在 runtime.zip 候选；
      2) 动态实测：启动 exe 后轮询 %TEMP%\_MEI*、%LOCALAPPDATA%\ThinkBuddy\runtime
         的生成过程，量出首启自解压窗口（秒）、写入字节数、文件数、吞吐；
      3) 读 %LOCALAPPDATA%\ThinkBuddy\logs\app.log 中的自解压/托管运行时日志行。

    ⚠️ 首启自解压耗时 ≠ 安装耗时：本脚本只测「装完之后第一次双击 exe 到运行时就位」，
       安装阶段（Inno）耗时请用 measure-inno-install.ps1，两者不要混在一起汇报。

.PARAMETER ExePath
    已安装的 ThinkBuddyDesktop.exe 路径（必填）。

.PARAMETER Root
    托管根目录，默认 %LOCALAPPDATA%\ThinkBuddy（对应 runtime.py 的 ROOT）。

.PARAMETER ClearManagedRuntime
    测试前删除 <Root>\runtime（模拟「安装器 [InstallDelete] 刚清过缓存」的真实首启）。
    ★该参数会删除托管缓存目录，只影响派生运行时，不影响 %USERPROFILE%\ThinkBuddy 学习数据。

.PARAMETER StopAfter
    采集结束后结束被启动的 ThinkBuddyDesktop 进程（默认结束）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\diag\probe-firstlaunch-extract.ps1 `
        -ExePath "$env:LOCALAPPDATA\Programs\ThinkBuddy\ThinkBuddyDesktop.exe" `
        -ClearManagedRuntime -OutJson "$env:TEMP\tbdiag\firstlaunch.json"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ExePath,
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'ThinkBuddy'),
    [int]$TimeoutSeconds = 900,
    [int]$PollMs = 400,
    [switch]$ClearManagedRuntime,
    [switch]$KeepRunning,
    [string]$OutJson
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

if (-not (Test-Path -LiteralPath $ExePath)) { throw ('找不到已安装的 exe: ' + $ExePath) }
$ExePath = (Resolve-Path -LiteralPath $ExePath).Path
$exeDir = Split-Path -Parent $ExePath
$managedRuntime = Join-Path $Root 'runtime'
$appLog = Join-Path $Root 'logs\app.log'

Write-Host '============================================================'
Write-Host ' 模块6 · Windows 首启 runtime 自解压验证（与安装耗时严格分开）'
Write-Host (' 主机: ' + $env:COMPUTERNAME + '   时间: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Host (' exe : ' + $ExePath)
Write-Host (' 托管: ' + $Root)
Write-Host '============================================================'

# ---------------------------------------------------------------- 静态判定 ---- #
Write-Host ''
Write-Host '=== 静态判定：runtime 候选位置（runtime.py::_runtime_zip_candidates / _runtime_tree_candidates）===' -ForegroundColor Cyan
$exeZip   = Join-Path $exeDir 'runtime.zip'
$exeTree  = Join-Path $exeDir 'runtime'
$rootZip  = Join-Path $Root 'runtime.zip'
$checks = @(
    @{ Name = 'exe 旁 runtime.zip (EXE_DIR)';      Path = $exeZip;  Kind = 'zip' },
    @{ Name = 'exe 旁 runtime/ 树 (EXE_DIR)';      Path = $exeTree; Kind = 'tree' },
    @{ Name = '托管缓存 runtime/ (ROOT)';          Path = $managedRuntime; Kind = 'tree' },
    @{ Name = '托管缓存 runtime.zip (ROOT)';       Path = $rootZip; Kind = 'zip' }
)
foreach ($c in $checks) {
    if (Test-Path -LiteralPath $c.Path) {
        if ($c.Kind -eq 'zip') {
            $sz = (Get-Item -LiteralPath $c.Path).Length
            Write-Host ('  [有] ' + $c.Name + '  -> ' + (Format-Size $sz)) -ForegroundColor Green
        } else {
            Write-Host ('  [有] ' + $c.Name) -ForegroundColor Green
        }
    } else {
        Write-Host ('  [无] ' + $c.Name) -ForegroundColor DarkGray
    }
}
Write-Host ''
Write-Host '  说明：extract_bundled_runtime() 的候选还包括 PyInstaller 解包目录 APP_DIR'
Write-Host '        (= %TEMP%\_MEIxxxxxx)，onefile 内嵌了 runtime.zip 时该处才有 zip；'
Write-Host '        本脚本在启动后按 _MEI 目录实测。'

$managedBefore = $false
if (Test-Path -LiteralPath (Join-Path $managedRuntime 'python\python.exe')) { $managedBefore = $true }
Write-Host ''
Write-Host ('  托管 runtime 测试前状态: ' + $(if ($managedBefore) { '已存在（本次不会触发解压，除非 manifest 不一致）' } else { '不存在（会触发一次完整自解压）' }))

if ($ClearManagedRuntime) {
    Write-Host (' [ClearManagedRuntime] 删除托管缓存: ' + $managedRuntime) -ForegroundColor Yellow
    Remove-Item -LiteralPath $managedRuntime -Recurse -Force -ErrorAction SilentlyContinue
    $managedBefore = $false
}

# 记录 app.log 现有长度，便于只取本次启动的新增行
$logOffset = 0
if (Test-Path -LiteralPath $appLog) { $logOffset = (Get-Item -LiteralPath $appLog).Length }

# ---------------------------------------------------------------- 动态实测 ---- #
$meiBefore = @(Get-ChildItem -LiteralPath $env:TEMP -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue |
               ForEach-Object { $_.FullName })

Get-Process -Name 'ThinkBuddyDesktop' -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host (' [预检] 先结束已在运行的实例 pid=' + $_.Id) -ForegroundColor Yellow
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 800

Write-Host ''
Write-Host '=== 启动 exe，开始轮询 ===' -ForegroundColor Cyan
$launched = Get-Date
$proc = Start-Process -FilePath $ExePath -PassThru

$meiSeen = $null
$extractStart = $null
$extractDone = $null
$manifestPath = Join-Path $managedRuntime 'runtime-manifest.json'
$managedPy = Join-Path $managedRuntime 'python\python.exe'
$pollStart = Get-Date

while (((Get-Date) - $pollStart).TotalSeconds -lt $TimeoutSeconds) {
    if ($null -eq $meiSeen) {
        $meiNow = @(Get-ChildItem -LiteralPath $env:TEMP -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue |
                    ForEach-Object { $_.FullName })
        $newMei = @($meiNow | Where-Object { $meiBefore -notcontains $_ })
        if ($newMei.Count -gt 0) {
            $meiSeen = Get-Date
            Write-Host ('  +' + [math]::Round(($meiSeen - $launched).TotalSeconds, 1) +
                        's  PyInstaller 解包目录出现: ' + $newMei[0])
            $meiZip = Join-Path $newMei[0] 'runtime.zip'
            if (Test-Path -LiteralPath $meiZip) {
                Write-Host ('         ^ 目录内存在 runtime.zip (' +
                            (Format-Size (Get-Item -LiteralPath $meiZip).Length) + ') -> exe 内嵌了运行时') -ForegroundColor Yellow
            } else {
                Write-Host '         ^ 目录内无 runtime.zip -> exe 是纯壳（未内嵌运行时）' -ForegroundColor Green
            }
        }
    }

    if ($null -eq $extractStart -and (Test-Path -LiteralPath $manifestPath)) {
        $extractStart = Get-Date
        Write-Host ('  +' + [math]::Round(($extractStart - $launched).TotalSeconds, 1) +
                    's  托管 runtime 解压开始（runtime-manifest.json 出现）')
    }
    if ($null -eq $extractDone -and (Test-Path -LiteralPath $managedPy)) {
        $extractDone = Get-Date
        Write-Host ('  +' + [math]::Round(($extractDone - $launched).TotalSeconds, 1) +
                    's  托管 runtime 解包完成（python\python.exe 出现）') -ForegroundColor Green
    }
    if ($null -ne $extractDone) { break }

    if ($proc.HasExited -and $null -eq $extractDone) {
        Write-Host '  [注意] 壳进程已退出但托管 runtime 仍未就绪。' -ForegroundColor Yellow
        break
    }
    Start-Sleep -Milliseconds $PollMs
}

# ---------------------------------------------------------------- 结果统计 ---- #
$managedFiles = 0
$managedBytes = 0
if (Test-Path -LiteralPath $managedRuntime) {
    $agg = Get-ChildItem -LiteralPath $managedRuntime -Recurse -File -Force -ErrorAction SilentlyContinue |
           Measure-Object -Property Length -Sum
    $managedFiles = $agg.Count
    $managedBytes = [int64]$agg.Sum
}

Write-Host ''
Write-Host '=== 首启行为观测结果 ===' -ForegroundColor Cyan
if ($null -ne $extractDone) {
    $extractSeconds = [math]::Round(($extractDone - $launched).TotalSeconds, 2)
    $mbps = 0
    if ($extractSeconds -gt 0) { $mbps = [math]::Round(($managedBytes / 1MB) / $extractSeconds, 1) }
    Write-Host ('  首启 -> 托管 runtime 就位 : ' + $extractSeconds + ' s') -ForegroundColor Yellow
    Write-Host ('  写入文件数 / 体积         : ' + $managedFiles + ' / ' + (Format-Size $managedBytes))
    Write-Host ('  平均吞吐                  : ' + $mbps + ' MB/s')
} else {
    Write-Host '  未观测到托管 runtime 解压完成。'
    Write-Host '  可能原因：exe 未内嵌 runtime.zip（纯壳）-> 不触发自解压；'
    Write-Host '            或托管缓存已存在且 manifest 一致 -> 直接复用。'
}
if ($null -ne $meiSeen) {
    Write-Host ('  PyInstaller _MEI 解包       : +' + [math]::Round(($meiSeen - $launched).TotalSeconds, 2) +
                ' s（每次启动都会发生，与安装无关）')
}

Write-Host ''
Write-Host '=== app.log 新增行（自解压 / 运行时选择相关）===' -ForegroundColor Cyan
$markers = @()
if (Test-Path -LiteralPath $appLog) {
    try {
        $tail = ''
        $fs = [System.IO.File]::Open($appLog, 'Open', 'Read', 'ReadWrite')
        try {
            if ($logOffset -lt $fs.Length) { $fs.Position = $logOffset }
            $reader = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
            $tail = $reader.ReadToEnd()
        } finally { $fs.Dispose() }
        $markers = @($tail -split "`r?`n" | Where-Object {
            $_ -match 'provisioning managed runtime|runtime extracted to|managed runtime differs|using embedded runtime|using managed node|runtime extraction failed'
        })
    } catch { }
}
if ($markers.Count -gt 0) {
    foreach ($line in $markers) { Write-Host ('  ' + $line) }
} else {
    Write-Host '  (无相关新增日志；debug 日志级别可能未开启)'
}

if (-not $KeepRunning) {
    Get-Process -Name 'ThinkBuddyDesktop' -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host (' 结束测试启动的进程 pid=' + $_.Id)
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
}

if ($OutJson) {
    [ordered]@{
        generated_at     = (Get-Date -Format 'o')
        host             = $env:COMPUTERNAME
        exe              = $ExePath
        exe_dir_zip      = (Test-Path -LiteralPath $exeZip)
        exe_dir_tree     = (Test-Path -LiteralPath $exeTree)
        managed_before   = $managedBefore
        mei_seen_seconds = $(if ($meiSeen) { [math]::Round(($meiSeen - $launched).TotalSeconds, 2) } else { $null })
        extract_start_s  = $(if ($extractStart) { [math]::Round(($extractStart - $launched).TotalSeconds, 2) } else { $null })
        extract_done_s   = $(if ($extractDone) { [math]::Round(($extractDone - $launched).TotalSeconds, 2) } else { $null })
        managed_files    = $managedFiles
        managed_bytes    = $managedBytes
        log_markers      = @($markers)
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $OutJson -Encoding UTF8
    Write-Host ''
    Write-Host ('JSON 已写出: ' + $OutJson) -ForegroundColor Green
}


