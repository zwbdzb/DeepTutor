<#
.SYNOPSIS
    模块2 诊断脚本：runtime 与安装目录量化快照（只读）。

.DESCRIPTION
    对三类对象分别统计：总大小、总文件数、体积 Top10、文件数 Top10
      1) desktop-shell\dist\ 下的产物（runtime.zip / ThinkBuddyDesktop.exe / ThinkBuddySetup-*.exe）
      2) runtime.tar.xz 与解压后的 staging 树（desktop-shell\runtime-build\staging）
      3) 本机已安装的 ThinkBuddy 目录（存在才统计）
    额外输出：staging 树总大小 / runtime.tar.xz 大小的压缩比。
    所有统计走 PowerShell 聚合（Measure-Object / Group-Object），只输出汇总与 TopN，
    不打印全量文件列表。

.PARAMETER ProjectRoot
    仓库根目录，默认由脚本位置推导（tools\diag\ 往上两级）。

.PARAMETER InstallDir
    已安装的 ThinkBuddy 目录。默认自动探测：
      %LOCALAPPDATA%\Programs\ThinkBuddy 、 %ProgramFiles%\ThinkBuddy

.PARAMETER OutJson
    可选：汇总结果写为 JSON，便于多轮对照。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\diag\measure-runtime-payload.ps1
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$StagingPath,
    [string]$DistPath,
    [string]$ArchivePath,
    [string]$InstallDir,
    [int]$TopN = 10,
    [string]$OutJson
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# $PSScriptRoot 在部分 PS 5.1 场景的 param 默认值里为空，这里在函数体内解析。
if (-not $ProjectRoot) {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
}
if (-not $StagingPath) { $StagingPath = Join-Path $ProjectRoot 'desktop-shell\runtime-build\staging' }
if (-not $DistPath)    { $DistPath    = Join-Path $ProjectRoot 'desktop-shell\dist' }
if (-not $ArchivePath) { $ArchivePath = Join-Path $ProjectRoot 'desktop-shell\build\runtime.tar.xz' }

function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N1} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} B' -f $Bytes)
}

