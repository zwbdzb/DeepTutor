<#
.SYNOPSIS
    ThinkBuddy 测试前清理脚本 —— 恢复干净状态，保证安装计时数据可信。
.DESCRIPTION
    停止进程、删除安装目录、用户数据、PyInstaller 临时目录、快捷方式、注册表残留。
    每次诊断测量前执行一次。
.PARAMETER Quiet
    静默模式，减少输出。
#>
param(
    [switch]$Quiet
)

$ErrorActionPreference = "SilentlyContinue"

function Write-Status($msg, $color = "Cyan") {
    if (-not $Quiet) { Write-Host $msg -ForegroundColor $color }
}

Write-Status "=== ThinkBuddy 清理开始 ===" "Yellow"

# 1. 停止 ThinkBuddy 相关进程
Write-Status "[1/7] 停止 ThinkBuddy 进程..."
$procs = @("ThinkBuddyDesktop", "ThinkBuddy", "python", "node")
foreach ($p in $procs) {
    Get-Process -Name $p -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2

# 2. 删除安装目录
Write-Status "[2/7] 删除安装目录..."
$installPaths = @(
    "$env:LOCALAPPDATA\Programs\ThinkBuddy"
    "$env:ProgramFiles\ThinkBuddy"
    "${env:ProgramFiles(x86)}\ThinkBuddy"
    "$env:LOCALAPPDATA\ThinkBuddyDesktop"
)
foreach ($p in $installPaths) {
    if (Test-Path $p) {
        Write-Status "  删除: $p"
        Remove-Item $p -Recurse -Force
    }
}

# 3. 删除用户数据目录
Write-Status "[3/7] 删除用户数据目录..."
$userDataPaths = @(
    "$env:APPDATA\ThinkBuddy"
    "$env:LOCALAPPDATA\ThinkBuddy"
    "$env:LOCALAPPDATA\ThinkBuddyDesktop"
)
foreach ($p in $userDataPaths) {
    if (Test-Path $p) {
        Write-Status "  删除: $p"
        Remove-Item $p -Recurse -Force
    }
}

# 4. 删除 PyInstaller onefile 临时解压目录
Write-Status "[4/7] 删除 PyInstaller 临时目录..."
$tempMei = Get-ChildItem "$env:TEMP" -Directory -Filter "_MEI*" -ErrorAction SilentlyContinue
foreach ($d in $tempMei) {
    Write-Status "  删除: $($d.FullName)"
    Remove-Item $d.FullName -Recurse -Force
}

# 5. 删除快捷方式
Write-Status "[5/7] 删除快捷方式..."
$shortcuts = @(
    "$env:PUBLIC\Desktop\ThinkBuddy*.lnk"
    "$env:USERPROFILE\Desktop\ThinkBuddy*.lnk"
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\ThinkBuddy*"
)
foreach ($s in $shortcuts) {
    Remove-Item $s -Force -ErrorAction SilentlyContinue
}

# 6. 清理注册表残留
Write-Status "[6/7] 清理注册表..."
$regPaths = @(
    "HKCU:\Software\ThinkBuddy"
    "HKLM:\Software\ThinkBuddy"
    "HKCU:\Software\ThinkBuddyDesktop"
    "HKLM:\Software\ThinkBuddyDesktop"
)
foreach ($r in $regPaths) {
    if (Test-Path $r) {
        Write-Status "  删除: $r"
        Remove-Item $r -Recurse -Force
    }
}

# 7. 验证清理结果
Write-Status "[7/7] 验证清理结果..."
$leftover = @()
foreach ($p in $installPaths + $userDataPaths) {
    if (Test-Path $p) { $leftover += $p }
}

if ($leftover.Count -eq 0) {
    Write-Status "✅ 清理完成，环境干净" "Green"
} else {
    Write-Status "⚠️ 以下目录未清理干净（可能需要管理员权限）:" "Red"
    foreach ($l in $leftover) {
        Write-Status "  $l" "Red"
    }
}

Write-Status "=== 清理结束 ===" "Yellow"