<#
.SYNOPSIS
    重点假设1 验证脚本：runtime.zip 内嵌 onefile exe + Inno 携带 runtime.tar.xz 是否重复投递 payload。

.DESCRIPTION
    只读检查产物，不构建、不改配置：
      1) 列出 dist 下 ThinkBuddyDesktop.exe / runtime.zip / ThinkBuddySetup-*.exe 与
         build\runtime.tar.xz 的实际体积。
      2) 在 onefile exe 里按字节扫描 ASCII 名 "runtime.zip"（PyInstaller CArchive 的
         TOC 里带条目名）与 "PYZ-00.pyz"（确认是 PyInstaller 单文件包），输出命中偏移。
      3) 判定是否有重复 payload，并量化重复字节：
         - exe 内嵌 runtime.zip（-MakeZip 路径）-> 运行时被投递两次：
           一次在 exe 内，一次是 Inno [Files] 的 runtime.tar.xz。
         - exe 未内嵌（CI 当前未传 -MakeZip）-> 无重复 payload。
      4) 若提供 SetupPath，量化安装包相对「exe + tar.xz」的额外开销。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\diag\verify-runtime-payload-duplication.ps1 `
        -SetupPath "$env:USERPROFILE\Downloads\ThinkBuddySetup-dt1.6.14.10131621.exe"
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$ExePath,
    [string]$ZipPath,
    [string]$ArchivePath,
    [string]$SetupPath,
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

if (-not $ExePath)     { $ExePath     = Join-Path $ProjectRoot 'desktop-shell\dist\ThinkBuddyDesktop.exe' }
if (-not $ZipPath)     { $ZipPath     = Join-Path $ProjectRoot 'desktop-shell\dist\runtime.zip' }
if (-not $ArchivePath) { $ArchivePath = Join-Path $ProjectRoot 'desktop-shell\build\runtime.tar.xz' }

function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N1} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} B' -f $Bytes)
}

function Find-AsciiInFile {
    <# 分块扫描文件里的 ASCII 子串，返回全部命中偏移（最多 $Max）。 #>
    param([string]$Path, [string]$Needle, [int64]$ChunkSize = 4MB, [int]$Max = 8)
    $hits = New-Object System.Collections.ArrayList
    if (-not (Test-Path -LiteralPath $Path)) { return $hits }
    $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
    try {
        $buf = New-Object byte[] $ChunkSize
        $overlap = $Needle.Length - 1
        $carry = New-Object byte[] 0
        [int64]$base = 0
        while (($read = $fs.Read($buf, 0, $buf.Length)) -gt 0) {
            $segment = New-Object byte[] ($carry.Length + $read)
            [Array]::Copy($carry, 0, $segment, 0, $carry.Length)
            [Array]::Copy($buf, 0, $segment, $carry.Length, $read)
            $text = [System.Text.Encoding]::ASCII.GetString($segment)
            $idx = 0
            while ($true) {
                $idx = $text.IndexOf($Needle, $idx, [System.StringComparison]::Ordinal)
                if ($idx -lt 0) { break }
                [void]$hits.Add($base + $idx)
                if ($hits.Count -ge $Max) { return $hits }
                $idx++
            }
            $keep = [Math]::Min($overlap, $segment.Length)
            $carry = New-Object byte[] $keep
            [Array]::Copy($segment, $segment.Length - $keep, $carry, 0, $keep)
            $base += $segment.Length - $keep
        }
    } finally { $fs.Dispose() }
    return $hits
}

Write-Host '============================================================'
Write-Host ' 重点假设1 · 重复 payload 验证'
Write-Host (' 主机: ' + $env:COMPUTERNAME + '   时间: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Host '============================================================'

$artifacts = [ordered]@{
    'onefile exe'     = $ExePath
    'runtime.zip'     = $ZipPath
    'runtime.tar.xz'  = $ArchivePath
    'setup exe'       = $SetupPath
}
$sizes = @{}
Write-Host ''
Write-Host '=== 产物体积 ===' -ForegroundColor Cyan
foreach ($k in $artifacts.Keys) {
    $p = $artifacts[$k]
    if ($p -and (Test-Path -LiteralPath $p)) {
        $len = (Get-Item -LiteralPath $p).Length
        $sizes[$k] = $len
        Write-Host ('  {0,-14} {1,12}  {2}' -f $k, (Format-Size $len), $p)
    } else {
        Write-Host ('  {0,-14} {1,12}  [缺失] {2}' -f $k, '-', $p) -ForegroundColor DarkYellow
    }
}

# ---- exe 内嵌探测 ----
Write-Host ''
Write-Host '=== onefile exe 内嵌探测（字节级 ASCII 扫描）===' -ForegroundColor Cyan
$embeddedZip = $false
$exeExists = $false
$zipHits = @()
$pyzHits = @()
if (Test-Path -LiteralPath $ExePath) {
    $exeExists = $true
    $zipHits = @(Find-AsciiInFile -Path $ExePath -Needle 'runtime.zip')
    $pyzHits = @(Find-AsciiInFile -Path $ExePath -Needle 'PYZ-00.pyz')
    Write-Host ('  PyInstaller 标志 "PYZ-00.pyz" 命中: ' + $pyzHits.Count)
    if ($zipHits.Count -gt 0) {
        $embeddedZip = $true
        Write-Host ('  "runtime.zip" 命中 ' + $zipHits.Count + ' 处，前几个偏移: ' +
            (($zipHits | ForEach-Object { $_ }) -join ', ')) -ForegroundColor Yellow
    } else {
        Write-Host '  "runtime.zip" 未命中 -> 该 exe 是「纯壳」单文件包（未内嵌运行时）。' -ForegroundColor Green
    }
} else {
    Write-Host '  [缺失] exe 不存在，无法探测。' -ForegroundColor DarkYellow
}

# ---- 重复 payload 量化 ----
Write-Host ''
Write-Host '=== 重复 payload 量化 ===' -ForegroundColor Cyan
$dupBytes = 0
if (-not $exeExists) {
    Write-Host '  判定: 无法判定 —— onefile exe 缺失。' -ForegroundColor DarkYellow
    Write-Host '  请在产物所在机器运行本脚本（或指定 -ExePath）。' -ForegroundColor DarkYellow
} elseif ($embeddedZip) {
    if ($sizes.ContainsKey('runtime.zip')) {
        $dupBytes = $sizes['runtime.zip']
    } elseif ($sizes.ContainsKey('onefile exe') -and $sizes.ContainsKey('runtime.tar.xz')) {
        # zip 不在磁盘上时，用「exe 体积 - 纯壳基线(约14MB)」粗估内嵌体积
        $dupBytes = [Math]::Max(0, $sizes['onefile exe'] - 14MB)
        Write-Host '  runtime.zip 不在 dist（PyInstaller 已消费），内嵌体积按 exe-14MB 粗估。' -ForegroundColor DarkYellow
    }
    Write-Host ('  判定: 存在重复投递 —— 同一份 runtime 同时出现在 exe 内嵌 zip 与 Inno 的 tar.xz。') -ForegroundColor Red
    Write-Host ('  重复字节数（压缩形态，取 zip 侧）: ' + (Format-Size $dupBytes) + '  (' + $dupBytes + ' bytes)')
    if ($sizes.ContainsKey('setup exe') -and $sizes['setup exe'] -gt 0) {
        Write-Host ('  占安装包体积: ' + [math]::Round(100.0 * $dupBytes / $sizes['setup exe'], 1) + '%')
    }
    Write-Host '  实际下载/落盘重复量还应叠加 PyInstaller 每次启动把内嵌 zip 解到 %TEMP%\_MEI 的那一份。'
} else {
    Write-Host '  判定: 未发现重复投递（exe 未内嵌 runtime.zip）。' -ForegroundColor Green
    Write-Host '  运行时只经 Inno 的 runtime.tar.xz 单路投递。'
}

if ($sizes.ContainsKey('setup exe') -and $sizes.ContainsKey('onefile exe') -and $sizes.ContainsKey('runtime.tar.xz')) {
    $overhead = $sizes['setup exe'] - $sizes['onefile exe'] - $sizes['runtime.tar.xz']
    Write-Host ''
    Write-Host '=== 安装包结构核对 ===' -ForegroundColor Cyan
    Write-Host ('  setup = ' + (Format-Size $sizes['setup exe']))
    Write-Host ('  exe + tar.xz = ' + (Format-Size ($sizes['onefile exe'] + $sizes['runtime.tar.xz'])))
    Write-Host ('  差额（安装器自身 + 图标 + 未压缩存储的对齐开销）= ' + (Format-Size $overhead))
    if ([math]::Abs($overhead) -gt 30MB) {
        Write-Host '  差额偏大：安装包内可能还有别的 payload（例如旧版逐文件 staging），需人工核对。' -ForegroundColor Yellow
    }
}

Write-Host ''
if ($OutJson) {
    $payload = [ordered]@{
        generated_at       = (Get-Date -Format 'o')
        host               = $env:COMPUTERNAME
        exe_path           = $ExePath
        exe_bytes          = $(if ($sizes.ContainsKey('onefile exe')) { $sizes['onefile exe'] } else { $null })
        zip_path           = $ZipPath
        zip_bytes          = $(if ($sizes.ContainsKey('runtime.zip')) { $sizes['runtime.zip'] } else { $null })
        archive_bytes      = $(if ($sizes.ContainsKey('runtime.tar.xz')) { $sizes['runtime.tar.xz'] } else { $null })
        setup_bytes        = $(if ($sizes.ContainsKey('setup exe')) { $sizes['setup exe'] } else { $null })
        embedded_zip       = $embeddedZip
        zip_name_hits      = @($zipHits)
        pyz_name_hits      = @($pyzHits)
        duplicate_bytes    = $dupBytes
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $OutJson -Encoding UTF8
    Write-Host ('JSON 已写出: ' + $OutJson) -ForegroundColor Green
}

