# Build ThinkBuddy end-to-end (Windows).
#   1. builds the offline runtime staging tree (embeddable python + deeptutor + node)
#   2. packages the native shell into dist\ThinkBuddyDesktop.exe  (PyInstaller)
#   3. (optional) compiles dist\ThinkBuddySetup-dt<版本>.<时间戳>.exe with Inno Setup 7
#
# 默认只产出两个包：ThinkBuddyDesktop.exe + ThinkBuddySetup-dt<版本>.<时间戳>.exe。
#   文件名示例: ThinkBuddySetup-dt1.6.14.10131621.exe（"dt"紧贴版本号，版本与时间戳都用「.」分隔）；
#   exe 属性/注册表里的 AppVersion 与文件名尾部一致（不含 dt 前缀）：1.6.14.10131621。
# dist\ThinkBuddyPortable.zip 默认【不】制作——它只是同一个运行时的另一种分发形态，
# 每次都要重新压缩 ~500MB / 2.1 万个文件（约 8 分钟），日常迭代没必要。
# 确实需要时显式加 -MakePortable。
#
# Prereqs: Python 3.11+ with PyInstaller, and Inno Setup 7 installed at the
#          standard path (only needed for step 3).
# Run:     powershell -ExecutionPolicy Bypass -File build\build.ps1
#              [-SkipRuntime] [-SkipWebBuild] [-MakePortable] [-MakeZip] [-SkipInstaller]
#              [-StrictRuntime] [-FastInstaller]

param(
    [switch]$SkipRuntime,       # kept for compatibility; the version gate ALWAYS runs (see below)
    [switch]$SkipWebBuild,      # CI 已在独立步骤构建前端+打包 deeptutor_web，跳过 [0/3] 避免重复 npm run build
    [switch]$MakePortable,      # also build dist\ThinkBuddyPortable.zip (slow, ~8 min)
    [switch]$MakeZip,           # also build dist\runtime.zip (slow, for Inno path)
    [switch]$SkipInstaller,     # do not compile the Inno .iss
    [switch]$StrictRuntime,     # content-hash runtime sources (slower; release-time assurance)
    [switch]$FastInstaller,     # Inno lzma2/fast（开发迭代用，安装包体积 +10~15%，压缩时间大幅缩短）
    # ADR-006 更新发布参数：出包后自动生成「带版本名安装包 + latest.json」（dist/）
    [string]$UpdateBase = "https://thinkbuddy.hanyoai.com/update",
    [string]$MinVersion = "0.1.0",
    [string]$Changelog = ""
)
$ErrorActionPreference = "Stop"
$Root   = Split-Path -Parent $PSScriptRoot
$VenPy  = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenPy)) { Write-Host "venv missing. run:  python -m venv .venv && .\.venv\Scripts\pip install pywebview pillow pyinstaller" -ForegroundColor Red; exit 1 }

# 每阶段计时：优化/定位耗时靠数据，不靠猜。
$script:StageStart = Get-Date
$BuildTotal = Get-Date
function Start-Stage { $script:StageStart = Get-Date }
function Stop-Stage($name) { Write-Host ("  [{0}] done in {1:mm\:ss}" -f $name, ((Get-Date) - $script:StageStart)) }

Push-Location $Root

