#Requires -Version 5.1
<#
.SYNOPSIS
    在已生成的 base.tar 上跑「压缩 + 解码落盘」基准（compare-runtime-archive-formats 的续跑段）。
.DESCRIPTION
    与主脚本配合：主脚本负责建子集与 base.tar（慢在 I/O），本段只做 CPU/解码对比，
    便于反复调参而不必重建子集。
#>
[CmdletBinding()]
param(
    [string]$WorkRoot = (Join-Path $env:TEMP 'tb-arcfmt'),
    [string]$PythonExe = 'D:\pywork\DeepTutor\.venv\Scripts\python.exe',
    [string]$Levels = '3,19',
    [int]$Runs = 3
)
$ErrorActionPreference = 'Stop'
function Now-Ms { [DateTime]::UtcNow.Ticks / 10000.0 }

$tarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
$baseTar = Join-Path $WorkRoot 'base.tar'
if (-not (Test-Path $baseTar)) { throw "missing $baseTar; run compare-runtime-archive-formats.ps1 first" }
$baseBytes = (Get-Item $baseTar).Length
$fileCount = (Get-ChildItem (Join-Path $WorkRoot 'subset') -Recurse -File -Force | Measure-Object).Count
Write-Host (" base.tar = {0:N1} MB, subset files = {1:N0}" -f ($baseBytes / 1MB), $fileCount)

$pyFile = Join-Path $WorkRoot 'encode.py'
$pyCode = @'
import sys, time
from pathlib import Path
kind, lvl, src, dst = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
data = Path(src).read_bytes()
t0 = time.perf_counter()
if kind == "xz":
    import lzma
    out = lzma.compress(data, format=lzma.FORMAT_XZ,
                        filters=[{"id": lzma.FILTER_LZMA2, "preset": lvl}])
else:
    from compression import zstd
    out = zstd.compress(data, level=lvl)
enc = time.perf_counter() - t0
Path(dst).write_bytes(out)
print(f"{len(out)} {enc:.2f}")
'@
Set-Content -LiteralPath $pyFile -Value $pyCode -Encoding UTF8

$arcs = @()
function Do-Encode($kind, $level, $outName) {
    $dst = Join-Path $WorkRoot $outName
    if (Test-Path $dst) { Remove-Item $dst -Force }
    $errFile = Join-Path $WorkRoot "enc-$kind-$level.err"
    $res = (& $PythonExe $pyFile $kind $level $baseTar $dst) 2>$errFile
    if ($LASTEXITCODE -ne 0) {
        Write-Host (" [skip] {0}-{1} failed: {2}" -f $kind, $level, ((Get-Content $errFile -Raw -EA SilentlyContinue) -replace '\s+', ' ')) -ForegroundColor Yellow
        return $null
    }
    $parts = ($res | Out-String).Trim() -split '\s+'
    $size = [int64]$parts[0]; $enc = [double]$parts[1]
    Write-Host (" {0,-8} L{1,-3} -> {2,8:N2} MB   ratio {3:N2}x   encode {4,6:N1}s" -f `
            $kind, $level, ($size / 1MB), ($baseBytes / [math]::Max($size, 1)), $enc)
    [pscustomobject]@{ Format = "$kind-$level"; Path = $dst; SizeBytes = $size; EncodeSec = $enc }
}

Write-Host "`n=== compress ==="
$a = Do-Encode 'xz' 3 'base.tar.xz'; if ($a) { $arcs += $a }
foreach ($lv in ($Levels -split '[,\s]+' | Where-Object { $_ } | ForEach-Object { [int]$_ })) {
    $a = Do-Encode 'zstd' $lv 'base.tar.zst'; if ($a) { $arcs += $a }
}

Write-Host "`n=== decode (tar.exe -xf, $Runs runs, median) ==="
$rows = @()
foreach ($arc in $arcs) {
    $times = @()
    for ($i = 1; $i -le $Runs; $i++) {
        $dest = Join-Path $WorkRoot ("x-" + $arc.Format + "-$i")
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        $t0 = Now-Ms
        & $tarExe -xf $arc.Path -C $dest
        $rc = $LASTEXITCODE
        $ms = (Now-Ms) - $t0
        Remove-Item -LiteralPath $dest -Recurse -Force -EA SilentlyContinue
        if ($rc -ne 0) { Write-Host "  [warn] $($arc.Format) run $i rc=$rc" -ForegroundColor Yellow }
        $times += $ms
    }
    $sorted = $times | Sort-Object
    $median = $sorted[[int][math]::Floor($sorted.Count / 2)]
    Write-Host (" {0,-8} {1,7:N2}s median  (runs: {2})" -f $arc.Format, ($median / 1000), (($times | ForEach-Object { '{0:N2}' -f ($_ / 1000) }) -join ', '))
    $rows += [pscustomobject]@{
        Format    = $arc.Format
        SizeMB    = [math]::Round($arc.SizeBytes / 1MB, 2)
        RatioVsBase = [math]::Round($baseBytes / [math]::Max($arc.SizeBytes, 1), 2)
        EncodeSec = [math]::Round($arc.EncodeSec, 1)
        DecodeSec = [math]::Round($median / 1000, 2)
        MsPerFile = [math]::Round($median / [math]::Max($fileCount, 1), 3)
    }
}

Write-Host "`n=== summary ==="
$rows | Format-Table -AutoSize
$report = Join-Path $WorkRoot 'result-resume.json'
[pscustomobject]@{
    generated = (Get-Date).ToString('s'); files = $fileCount
    baseMB = [math]::Round($baseBytes / 1MB, 2)
    tar = (& $tarExe --version | Select-Object -First 1); rows = $rows
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $report -Encoding UTF8
Write-Host "[json] $report"
