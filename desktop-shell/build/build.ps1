# Build ThinkBuddy end-to-end (Windows).
#   1. builds the offline runtime staging tree (embeddable python + deeptutor + node)
#   2. packages the native shell into dist\ThinkBuddyDesktop.exe  (PyInstaller)
#   3. (optional) compiles dist\ThinkBuddySetup.exe with Inno Setup 7
#
# 默认只产出两个包：ThinkBuddyDesktop.exe + ThinkBuddySetup.exe。
# dist\ThinkBuddyPortable.zip 默认【不】制作——它只是同一个运行时的另一种分发形态，
# 每次都要重新压缩 ~500MB / 2.1 万个文件（约 8 分钟），日常迭代没必要。
# 确实需要时显式加 -MakePortable。
#
# Prereqs: Python 3.11+ with PyInstaller, and Inno Setup 7 installed at the
#          standard path (only needed for step 3).
# Run:     powershell -ExecutionPolicy Bypass -File build\build.ps1 [-SkipRuntime] [-MakePortable] [-MakeZip] [-SkipInstaller]

param(
    [switch]$SkipRuntime,       # kept for compatibility; the version gate ALWAYS runs (see below)
    [switch]$MakePortable,      # also build dist\ThinkBuddyPortable.zip (slow, ~8 min)
    [switch]$MakeZip,           # also build dist\runtime.zip (slow, for Inno path)
    [switch]$SkipInstaller      # do not compile the Inno .iss
)
$ErrorActionPreference = "Stop"
$Root   = Split-Path -Parent $PSScriptRoot
$VenPy  = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenPy)) { Write-Host "venv missing. run:  python -m venv .venv && .\.venv\Scripts\pip install pywebview pillow pyinstaller" -ForegroundColor Red; exit 1 }

Push-Location $Root

Write-Host "[0/3] building and packaging the web frontend ..."
$WebDir = Join-Path (Split-Path -Parent $Root) "web"
$StampFile = Join-Path $WebDir ".web-build-stamp"

