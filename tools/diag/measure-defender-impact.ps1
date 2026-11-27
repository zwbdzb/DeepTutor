<#
.SYNOPSIS
    模块5 诊断脚本：杀毒软件（Windows Defender 实时防护）干扰对照测试。

.DESCRIPTION
    两组对照，各跑 N 轮并取中位数：
      A) Defender 实时防护开启、无排除项 —— 完整安装计时
      B) 把安装目标目录 / 安装包目录 / 临时目录加入 Defender 排除项后重新计时
    同时记录两组安装期 MsMpEng（Defender 引擎）CPU 时间增量与峰值工作集（由
    measure-inno-install.ps1 采集），最后输出差值与占比。

    需要管理员权限才能增删 Defender 排除项。非管理员时会打印手工操作指南并退出（不静默失败）。

.PARAMETER SetupPath
    ThinkBuddySetup-*.exe（必填）。

.PARAMETER Rounds
    每组轮数，默认 2（★正式数据建议 3 轮取中位数，总耗时较长）。

.PARAMETER InstallDir
    安装目标目录，默认 %LOCALAPPDATA%\Programs\ThinkBuddy。

.PARAMETER OutDir
    各轮 JSON 与日志输出目录，默认 %TEMP%\tbdiag\defender。

.NOTES
    副作用说明：A 组前置会 Remove-MpPreference 掉「安装目录 / 安装包目录 / %TEMP%\tbdiag」
    这三处排除项，B 组结束后再整体移除（finally 保证执行）。
    若测试机上原本就有针对这三处的排除项，请先 `(Get-MpPreference).ExclusionPath` 记录再跑，
    结束后按记录恢复。脚本不触碰其它排除项，也不修改实时防护开关。

.EXAMPLE
    # 管理员 PowerShell
    powershell -ExecutionPolicy Bypass -File tools\diag\measure-defender-impact.ps1 `
        -SetupPath "D:\pkgs\ThinkBuddySetup-dt1.6.14.10131621.exe" -Rounds 3
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SetupPath,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\ThinkBuddy'),
    [int]$Rounds = 2,
    [string]$OutDir = (Join-Path $env:TEMP 'tbdiag\defender')
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$innoScript = Join-Path $PSScriptRoot 'measure-inno-install.ps1'
if (-not (Test-Path -LiteralPath $innoScript)) { throw ('缺少配套脚本: ' + $innoScript) }
if (-not (Test-Path -LiteralPath $SetupPath)) { throw ('找不到安装包: ' + $SetupPath) }
$SetupPath = (Resolve-Path -LiteralPath $SetupPath).Path
$setupDir = Split-Path -Parent $SetupPath

if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

function Get-Median {
    param([double[]]$Values)
    if (-not $Values -or $Values.Count -eq 0) { return $null }
    $sorted = @($Values | Sort-Object)
    $n = $sorted.Count
    if ($n % 2 -eq 1) { return $sorted[[int](($n - 1) / 2)] }
    return [math]::Round((($sorted[$n / 2 - 1] + $sorted[$n / 2]) / 2.0), 2)
}

$isAdmin = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host '============================================================'
Write-Host ' 模块5 · Defender 干扰对照测试'
Write-Host (' 主机: ' + $env:COMPUTERNAME + '   管理员: ' + $isAdmin + '   轮数/组: ' + $Rounds)
Write-Host (' 安装包: ' + $SetupPath)
Write-Host '============================================================'

try {
    $st = Get-MpComputerStatus -ErrorAction Stop
    Write-Host (' Defender: realtime=' + $st.RealTimeProtectionEnabled + '  antivirus=' + $st.AntivirusEnabled +
                '  mode=' + $st.AMRunningMode)
} catch {
    Write-Host (' Defender 状态读取失败: ' + $_.Exception.Message) -ForegroundColor Yellow
}

if (-not $isAdmin) {
    Write-Host ''
    Write-Host '【需人工采集】本项必须管理员权限才能增删排除项。手工步骤：' -ForegroundColor DarkYellow
    Write-Host '  A 组（实时防护开启，无排除）'
    Write-Host '    1) 以管理员打开「Windows 安全中心 > 病毒和威胁防护 > 管理设置」'
    Write-Host '       确认「实时保护」= 开，「排除项」中不存在 ThinkBuddy 安装目录 / 安装包目录 / 临时目录 tbdiag'
    Write-Host '    2) powershell -ExecutionPolicy Bypass -File tools\diag\measure-inno-install.ps1 `'
    Write-Host ("         -SetupPath `"<安装包>`" -CleanTarget -OutJson `"$env:TEMP\tbdiag\A1.json`"")
    Write-Host '    3) 重复 3 次（A1..A3），记录 wall_seconds 中位数'
    Write-Host '  B 组（加入排除项）'
    Write-Host ("    4) 管理员 PowerShell: Add-MpPreference -ExclusionPath `"<安装目录>`",`"<安装包目录>`",`"$env:TEMP\tbdiag`"")
    Write-Host '    5) 与 A 组同样跑 3 次（B1..B3），记录中位数'
    Write-Host '    6) 实验后务必清理: Remove-MpPreference -ExclusionPath "<同上三处>"'
    Write-Host '  记录项：两次总耗时差、安装期 MsMpEng CPU 秒数（脚本内已采样）、峰值工作集。'
    Write-Host ''
    Write-Host '【备选】（仅诊断用途、临时关闭实时防护）'
    Write-Host '    Set-MpPreference -DisableRealtimeMonitoring $true    # 实验后立即改回 $false'
    Write-Host '    注意：该开关可能被篡改防护（Tamper Protection）拒绝，需先在安全中心关闭篡改防护。'
    exit 3
}