function Get-TreeStats {
    <# 聚合统计一棵树：文件数 / 目录数 / 总字节 / 体积 TopN / 文件数 TopN。 #>
    param([string]$Path, [int]$TopN = 10)

    if (-not (Test-Path -LiteralPath $Path)) { return $null }

    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue)
    $dirs  = @(Get-ChildItem -LiteralPath $Path -Recurse -Directory -Force -ErrorAction SilentlyContinue)
    $sum   = ($files | Measure-Object -Property Length -Sum)

    $topFiles = @(
        $files | Sort-Object -Property Length -Descending | Select-Object -First $TopN | ForEach-Object {
            [pscustomobject]@{
                RelPath = $_.FullName.Substring($Path.Length).TrimStart('\')
                Size    = (Format-Size $_.Length)
                Bytes   = $_.Length
            }
        }
    )

    $topDirs = @(
        $files | Group-Object -Property DirectoryName | Sort-Object -Property Count -Descending |
            Select-Object -First $TopN | ForEach-Object {
                $bytes = 0
                $m = $_.Group | Measure-Object -Property Length -Sum
                if ($m.Sum) { $bytes = [int64]$m.Sum }
                [pscustomobject]@{
                    RelDir    = $_.Name.Replace($Path, '').TrimStart('\')
                    FileCount = $_.Count
                    Size      = (Format-Size $bytes)
                    Bytes     = $bytes
                }
            }
    )

    [pscustomobject]@{
        Path      = $Path
        FileCount = $files.Count
        DirCount  = $dirs.Count
        Bytes     = [int64]($sum.Sum)
        Size      = (Format-Size ([int64]($sum.Sum)))
        TopFiles  = $topFiles
        TopDirs   = $topDirs
    }
}

function Show-TreeStats {
    param($Stats)
    if ($null -eq $Stats) { return }
    Write-Host ''
    Write-Host ('=== ' + $Stats.Path) -ForegroundColor Cyan
    Write-Host ('  总大小   : ' + $Stats.Size + '  (' + $Stats.Bytes + ' bytes)')
    Write-Host ('  总文件数 : ' + $Stats.FileCount)
    Write-Host ('  总目录数 : ' + $Stats.DirCount)
    if ($Stats.FileCount -gt 0) {
        Write-Host ('  平均文件 : ' + (Format-Size ($Stats.Bytes / $Stats.FileCount)))
    }
    if ($Stats.TopFiles.Count -gt 0) {
        Write-Host ('  -- 体积 Top' + $TopN + ' --') -ForegroundColor Yellow
        $Stats.TopFiles | Format-Table -AutoSize RelPath, Size | Out-String -Width 200 | Write-Host
    }
    if ($Stats.TopDirs.Count -gt 0) {
        Write-Host ('  -- 文件数 Top' + $TopN + '（按目录聚合）--') -ForegroundColor Yellow
        $Stats.TopDirs | Format-Table -AutoSize RelDir, FileCount, Size | Out-String -Width 200 | Write-Host
    }
}

Write-Host '============================================================'
Write-Host ' ThinkBuddy Windows 打包诊断 · 模块2 payload 量化快照'
Write-Host (' 时间: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '   主机: ' + $env:COMPUTERNAME)
Write-Host '============================================================'

$result = [ordered]@{
    generated_at       = (Get-Date -Format 'o')
    host               = $env:COMPUTERNAME
    objects            = [ordered]@{}
    dist_files         = @()
    archive_bytes      = 0
    compression_ratio  = $null
}

# ---- 1) dist 产物一览 ----
Write-Host ''
Write-Host ('=== dist 目录: ' + $DistPath + ' ===') -ForegroundColor Cyan
if (Test-Path -LiteralPath $DistPath) {
    $distItems = @(Get-ChildItem -LiteralPath $DistPath -File -Force -ErrorAction SilentlyContinue |
                   Sort-Object -Property Length -Descending)
    if ($distItems.Count -eq 0) {
        Write-Host '  (目录存在但为空)'
    } else {
        $distItems | Select-Object Name,
            @{n = 'Size'; e = { Format-Size $_.Length }},
            @{n = 'Bytes'; e = { $_.Length }},
            LastWriteTime | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
        $result.dist_files = @($distItems | Select-Object Name,
            @{n = 'Bytes'; e = { $_.Length }},
            @{n = 'LastWriteTime'; e = { $_.LastWriteTime.ToString('o') }})
    }
} else {
    Write-Host '  [缺失] dist 目录不存在 —— 需先在构建机执行 build\build.ps1。'
    Write-Host '         【需人工采集】本项依赖构建产物。' -ForegroundColor DarkYellow
}

# ---- 2) runtime.tar.xz ----
Write-Host ''
Write-Host ('=== runtime 单归档: ' + $ArchivePath + ' ===') -ForegroundColor Cyan
$archiveBytes = 0
if (Test-Path -LiteralPath $ArchivePath) {
    $fi = Get-Item -LiteralPath $ArchivePath
    $archiveBytes = $fi.Length
    Write-Host ('  归档大小 : ' + (Format-Size $archiveBytes) + '  (' + $archiveBytes + ' bytes)')
    Write-Host ('  修改时间 : ' + $fi.LastWriteTime)
} else {
    Write-Host '  [缺失] runtime.tar.xz 不存在（build.ps1 的 [2c] 步骤只在未传 -SkipInstaller 时生成）。'
    Write-Host '         【需人工采集】' -ForegroundColor DarkYellow
}

# ---- 2b) staging 树 ----
$stagingStats = Get-TreeStats -Path $StagingPath -TopN $TopN
if ($null -eq $stagingStats) {
    Write-Host ''
    Write-Host ('[缺失] staging 树不存在: ' + $StagingPath) -ForegroundColor DarkYellow
    Write-Host '       需在具备 cp312 构建解释器（BUILD_PY）的机器上执行:' -ForegroundColor DarkYellow
    Write-Host '       desktop-shell\.venv\Scripts\python.exe tools\build_runtime.py --no-zip' -ForegroundColor DarkYellow
    Write-Host '       【需人工采集】' -ForegroundColor DarkYellow
}
Show-TreeStats -Stats $stagingStats

# ---- 2c) 压缩比 ----
$ratio = $null
if ($stagingStats -and $archiveBytes -gt 0) {
    $ratio = [math]::Round(($stagingStats.Bytes / $archiveBytes), 2)
    Write-Host ''
    Write-Host '=== 压缩比（staging 树 / runtime.tar.xz） ===' -ForegroundColor Cyan
    Write-Host ('  staging 解压后 : ' + $stagingStats.Size)
    Write-Host ('  runtime.tar.xz : ' + (Format-Size $archiveBytes))
    Write-Host ('  压缩比          : ' + $ratio + ' : 1  （归档 = 解压后体积的 ' +
        [math]::Round(100.0 * $archiveBytes / $stagingStats.Bytes, 1) + '%）')
}


# ---- 3) 已安装目录 ----
$installCandidates = @()
if ($InstallDir) {
    $installCandidates = @($InstallDir)
} else {
    $installCandidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\ThinkBuddy'),
        (Join-Path $env:ProgramFiles 'ThinkBuddy'),
        (Join-Path ${env:ProgramFiles(x86)} 'ThinkBuddy')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
}

Write-Host ''
Write-Host '=== 本机已安装的 ThinkBuddy 目录 ===' -ForegroundColor Cyan
if (@($installCandidates).Count -eq 0) {
    Write-Host '  [无] 本机未发现 ThinkBuddy 安装目录（开发机常见）。' -ForegroundColor DarkYellow
    Write-Host '       【需人工采集】在干净测试机安装一次 ThinkBuddySetup-*.exe 后重跑本脚本。' -ForegroundColor DarkYellow
} else {
    foreach ($cand in $installCandidates) {
        $st = Get-TreeStats -Path $cand -TopN $TopN
        Show-TreeStats -Stats $st
        if ($st) {
            $result.objects[$cand] = [ordered]@{
                file_count = $st.FileCount; dir_count = $st.DirCount; bytes = $st.Bytes
            }
        }
    }
    foreach ($cand in $installCandidates) {
        $rt = Join-Path $cand 'runtime'
        if (Test-Path -LiteralPath $rt) {
            $st = Get-TreeStats -Path $rt -TopN $TopN
            Show-TreeStats -Stats $st
            if ($st) {
                $result.objects[$rt] = [ordered]@{
                    file_count = $st.FileCount; dir_count = $st.DirCount; bytes = $st.Bytes
                }
            }
        }
    }
}

if ($stagingStats) {
    $result.objects[$StagingPath] = [ordered]@{
        file_count = $stagingStats.FileCount; dir_count = $stagingStats.DirCount; bytes = $stagingStats.Bytes
    }
}
$result.archive_bytes = $archiveBytes
$result.compression_ratio = $ratio

Write-Host ''
Write-Host '提示：统计已按约束聚合输出（汇总 + TopN），未打印全量文件列表。' -ForegroundColor DarkGray

if ($OutJson) {
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutJson -Encoding UTF8
    Write-Host ('JSON 已写出: ' + $OutJson) -ForegroundColor Green
}

