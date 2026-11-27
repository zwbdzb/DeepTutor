#Requires -Version 5.1
<#
.SYNOPSIS
    对比 runtime 归档格式：.tar.xz（现状）vs .tar.zst（候选）的体积与「解压落盘」耗时。

.DESCRIPTION
    批次3（zstd 替换 xz）的决策门禁需要两个实测数字：
      1) setup 体积增幅是否 < 3%；
      2) 安装期「解压 + 逐文件落盘」是否真的更快。
    本脚本在一棵**代表性子集**上把这两件事量出来：
      - 用 bsdtar 打 base.tar（不压缩）；
      - 用 Python 的 lzma(preset=3) 生成 .tar.xz —— 与 build.ps1 `xz -T0 -3` 同档位
        （编码线程数不同，只影响编码耗时，不影响压缩比与解码路径）；
      - 用 Python 3.14 `compression.zstd` 生成 .tar.zst（level 可配，默认 3/19 两档）；
      - 解码统一走 `tar.exe -xf`（bsdtar），也就是 installer.iss 实际使用的路径；
        每档跑 N 次取中位数。

    只读：源目录只被读取；所有产物写进 -WorkRoot（默认 %TEMP%）。
    不修改任何打包配置，也不产出安装包。

.PARAMETER SourceDir
    要取样的一棵目录（例如 .venv\Lib\site-packages）。脚本只读取它。

.PARAMETER MaxFiles
    子集上限（默认 8000）。子集按「原目录顺序」截取，够代表小文件密度即可。

.PARAMETER WorkRoot
    工作目录，默认 %TEMP%\tb-arcfmt。

.PARAMETER PythonExe
    需要 3.14+（compression.zstd，PEP 784）。缺失则该格式跳过并标注。

.PARAMETER Levels
    zstd 档位，默认 @(3,19)。

.PARAMETER Runs
    每档解码计时次数，默认 3（取中位数）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\diag\compare-runtime-archive-formats.ps1 `
      -SourceDir D:\pywork\DeepTutor\.venv\Lib\site-packages -MaxFiles 8000
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [int]$MaxFiles = 8000,
    [string]$WorkRoot = (Join-Path $env:TEMP 'tb-arcfmt'),
    [string]$PythonExe = '',
    # 用 string 再手动切分：命令行传 "3,19" 时 [int[]] 绑定会把它当成 319
    [string]$Levels = '3,19',
    [int]$Runs = 3
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-Step($m) { Write-Host "`n=== $m" -ForegroundColor Cyan }
function Now-Ms { [DateTime]::UtcNow.Ticks / 10000.0 }

# --- 定位依赖 -------------------------------------------------------------
$tarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path -LiteralPath $tarExe)) { throw "bsdtar not found: $tarExe" }

if (-not $PythonExe) {
    foreach ($cand in @(
            'D:\pywork\DeepTutor\.venv\Scripts\python.exe',
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'))) {
        if (Test-Path -LiteralPath $cand) { $PythonExe = $cand; break }
    }
}
if (-not $PythonExe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $PythonExe = $cmd.Source }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    throw "python not found; pass -PythonExe (need 3.14+ for compression.zstd)"
}

$pyVer = (& $PythonExe -c "import sys;print('%d.%d.%d'%sys.version_info[:3])").Trim()
$hasZstd = $false
& $PythonExe -c "from compression import zstd" 2>$null
if ($LASTEXITCODE -eq 0) { $hasZstd = $true }

Write-Step 'environment'
Write-Host " tar      : $tarExe"
& $tarExe --version | Select-Object -First 1 | ForEach-Object { Write-Host "            $_" }
Write-Host " python   : $PythonExe ($pyVer)  compression.zstd=$hasZstd"
Write-Host " source   : $SourceDir"
Write-Host " workroot : $WorkRoot"