if ($SkipWebBuild) {
    Write-Host "[0/3] skipped - CI pre-built frontend and deeptutor_web package"
} else {
Write-Host "[0/3] building and packaging the web frontend ..."
$WebDir = Join-Path (Split-Path -Parent $Root) "web"
$StampFile = Join-Path $WebDir ".web-build-stamp"

# web 源码指纹（git 追踪文件 + 非忽略未跟踪文件的 size+mtime）。未变则复用上次
# npm 产物。fail-open：git 不可用/枚举异常/无既有产物 → 一律照常重建。
function Get-WebFingerprint {
    try {
        $rel = & git -C $WebDir ls-files -c -o --exclude-standard 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $rel) { return $null }
        $sb = New-Object System.Text.StringBuilder
        foreach ($r in ($rel | Sort-Object)) {
            # 指纹戳自身不参与指纹（防自指：写戳后指纹恒变）
            if ($r -eq '.web-build-stamp' -or $r -eq '.deeptutor_web-stamp') { continue }
            $p = Join-Path $WebDir $r
            if (Test-Path -LiteralPath $p -PathType Leaf) {
                $fi = Get-Item -LiteralPath $p
                [void]$sb.Append($r); [void]$sb.Append('|')
                [void]$sb.Append($fi.Length); [void]$sb.Append('|')
                [void]$sb.AppendLine($fi.LastWriteTimeUtc.Ticks)
            }
        }
        $sha = [System.Security.Cryptography.SHA256]::Create()
        ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sb.ToString()))) -replace '-', '')
    } catch { return $null }
}

