<#
.SYNOPSIS
    模块4 诊断脚本：runtime.tar.xz 解压拆分计时（xz 解压 vs tar 落盘），并生成对照实验素材。

.DESCRIPTION
    installer.iss 的 [Files] 对 runtime.tar.xz 使用 `Flags: nocompression`（归档本身已是
    xz 压缩体，Inno 只做纯拷贝），所以「双重压缩/双重解压」在当前设计里**不在 Inno 侧**：
      - Inno 侧：纯字节拷贝（本脚本用 Copy-Item 做等价基线）
      - 部署侧：{sys}\tar.exe -xf runtime.tar.xz = xz 解压 + tar 落盘两段
    本脚本把部署侧拆成三段分别计时，输出占比，并统计解压后文件数/体积/压缩比。

    可选 -PrepareUncompressedTar：在 WorkDir 里另存一份未压缩 runtime.tar
    （供「用未压缩归档构建对照安装包」实验使用，需在仓库副本上改 iss，脚本不动仓库）。

.PARAMETER ArchivePath
    runtime.tar.xz 路径，默认 <root>\desktop-shell\build\runtime.tar.xz

.PARAMETER WorkDir
    解压实测目录，默认 %TEMP%\tbdiag\tarxz

.PARAMETER PrepareUncompressedTar
    额外产出 runtime.tar（未压缩）到 WorkDir，供对照实验。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\diag\measure-tarxz-extract.ps1 -Keep
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$ArchivePath,
    [string]$WorkDir,
    [switch]$PrepareUncompressedTar,
    [switch]$Keep,
    [string]$OutJson
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# $PSScriptRoot 在部分 PS 5.1 场景的 param 默认值里为空，这里在函数体内解析。
if (-not $ProjectRoot) {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
}

if (-not $ArchivePath) { $ArchivePath = Join-Path $ProjectRoot 'desktop-shell\build\runtime.tar.xz' }
if (-not $WorkDir) { $WorkDir = Join-Path $env:TEMP 'tbdiag\tarxz' }

function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N1} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} B' -f $Bytes)
}

function Find-XzExe {
    $cmd = Get-Command xz.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($cand in @(
        "$env:ProgramFiles\Git\usr\bin\xz.exe",
        "$env:ProgramFiles\Git\mingw64\bin\xz.exe",
        "$env:ProgramFiles\Git\mingw64\usr\bin\xz.exe",
        "${env:ProgramFiles(x86)}\Git\usr\bin\xz.exe"
    )) { if (Test-Path -LiteralPath $cand) { return $cand } }
    return $null
}

if (-not (Test-Path -LiteralPath $ArchivePath)) {
    Write-Host ('[缺失] 找不到归档: ' + $ArchivePath) -ForegroundColor Red
    Write-Host '  需先执行（未传 -SkipInstaller 的）build\build.ps1，或从 CI artifact 取 runtime.tar.xz。'
    Write-Host '  【需人工采集】' -ForegroundColor DarkYellow
    exit 2
}

$archive = Get-Item -LiteralPath $ArchivePath
$tarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path -LiteralPath $tarExe)) {
    $tarCmd = Get-Command tar.exe -ErrorAction SilentlyContinue
    if ($tarCmd) { $tarExe = $tarCmd.Source } else { throw '未找到 tar.exe（需 Windows 10 1803+）' }
}
$xzExe = Find-XzExe

if (Test-Path -LiteralPath $WorkDir) { Remove-Item -LiteralPath $WorkDir -Recurse -Force }
New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null

