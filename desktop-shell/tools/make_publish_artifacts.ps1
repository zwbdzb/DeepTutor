# 为一份既有的 ThinkBuddySetup.exe 生成发布产物：
#   1) <对应dist>\ThinkBuddySetup-<safeVer>.exe   （带版本名 = 上传到服务器的那一份）
#   2) <对应dist>\latest.json                     （客户端「检查更新」读的清单，无 BOM UTF-8）
#
# 版本号与 build.ps1 同源：0.2.0+dt<deeptutor/__version__.py>；'+' 转 '_' 防 URL 路径问题。
# 独立于完整构建：
#   * 正式发布——重跑 build.ps1 出新的 ThinkBuddySetup.exe（含最新代码）后，再跑本脚本出发布包；
#   * 快速验证——可直接对已有/旧 ThinkBuddySetup.exe 跑本脚本，先验证「下载→校验→安装」链路
#     （注意：旧安装包不含当前源码的改动，仅用于链路验证，不做正式分发）。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File tools\make_publish_artifacts.ps1
#   powershell -ExecutionPolicy Bypass -File tools\make_publish_artifacts.ps1 -UpdateBase https://thinkbuddy.hanyoai.com/update -MinVersion "0.1.0" -Changelog "xxx"
param(
    [string]$Installer,    # 基础安装包路径（默认 <desktop-shell>\dist\ThinkBuddySetup.exe）
    [string]$UpdateBase = "https://thinkbuddy.hanyoai.com/update",
    [string]$MinVersion = "0.1.0",
    [string]$Changelog = ""
)
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot          # desktop-shell 根（脚本在 tools/ 下）
if (-not $Installer) { $Installer = Join-Path $Root "dist\ThinkBuddySetup.exe" }
if (Test-Path -LiteralPath $Installer -PathType Container) {
    throw "Installer 指向了目录：$Installer"
}
if (-not (Test-Path -LiteralPath $Installer)) {
    throw "找不到基础安装包：$Installer（请先运行 build\build.ps1 出包）"
}

$verPy = Join-Path (Split-Path -Parent $Root) "deeptutor\__version__.py"
$m = Select-String -Path $verPy -Pattern '__version__\s*=\s*"([^"]+)"'
if (-not $m) { throw "无法解析版本号：$verPy" }
$appVer = "0.2.0+dt" + $m.Matches[0].Groups[1].Value
$safeVer = $appVer.Replace('+', '_')

$publishDir = Split-Path -Parent $Installer
$namedSetup = Join-Path $publishDir "ThinkBuddySetup-$safeVer.exe"
Copy-Item -LiteralPath $Installer -Destination $namedSetup -Force
$fileHash = (Get-FileHash -LiteralPath $namedSetup -Algorithm SHA256).Hash.ToLower()
$size     = (Get-Item -LiteralPath $namedSetup).Length
$manifest = @{
    app          = "ThinkBuddy"
    version      = $appVer
    min_version  = $MinVersion
    url          = ("{0}/ThinkBuddySetup-{1}.exe" -f $UpdateBase.TrimEnd('/'), $safeVer)
    sha256       = $fileHash
    size         = $size
    published_at = (Get-Date).ToString("o")
    changelog    = $Changelog
} | ConvertTo-Json
$manifestPath = Join-Path $publishDir "latest.json"
# UTF8 无 BOM：PS 5.1 的 Set-Content -Encoding UTF8 会带 BOM，客户端 json.loads 会被卡住。
[System.IO.File]::WriteAllText($manifestPath, $manifest, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "发布产物已生成："
Write-Host "  $namedSetup"
Write-Host "  $manifestPath"
Write-Host "上传这两个文件到 $UpdateBase/ 即可让客户端检查到更新（顺序铁律：先安装包，后 latest.json）"