$fp = Get-WebFingerprint
$prev = $null
if (Test-Path -LiteralPath $StampFile) { try { $prev = (Get-Content -LiteralPath $StampFile -Raw).Trim() } catch {} }
# 产物完整性防线：除 server.js 外校验 next 运行时关键文件，防半成品 standalone
# 被静默复用打进安装包；缺失视为无产物 → 强制重建。
$standaloneDir = Join-Path $WebDir ".next\standalone"
$haveOutput = (Test-Path (Join-Path $standaloneDir "server.js")) -and `
              (Test-Path (Join-Path $standaloneDir "node_modules\next\dist\build\output\log.js"))
# web → deeptutor_web 拷贝增量：web 未变且已打包同一指纹时跳过删+拷。
$PkgStampFile = Join-Path $WebDir ".deeptutor_web-stamp"
$pkgStamp = $null
if (Test-Path -LiteralPath $PkgStampFile) { try { $pkgStamp = (Get-Content -LiteralPath $PkgStampFile -Raw).Trim() } catch {} }
$webFresh = ($fp -and $prev -and ($fp -eq $prev) -and $haveOutput)
if ($webFresh -and ($pkgStamp -eq $fp)) {
    Write-Host "[0/3] web sources unchanged AND deeptutor_web already packaged - skipping build & copy (stamp match)"
} else {
    if (-not $webFresh) {
        Push-Location $WebDir
        try {
            npm run build
            if ($LASTEXITCODE -ne 0) { throw "web build failed" }
        } finally { Pop-Location }
        # 指纹在构建【后】重算：build.mjs 会改写再还原部分配置文件，以落定后的树为准。
        $fp2 = Get-WebFingerprint
        if ($fp2) {
            try { Set-Content -LiteralPath $StampFile -Value $fp2 -Encoding ASCII -NoNewline } catch {}
            $fp = $fp2   # deeptutor_web-stamp 与 web stamp 同源
        }
    }
    Start-Stage
    & $VenPy (Join-Path (Split-Path -Parent $Root) "scripts\prepare_web_package.py") --skip-build
    if ($LASTEXITCODE -ne 0) { throw "web package preparation failed" }
    if ($fp) { try { Set-Content -LiteralPath $PkgStampFile -Value $fp -Encoding ASCII -NoNewline } catch {} }
    Stop-Stage "web package copy"
}
}  # end if (-not $SkipWebBuild)
try {
    # ---------- 1. offline runtime (staging tree) ---------------------------
    # 无条件跑 build_runtime.py（幂等增量）；版本门禁不可跳过——曾因跳过把旧运行时打进新包。
    Write-Host "[1/3] building/verifying offline runtime (embeddable python + deeptutor + node) ..."
    Start-Stage
    $rtArgs = @("tools\build_runtime.py")
    if (-not $MakeZip) { $rtArgs += "--no-zip" }
    if ($StrictRuntime) { $rtArgs += "--strict-fingerprint" }
    & $VenPy @rtArgs
    if ($LASTEXITCODE -ne 0) { throw "runtime build/gate failed" }
    Stop-Stage "runtime build/gate"
    # 陈旧 zip 防线：没打 zip 时删掉 dist 里的旧 runtime.zip，防旧运行时被静默内嵌进新壳。
    if (-not $MakeZip) {
        $staleZip = Join-Path $Root "dist\runtime.zip"
        if ($staleZip -and (Test-Path -LiteralPath $staleZip)) {
            Write-Host "[1] removing stale dist\runtime.zip (built without -MakeZip; prevents embedding an old runtime)"
            Remove-Item -LiteralPath $staleZip -Force
        }
    }

    # ---------- 1b. rebrand staging (DeepTutor -> ThinkBuddy) -----------------
    # 只重写 staging 产物里的用户可见品牌名；包名/类名/URL 受保护。幂等。
    Write-Host "[1b] rebranding staging (DeepTutor -> ThinkBuddy) ..."
    Start-Stage
    & $VenPy tools\rebrand.py
    if ($LASTEXITCODE -ne 0) { throw "rebrand failed" }
    Stop-Stage "rebrand"

    # ---------- 2. native shell exe ------------------------------------------
    # 版本号注入：壳版本 = 引擎版本 + 打包时间戳。
    $verPy = Join-Path (Split-Path -Parent $Root) "deeptutor\__version__.py"
    $m = Select-String -Path $verPy -Pattern '__version__\s*=\s*"([^"]+)"'
    if (-not $m) { throw "cannot parse __version__ from $verPy" }
    $dtVer = $m.Matches[0].Groups[1].Value
    # 统一用北京时间：时间戳（版本号/发布文件名）与 latest.json 的 published_at 同源。
    $beijing = [System.TimeZoneInfo]::ConvertTimeFromUtc(
        [System.DateTime]::UtcNow,
        [System.TimeZoneInfo]::FindSystemTimeZoneById("China Standard Time")
    )
    $timestamp = $beijing.ToString("MMddHHmm")
    # 壳版本号 形如 1.6.14.10131621：引擎版本 + 时间戳用「.」拼接，
    # 与注册表 DisplayVersion 同源同格式，避免"关于"框出现 +（用户要求）。
    $shellVer = "$dtVer.$timestamp"

    # 把 desktop/__init__.py 的 __version__ 行替换为打包版本号字面量。
    $initPy = Join-Path $Root "desktop\__init__.py"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $origInit = [System.IO.File]::ReadAllText($initPy, [System.Text.Encoding]::UTF8)
    Write-Host "[2/3] packaging ThinkBuddyDesktop.exe (PyInstaller, shell v$shellVer) ..."
    Start-Stage
    try {
        $newInit = $origInit -replace '__version__\s*=\s*"[^"]*"', "__version__ = `"$shellVer`""
        if ($newInit -eq $origInit) { throw 'failed to replace __version__ in __init__.py' }
        [System.IO.File]::WriteAllText($initPy, $newInit, $utf8NoBom)
        Write-Host "  __init__.py __version__ -> $shellVer"

        # 不加 --clean：PyInstaller 的分析缓存（build\）对壳这种几乎不变的输入
        # 完全可复用，--clean 每次强制全量重分析，纯浪费 1-3 分钟。
        & $VenPy -m PyInstaller --noconfirm build\ThinkBuddyDesktop.spec
        if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
    }
    finally {
        # 无论打包成功与否都还原，避免把临时版本号留在源码里。
        [System.IO.File]::WriteAllText($initPy, $origInit, $utf8NoBom)
        Write-Host "  __init__.py restored to original"
    }
    Stop-Stage "pyinstaller shell exe"

    # ---------- 2b. portable dir + zip (opt-in) ------------------------------
    if ($MakePortable) {
        Write-Host "[2b] assembling dist\portable & ThinkBuddyPortable.zip ..."
        & $VenPy tools\make_portable.py
        if ($LASTEXITCODE -ne 0) { throw "make_portable failed" }
    } else {
        Write-Host "[2b] skip ThinkBuddyPortable.zip (default). pass -MakePortable to build it."
    }

    # ---------- 3. click-installer (Inno Setup) ------------------------------
    if (-not $SkipInstaller) {
        $iscc = @(
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
            "C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
            "C:\Program Files\Inno Setup 7\ISCC.exe",
            "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            "C:\Program Files\Inno Setup 6\ISCC.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $iscc) {
            Write-Host "Inno Setup not found — skipping installer. Install from https://jrsoftware.org/isdl.php"
        } else {
            # AppVersion 与发布文件名同源于第 2 步：appVer=1.6.14.MMddHHmm，safeVer=dt+appVer。
            $appVer = "$dtVer.$timestamp"
            $safeVer = "dt$appVer"
            Write-Host "[3/3] compiling ThinkBuddySetup-$safeVer.exe (Inno Setup, AppVersion=$appVer) ..."
            Start-Stage
            # 编译前清掉历史固定名 ThinkBuddySetup.exe 残留，防与带版本名产物混淆。
            $legacySetup = Join-Path $Root "dist\ThinkBuddySetup.exe"
            if (Test-Path -LiteralPath $legacySetup) {
                try { Remove-Item -LiteralPath $legacySetup -Force; Write-Host "  removed stale dist\ThinkBuddySetup.exe (pre-rename artifact)" }
                catch { Write-Host "  WARN: cannot remove stale dist\ThinkBuddySetup.exe: $_" }
            }
            $isccArgs = @()
            if ($FastInstaller) { $isccArgs += "/DFastCompression" }
            $isccArgs += @("/DMyAppVersion=$appVer", "/DMyOutputName=$safeVer", "build\installer.iss")
            & $iscc @isccArgs
            if ($LASTEXITCODE -ne 0) { throw "iscc failed" }
            Stop-Stage "inno setup installer"

            # ---------- 3b. publish artifacts (named installer + latest.json) ----------
            # 上传顺序铁律：先安装包，后清单。
            $namedSetup = Join-Path $Root "dist\ThinkBuddySetup-$safeVer.exe"
            if (Test-Path -LiteralPath $namedSetup) {
                $fileHash = (Get-FileHash -LiteralPath $namedSetup -Algorithm SHA256).Hash.ToLower()
                $size = (Get-Item -LiteralPath $namedSetup).Length
                $manifest = @{
                    app          = "ThinkBuddy"
                    version      = $appVer
                    min_version  = $MinVersion
                    url          = ("{0}/ThinkBuddySetup-{1}.exe" -f $UpdateBase.TrimEnd('/'), $safeVer)
                    sha256       = $fileHash
                    size         = $size
                    published_at = $beijing.ToString("yyyy-MM-ddTHH:mm:ss+08:00")
                    changelog    = $Changelog
                } | ConvertTo-Json
                $manifestPath = Join-Path $Root "dist\latest.json"
                # UTF8 无 BOM：PS 5.1 的 Set-Content -Encoding UTF8 会带 BOM，客户端严格解析会卡住。
                [System.IO.File]::WriteAllText($manifestPath, $manifest, (New-Object System.Text.UTF8Encoding($false)))
                Write-Host "[3b] 发布产物已生成（上传顺序：先安装包，后 latest.json）："
                Write-Host "  $namedSetup"
                Write-Host "  $manifestPath  ->  $UpdateBase/"
            } else {
                Write-Host "[3b] dist\ThinkBuddySetup-$safeVer.exe 不存在，跳过发布产物生成"
            }
        }
    }
    Write-Host ""
    Write-Host ("TOTAL build time: {0:hh\:mm\:ss}" -f ((Get-Date) - $BuildTotal))
    Get-ChildItem "$Root\dist" -Filter *.exe | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}, LastWriteTime | Format-Table -AutoSize
}
finally { Pop-Location }