# web 源码指纹（git 追踪文件 + 非忽略未跟踪文件；node_modules/.next 被
# .gitignore 天然排除）。与 build_runtime.py 的源指纹同哲学：源码未变 →
# 复用上次 npm 产物，跳过 10 分钟级的 next build（2026-09-24：next build
# 在本机还偶发"拿锁后零 CPU 静默卡死"，日常只改 Python 的迭代更没必要重跑）。
# fail-open：git 不可用/枚举异常/无既有产物 → 一律照常重建，宁可慢，不可跳错。
function Get-WebFingerprint {
    try {
        $rel = & git -C $WebDir ls-files -c -o --exclude-standard 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $rel) { return $null }
        $sb = New-Object System.Text.StringBuilder
        foreach ($r in ($rel | Sort-Object)) {
            if ($r -eq '.web-build-stamp') { continue }   # 指纹戳自身不参与指纹（防自指：写戳后指纹恒变）
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
# 产物完整性防线（2026-09-28 轮8 事故闭环）：build 进程冻结可产出半成品
# standalone——server.js 存在但 next 运行时关键文件（dist/build/output/*）缺失，
# stamp 命中即静默复用残缺产物打进安装包，用户装机后前端 MODULE_NOT_FOUND 崩溃。
# 故除 server.js 外必须同时校验 next 运行时关键文件；缺失视为无产物 → 强制重建。
$standaloneDir = Join-Path $WebDir ".next\standalone"
$haveOutput = (Test-Path (Join-Path $standaloneDir "server.js")) -and `
              (Test-Path (Join-Path $standaloneDir "node_modules\next\dist\build\output\log.js"))
if ($fp -and $prev -and ($fp -eq $prev) -and $haveOutput) {
    Write-Host "[0/3] web sources unchanged since last build - reusing .next output (stamp match)"
} else {
    Push-Location $WebDir
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "web build failed" }
    } finally { Pop-Location }
    # 指纹在构建【后】重算：build.mjs 会改写并还原 next-env.d.ts/tsconfig.json，
    # 以构建落定后的树状态为准，下次构建才能命中 stamp。
    $fp2 = Get-WebFingerprint
    if ($fp2) { try { Set-Content -LiteralPath $StampFile -Value $fp2 -Encoding ASCII -NoNewline } catch {} }
}
& $VenPy (Join-Path (Split-Path -Parent $Root) "scripts\prepare_web_package.py") --skip-build
if ($LASTEXITCODE -ne 0) { throw "web package preparation failed" }
try {
    # ---------- 1. offline runtime (staging tree) ---------------------------
    # 无条件跑 build_runtime.py：它自身幂等——staging 版本与本地源一致且源码未变时
    # 只跑冒烟测试+版本门禁（十几秒）；版本不一致/源码 fingerprint 变化时自动重装。
    # 【版本门禁不可跳过】-SkipRuntime 不再绕过它：曾因跳过这一步把 1.6.9 旧运行时
    # 打进安装包而源码已是 1.6.10（1.6.7 时也发生过一次）。
    Write-Host "[1/3] building/verifying offline runtime (embeddable python + deeptutor + node) ..."
    if ($MakeZip) {
        & $VenPy tools\build_runtime.py
    } else {
        & $VenPy tools\build_runtime.py --no-zip
        # 陈旧 zip 防线：spec 只在 dist\runtime.zip 存在时才把它内嵌进 onefile
        # exe。若本轮没打 zip 而 dist 里留着上次的旧 zip，旧运行时会被静默
        # 打进新壳（2026-09-23 曾因此把 1.6.9 缓存刷新给用户）。不打包时就
        # 删掉残留，宁可壳里没运行时（安装版兜底），不可静默回退旧版。
        $staleZip = Join-Path $Root "dist\runtime.zip"
        if ($staleZip -and (Test-Path -LiteralPath $staleZip)) {
            Write-Host "[1] removing stale dist\runtime.zip (built without -MakeZip; prevents embedding an old runtime)"
            Remove-Item -LiteralPath $staleZip -Force
        }
    }
    if ($LASTEXITCODE -ne 0) { throw "runtime build/gate failed" }

    # ---------- 1b. rebrand staging (DeepTutor -> ThinkBuddy) -----------------
    # 只重写 staging 产物里的用户可见品牌名；包名/类名/URL 受保护。幂等。
    Write-Host "[1b] rebranding staging (DeepTutor -> ThinkBuddy) ..."
    & $VenPy tools\rebrand.py
    if ($LASTEXITCODE -ne 0) { throw "rebrand failed" }

    # ---------- 2. native shell exe ------------------------------------------
    Write-Host "[2/3] packaging ThinkBuddyDesktop.exe (PyInstaller) ..."
    & $VenPy -m PyInstaller --noconfirm --clean build\ThinkBuddyDesktop.spec
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }

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
            # version injection: app version tracks deeptutor/__version__.py
            $verPy = Join-Path (Split-Path -Parent $Root) "deeptutor\__version__.py"
            $m = Select-String -Path $verPy -Pattern '__version__\s*=\s*"([^"]+)"'
            if (-not $m) { throw "cannot parse __version__ from $verPy" }
            $appVer = "0.2.0+dt" + $m.Matches[0].Groups[1].Value
            Write-Host "[3/3] compiling ThinkBuddySetup.exe (Inno Setup, AppVersion=$appVer) ..."
            & $iscc "/DMyAppVersion=$appVer" "build\installer.iss"
            if ($LASTEXITCODE -ne 0) { throw "iscc failed" }
        }
    }
    Write-Host ""
    Get-ChildItem "$Root\dist" -Filter *.exe | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}, LastWriteTime | Format-Table -AutoSize
}
finally { Pop-Location }
