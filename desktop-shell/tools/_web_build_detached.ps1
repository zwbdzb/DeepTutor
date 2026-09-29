# 分离式 web 前端构建（escape hatch）。
# 背景：在 WorkBuddy 工具会话的进程树里，next build 孵化的原生辅助进程
# win32-x64.exe 总在数十秒内被杀（两次复现：拿锁后零 CPU 挂死，.next 零写入），
# 而用户交互控制台里同一构建正常。本脚本由 Start-Process 以独立进程启动，
# 脱离工具会话环境，日志与完成标记落盘供轮询。
$ErrorActionPreference = 'Continue'
$env:NEXT_TELEMETRY_DISABLED = '1'
# 钉死系统 node（工具会话 PATH 里的托管 node 不参与；构建历来在系统 node 下验证）
$env:Path = "C:\Program Files\nodejs;" + $env:Path

$log  = 'D:\studio\DeepTutor\desktop-shell\runtime-build\web-build.log'
$done = 'D:\studio\DeepTutor\desktop-shell\runtime-build\web-build.done'
Remove-Item $log, $done -Force -ErrorAction SilentlyContinue

Set-Location 'D:\studio\DeepTutor\web'
"START $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') node=$((Get-Command node).Source)" | Out-File $log -Encoding utf8
& "C:\Program Files\nodejs\npm.cmd" run build *>&1 | Out-File $log -Encoding utf8 -Append
$code = $LASTEXITCODE
"EXITCODE=$code END $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File $done -Encoding ascii
exit $code