$exclusionPaths = @($InstallDir, $setupDir, (Join-Path $env:TEMP 'tbdiag'))

function Invoke-Round {
    param([string]$Group, [int]$Index)
    $json = Join-Path $OutDir ('{0}{1}.json' -f $Group, $Index)
    $log = Join-Path $OutDir ('{0}{1}.install.log' -f $Group, $Index)
    Write-Host ''
    Write-Host ('---- [' + $Group + $Index + '] 开始安装计时 ----') -ForegroundColor Cyan
    & $innoScript -SetupPath $SetupPath -InstallDir $InstallDir -LogPath $log `
        -CleanTarget -OutJson $json
    if (-not (Test-Path -LiteralPath $json)) { return $null }
    $data = Get-Content -LiteralPath $json -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host ('     wall=' + $data.wall_seconds + 's  defender_cpu=' + $data.defender_cpu_seconds + 's')
    return $data
}

$groupA = New-Object System.Collections.ArrayList
$groupB = New-Object System.Collections.ArrayList
$addedExclusions = $false

try {
    # 确保 A 组处于「无排除项」状态
    try {
        Remove-MpPreference -ExclusionPath $exclusionPaths -ErrorAction SilentlyContinue
        Write-Host ' 已确保三处路径不在 Defender 排除列表中（A 组前置）。' -ForegroundColor DarkGray
    } catch { }

    Write-Host ''
    Write-Host '========== A 组：实时防护开启、无排除项 ==========' -ForegroundColor Yellow
    for ($i = 1; $i -le $Rounds; $i++) {
        $r = Invoke-Round -Group 'A' -Index $i
        if ($r) { [void]$groupA.Add($r) }
        Start-Sleep -Seconds 5
    }

    Write-Host ''
    Write-Host '========== B 组：目标目录加入排除项 ==========' -ForegroundColor Yellow
    foreach ($p in $exclusionPaths) {
        Write-Host ('  Add-MpPreference -ExclusionPath ' + $p)
    }
    Add-MpPreference -ExclusionPath $exclusionPaths -ErrorAction Stop
    $addedExclusions = $true
    for ($i = 1; $i -le $Rounds; $i++) {
        $r = Invoke-Round -Group 'B' -Index $i
        if ($r) { [void]$groupB.Add($r) }
        Start-Sleep -Seconds 5
    }
}
finally {
    if ($addedExclusions) {
        Write-Host ''
        Write-Host ' 清理 Defender 排除项（恢复测试前状态）...' -ForegroundColor DarkGray
        Remove-MpPreference -ExclusionPath $exclusionPaths -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------------------ 结果汇总 ---- #
function Get-GroupSummary {
    param($Rounds, [string]$Label)
    $walls = @($Rounds | ForEach-Object { [double]$_.wall_seconds })
    $cpu = @($Rounds | ForEach-Object { if ($null -ne $_.defender_cpu_seconds) { [double]$_.defender_cpu_seconds } })
    $sum = [pscustomobject]@{
        Group            = $Label
        Rounds           = $walls.Count
        WallAll          = ($walls -join ', ')
        WallMedian       = (Get-Median -Values $walls)
        DefenderCpuAll   = ($cpu -join ', ')
        DefenderCpuMedian = (Get-Median -Values $cpu)
        InstallFiles     = $(if ($Rounds.Count -gt 0) { $Rounds[0].install_files } else { $null })
    }
    return $sum
}

$sumA = Get-GroupSummary -Rounds $groupA -Label 'A: 实时防护开/无排除'
$sumB = Get-GroupSummary -Rounds $groupB -Label 'B: 已排除目标目录'

Write-Host ''
Write-Host '============================================================'
Write-Host ' 模块5 结果'
Write-Host '============================================================'
@($sumA, $sumB) | Format-Table -AutoSize | Out-String -Width 200 | Write-Host

if ($null -ne $sumA.WallMedian -and $null -ne $sumB.WallMedian) {
    $diff = [math]::Round($sumA.WallMedian - $sumB.WallMedian, 2)
    $pct = 0
    if ($sumA.WallMedian -gt 0) { $pct = [math]::Round(100.0 * $diff / $sumA.WallMedian, 1) }
    Write-Host (' 中位数差值（A - B）= ' + $diff + ' s   相对 A 的占比 = ' + $pct + '%')
    if ($diff -gt 0) {
        Write-Host ' 结论方向：Defender 实时扫描在总耗时中占上述份额（还包含 MsMpEng CPU 时间）。'
    } else {
        Write-Host ' 结论方向：排除项未带来可测收益（差值 <= 0），或噪声大于效应，需增加轮数。' -ForegroundColor Yellow
    }
} else {
    Write-Host ' 数据不足：至少需要一组成功完成的安装计时。' -ForegroundColor Yellow
}

$summaryPath = Join-Path $OutDir 'defender-summary.json'
[ordered]@{
    generated_at = (Get-Date -Format 'o')
    host         = $env:COMPUTERNAME
    rounds       = $Rounds
    group_a      = $sumA
    group_b      = $sumB
    median_diff_seconds = $(if ($null -ne $sumA.WallMedian -and $null -ne $sumB.WallMedian) {
        [math]::Round($sumA.WallMedian - $sumB.WallMedian, 2) } else { $null })
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
Write-Host ''
Write-Host ('JSON 汇总已写出: ' + $summaryPath) -ForegroundColor Green