# --- 取样：拷一份受控子集 --------------------------------------------------
Write-Step "stage subset (max $MaxFiles files)"
$subset = Join-Path $WorkRoot 'subset'
if (Test-Path -LiteralPath $WorkRoot) { Remove-Item -LiteralPath $WorkRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $subset | Out-Null

$copied = 0
$bytes = 0
foreach ($f in Get-ChildItem -LiteralPath $SourceDir -Recurse -File -Force -ErrorAction SilentlyContinue) {
    if ($copied -ge $MaxFiles) { break }
    $rel = $f.FullName.Substring($SourceDir.Length).TrimStart('\')
    $dst = Join-Path $subset $rel
    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
    try {
        Copy-Item -LiteralPath $f.FullName -Destination $dst -Force
        $copied++
        $bytes += $f.Length
    } catch { }
}
Write-Host (" files={0:N0}  size={1:N1} MB" -f $copied, ($bytes / 1MB))
if ($copied -eq 0) { throw "no files copied from $SourceDir" }

# --- 打未压缩 tar 作基准 ---------------------------------------------------
Write-Step 'build base.tar (uncompressed)'
$baseTar = Join-Path $WorkRoot 'base.tar'
$t0 = Now-Ms
& $tarExe -cf $baseTar -C $subset .
if ($LASTEXITCODE -ne 0) { throw "tar create failed rc=$LASTEXITCODE" }
$baseMs = (Now-Ms) - $t0
$baseBytes = (Get-Item -LiteralPath $baseTar).Length
Write-Host (" base.tar = {0:N1} MB in {1:N1}s" -f ($baseBytes / 1MB), ($baseMs / 1000))

# --- 生成各压缩档 ---------------------------------------------------------
$pyCode = @'
import sys, time
from pathlib import Path
kind, lvl, src, dst = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
data = Path(src).read_bytes()
t0 = time.perf_counter()
if kind == "xz":
    import lzma
    # preset 与 filters 不能同时传；用 filters 指定 preset 才等价于 `xz -3`
    out = lzma.compress(data, format=lzma.FORMAT_XZ,
                        filters=[{"id": lzma.FILTER_LZMA2, "preset": lvl}])
else:
    from compression import zstd
    out = zstd.compress(data, level=lvl)
enc = time.perf_counter() - t0
Path(dst).write_bytes(out)
print(f"{len(out)} {enc:.2f}")
'@
$pyFile = Join-Path $WorkRoot 'encode.py'
Set-Content -LiteralPath $pyFile -Value $pyCode -Encoding UTF8

$archivex = @()
function New-Archive($kind, $level, $outName) {
    $dst = Join-Path $WorkRoot $outName
    $res = (& $PythonExe $pyFile $kind $level $baseTar $dst) -split '\s+'
    if ($LASTEXITCODE -ne 0) { Write-Host "  [skip] $kind -$level failed" -ForegroundColor Yellow; return $null }
    $size = [int64]$res[0]
    $enc = [double]$res[1]
    Write-Host (" {0,-10} level {1,-3} -> {2,7:N1} MB  (ratio {3:N2}x base, encode {4:N1}s)" -f `
            $kind, $level, ($size / 1MB), ($baseBytes / [math]::Max($size, 1)), $enc)
    [pscustomobject]@{ Format = "$kind-$level"; Path = $dst; SizeBytes = $size; EncodeSec = $enc }
}

Write-Step 'compress'
$xzArc = New-Archive 'xz' 3 'base.tar.xz'
if ($xzArc) { $archivex += $xzArc }
if ($hasZstd) {
    foreach ($lv in ($Levels -split '[,\s]+' | Where-Object { $_ } | ForEach-Object { [int]$_ })) {
        $a = New-Archive 'zstd' $lv 'base.tar.zst'
        if ($a) { $archivex += $a }
    }
} else {
    Write-Host ' [skip] zstd: python lacks compression.zstd (need 3.14+)' -ForegroundColor Yellow
}


# --- 解码基准：走 installer.iss 的真实路径 tar.exe -xf ---------------------
Write-Step "decode benchmark (tar.exe -xf, $Runs runs each, median)"
$results = @()
foreach ($arc in $archivex) {
    $times = @()
    for ($i = 1; $i -le $Runs; $i++) {
        $dest = Join-Path $WorkRoot ("x-" + $arc.Format + "-$i")
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        $t0 = Now-Ms
        & $tarExe -xf $arc.Path -C $dest
        $rc = $LASTEXITCODE
        $ms = (Now-Ms) - $t0
        Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue
        if ($rc -ne 0) { Write-Host ("  [warn] {0} run {1} rc={2}" -f $arc.Format, $i, $rc) -ForegroundColor Yellow }
        $times += $ms
    }
    $sorted = $times | Sort-Object
    $median = $sorted[[int][math]::Floor($sorted.Count / 2)]
    Write-Host (" {0,-10} {1,7:N2}s median   (runs: {2})" -f $arc.Format, ($median / 1000), (($times | ForEach-Object { '{0:N2}' -f ($_ / 1000) }) -join ', '))
    $results += [pscustomobject]@{
        Format      = $arc.Format
        SizeMB      = [math]::Round($arc.SizeBytes / 1MB, 1)
        RatioVsBase = [math]::Round($baseBytes / [math]::Max($arc.SizeBytes, 1), 2)
        EncodeSec   = [math]::Round($arc.EncodeSec, 1)
        DecodeSec   = [math]::Round($median / 1000, 2)
        MsPerFile   = [math]::Round($median / [math]::Max($copied, 1), 3)
    }
}

# --- 汇总 -----------------------------------------------------------------
Write-Step 'summary'
$results | Format-Table -AutoSize

if ($results.Count -ge 2) {
    $xz = $results | Where-Object { $_.Format -like 'xz-*' } | Select-Object -First 1
    Write-Host "`n 相对现状 (xz-3) 的差异：" -ForegroundColor Cyan
    foreach ($r in $results | Where-Object { $_.Format -notlike 'xz-*' }) {
        $dSize = (($r.SizeMB - $xz.SizeMB) / $xz.SizeMB) * 100
        $dTime = $r.DecodeSec - $xz.DecodeSec
        $verdict = if ([math]::Abs($dSize) -lt 3) { '体积门禁(<3%) 通过' } else { '体积门禁(<3%) 未通过' }
        Write-Host ("   {0,-10} size {1,+6:N1}%   decode {2,+6:N2}s   -> {3}" -f $r.Format, $dSize, $dTime, $verdict)
    }
}

$report = Join-Path $WorkRoot 'result.json'
[pscustomobject]@{
    generated  = (Get-Date).ToString('s')
    source     = $SourceDir
    files      = $copied
    bytes      = $bytes
    baseTarMB  = [math]::Round($baseBytes / 1MB, 1)
    tarExe     = (& $tarExe --version | Select-Object -First 1)
    python     = $pyVer
    results    = $results
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $report -Encoding UTF8
Write-Host "`n[json] $report" -ForegroundColor Green
Write-Host '注意：本实验只比较「归档格式」；真实 setup 体积还需 ISCC 压缩后的 dist 产物复核。'