Write-Host '============================================================'
Write-Host ' ThinkBuddy 诊断 · 模块4 runtime.tar.xz 解压拆分计时'
Write-Host (' 主机    : ' + $env:COMPUTERNAME + '   时间: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Host (' 归档    : ' + $archive.FullName + '  (' + (Format-Size $archive.Length) + ')')
Write-Host (' tar.exe : ' + $tarExe)
if ($xzExe) { Write-Host (' xz.exe  : ' + $xzExe) } else { Write-Host ' xz.exe  : 未找到（只能测 tar -xf 合并耗时）' }
Write-Host (' 工作目录: ' + $WorkDir)
Write-Host '============================================================'

# ---- 1) Inno 侧等价基线：nocompression 纯拷贝 ----
$copyTarget = Join-Path $WorkDir 'copy-of-runtime.tar.xz'
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Copy-Item -LiteralPath $archive.FullName -Destination $copyTarget -Force
$sw.Stop()
$copySeconds = [math]::Round($sw.Elapsed.TotalSeconds, 3)
$copyMBs = 0
if ($copySeconds -gt 0) { $copyMBs = [math]::Round(($archive.Length / 1MB) / $copySeconds, 1) }
Write-Host ''
Write-Host ('[1] Inno nocompression 纯拷贝基线: ' + $copySeconds + ' s  (' + $copyMBs + ' MB/s)')

# ---- 2) xz 解压单独计时（若 xz.exe 可用）----
$xzSeconds = $null
$tarFile = Join-Path $WorkDir 'runtime.tar'
if ($xzExe) {
    # 注意：PS 5.1 的 '>' 重定向会把二进制当文本编码写，必须让 xz 自己落盘
    # （-d -k 原地解压，生成同名去掉 .xz 的文件）。
    $xzInput = Join-Path $WorkDir 'runtime.tar.xz'
    Copy-Item -LiteralPath $archive.FullName -Destination $xzInput -Force
    $sw.Restart()
    & $xzExe -d -k -f $xzInput
    $sw.Stop()
    if ($LASTEXITCODE -ne 0) { throw ('xz 解压失败 rc=' + $LASTEXITCODE) }
    $xzSeconds = [math]::Round($sw.Elapsed.TotalSeconds, 3)
    $tarBytes = (Get-Item -LiteralPath $tarFile).Length
    Write-Host ('[2] xz 解压（仅解压，不落盘目录）: ' + $xzSeconds + ' s   -> 中间 tar = ' + (Format-Size $tarBytes))
} else {
    Write-Host '[2] 跳过 xz 单独计时（无 xz.exe）。说明：CI 上多线程 xz -T0 -3 不可用时，build.ps1 回退 bsdtar -cJf 单线程。'
}

# ---- 3) tar -xf 单趟：xz 解压 + 目录落盘 ----
$extractDir = Join-Path $WorkDir 'extract'
New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
$sw.Restart()
& $tarExe -xf $archive.FullName -C $extractDir
if ($LASTEXITCODE -ne 0) { throw ('tar 解压失败 rc=' + $LASTEXITCODE) }
$sw.Stop()
$totalSeconds = [math]::Round($sw.Elapsed.TotalSeconds, 3)

# ---- 4) 结果统计 ----
$agg = Get-ChildItem -LiteralPath $extractDir -Recurse -File -Force -ErrorAction SilentlyContinue |
       Measure-Object -Property Length -Sum
$fileCount = $agg.Count
$bytes = [int64]$agg.Sum

Write-Host ('[3] tar -xf 单趟（解压+落盘）: ' + $totalSeconds + ' s   files=' + $fileCount +
           '  size=' + (Format-Size $bytes))

$msPerFile = 0
if ($fileCount -gt 0) { $msPerFile = [math]::Round(1000.0 * $totalSeconds / $fileCount, 2) }
$ratio = 0
if ($archive.Length -gt 0) { $ratio = [math]::Round($bytes / $archive.Length, 2) }

$untarSeconds = $null
if ($null -ne $xzSeconds) {
    $untarSeconds = [math]::Round($totalSeconds - $xzSeconds, 3)
}

Write-Host ''
Write-Host '=== 阶段拆分（部署侧）===' -ForegroundColor Cyan
Write-Host ('  压缩比（解压后 / 归档） : ' + $ratio + ' : 1')
Write-Host ('  解压后文件数            : ' + $fileCount)
Write-Host ('  单文件平均耗时          : ' + $msPerFile + ' ms/file')
if ($null -ne $xzSeconds) {
    $xzPct = 0; $untarPct = 0
    if ($totalSeconds -gt 0) {
        $xzPct = [math]::Round(100.0 * $xzSeconds / $totalSeconds, 1)
        $untarPct = [math]::Round(100.0 * $untarSeconds / $totalSeconds, 1)
    }
    Write-Host ('  xz 解压（CPU 密集）     : ' + $xzSeconds + ' s  (' + $xzPct + '%)')
    Write-Host ('  tar 落盘（IO 密集）     : ' + $untarSeconds + ' s  (' + $untarPct + '%)')
    Write-Host '  说明：tar 落盘 = 总耗时 - xz 解压，含 NTFS 建目录/写文件/杀软扫描开销。'
} else {
    Write-Host '  未拆分 xz/落盘（缺 xz.exe）：可安装 Git for Windows 后重跑。'
}

Write-Host ''
Write-Host '=== 与 Inno 的关系（installer.iss [Files] 证据）===' -ForegroundColor Cyan
Write-Host '  Source: "..\build\runtime.tar.xz"; Flags: nocompression ignoreversion'
Write-Host '  -> Inno 不对该文件做 LZMA2 解压，只做纯拷贝，故 Inno 侧 LZMA2 解压耗时 ≈ 0。'
Write-Host '     安装期的「解压」全部发生在 [Code] ssPostInstall 的 tar.exe 里（本脚本 [3] 项）。'

if ($PrepareUncompressedTar) {
    $flatTar = Join-Path $WorkDir 'runtime.tar'
    if (-not (Test-Path -LiteralPath $flatTar)) {
        & $tarExe -cf $flatTar -C $extractDir .
        if ($LASTEXITCODE -ne 0) { throw ('tar -cf 失败 rc=' + $LASTEXITCODE) }
    }
    $flatSize = (Get-Item -LiteralPath $flatTar).Length
    Write-Host ''
    Write-Host '=== 对照实验素材（未压缩归档）===' -ForegroundColor Yellow
    Write-Host ('  ' + $flatTar + '  ' + (Format-Size $flatSize))
    Write-Host '  做法（不改本仓库）：复制一份仓库到临时目录 -> 把 installer.iss 的 [Files] 指向该 .tar'
    Write-Host '  并把 [Code] 里 tar.exe 的 -xf 改为 -xf（tar 自动识别未压缩）-> 用同一 ISCC 参数编译对照包。'
    Write-Host '  该实验会改变安装包结构与体积，属【需研发授权】的对照实验，脚本只准备素材。'
}

if ($OutJson) {
    $payload = [ordered]@{
        generated_at        = (Get-Date -Format 'o')
        host                = $env:COMPUTERNAME
        archive             = $archive.FullName
        archive_bytes       = $archive.Length
        copy_seconds        = $copySeconds
        copy_mb_per_s       = $copyMBs
        xz_seconds          = $xzSeconds
        untar_seconds       = $untarSeconds
        tar_xf_seconds      = $totalSeconds
        extracted_files     = $fileCount
        extracted_bytes     = $bytes
        ms_per_file         = $msPerFile
        compression_ratio   = $ratio
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $OutJson -Encoding UTF8
    Write-Host ''
    Write-Host ('JSON 已写出: ' + $OutJson) -ForegroundColor Green
}

if (-not $Keep) {
    Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host ('工作目录已清理（-Keep 可保留）: ' + $WorkDir) -ForegroundColor DarkGray
} else {
    Write-Host ('工作目录保留: ' + $WorkDir) -ForegroundColor DarkGray
}

