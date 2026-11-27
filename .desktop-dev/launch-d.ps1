# D:\pywork\DeepTutor\.desktop-dev\launch-d.ps1
# D 模式一键启动：后端(uvicorn --reload) + 前端(next dev HMR) + 壳(attach 到外部服务)
# 退出：关掉壳窗口 → 在 "DT web" / "DT backend" 窗口各按一次 Ctrl+C
$ErrorActionPreference = 'Stop'
$Root        = 'D:\pywork\DeepTutor'
$DevDir      = Join-Path $Root '.desktop-dev'
$Py          = Join-Path $Root '.venv\Scripts\python.exe'
$Dtt         = Join-Path $Root '.venv\Scripts\deeptutor.exe'
$ShellPy     = Join-Path $Root 'desktop-shell\.venv\Scripts\python.exe'
$BackendPort = 8011
$FrontendPort= 3811

function Test-PortBusy([int]$port) {
    [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# ---- 0. 前置检查 ----------------------------------------------------------
foreach ($p in @($Py, $Dtt, $ShellPy)) {
    if (-not (Test-Path $p)) {
        throw "缺少：$p`n先跑：.\.venv\Scripts\python.exe -m pip install -e . --no-deps  以及 desktop-shell\.venv 的 pywebview/pystray/pillow"
    }
}
if (Get-Process ThinkBuddyDesktop -ErrorAction SilentlyContinue) {
    throw "已安装的 ThinkBuddy 正在运行：单实例互斥体是全局的，源码壳会被静默接管。请先托盘退出它。"
}
foreach ($p in @($BackendPort, $FrontendPort)) {
    if (Test-PortBusy $p) { throw "端口 $p 已被占用：先 Ctrl+C 上次的窗口，或用脚本末尾的清理命令" }
}
New-Item -ItemType Directory -Force -Path $DevDir | Out-Null

# ---- 1. 后端（uvicorn --reload，只重启 .py） ------------------------------
Start-Process powershell -ArgumentList @('-NoExit','-Command',
  "`$Host.UI.RawUI.WindowTitle='DT backend :$BackendPort'; Set-Location '$Root'; & '$Dtt' serve --host 127.0.0.1 --port $BackendPort --reload")

# ---- 2. 前端（next dev，代理到 8011） ------------------------------------
Start-Process powershell -ArgumentList @('-NoExit','-Command',
  "`$Host.UI.RawUI.WindowTitle='DT web :$FrontendPort'; Set-Location '$Root\web'; `$env:BACKEND_PORT='$BackendPort'; npm run dev -- -p $FrontendPort")

# ---- 3. 等前端真的监听（最多 180s），而不是猜 -----------------------------
$deadline = (Get-Date).AddSeconds(180)
while (-not (Test-PortBusy $FrontendPort)) {
    if ((Get-Date) -gt $deadline) { throw "前端 :$FrontendPort 180s 内未就绪，去看 'DT web' 窗口的报错" }
    Start-Sleep -Milliseconds 500
}
Write-Host "前端已就绪 -> http://127.0.0.1:$FrontendPort" -ForegroundColor Green

# ---- 4. 壳（attach，不 spawn、关窗不杀后端） ------------------------------
Start-Process powershell -ArgumentList @('-NoExit','-Command',
  "`$Host.UI.RawUI.WindowTitle='DT shell'; " +
  "`$env:DEEPTUTOR_DESKTOP_ATTACH_URL='http://127.0.0.1:$FrontendPort'; " +
  "`$env:DEEPTUTOR_DESKTOP_SKIP_LOGIN='1'; " +
  "`$env:DEEPTUTOR_DESKTOP_ROOT='$DevDir'; " +
  "`$env:DEEPTUTOR_DESKTOP_HOME='$DevDir\home'; " +
  "`$env:PATH='$Root\.venv\Scripts;'+`$env:PATH; " +
  "Set-Location '$Root\desktop-shell'; & '$ShellPy' -m desktop.main")

Write-Host "`n三个窗口已启动。退出：关壳窗口 -> 两个 Ctrl+C。" -ForegroundColor Cyan
Write-Host "残留端口检查：" -ForegroundColor DarkGray
Write-Host "  Get-NetTCPConnection -LocalPort $BackendPort,$FrontendPort -State Listen -ErrorAction SilentlyContinue" -ForegroundColor DarkGray
