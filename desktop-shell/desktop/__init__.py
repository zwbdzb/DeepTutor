"""ThinkBuddy Desktop — a native shell around the DeepTutor web app.

Layers:
    shell  : pywebview (WebView2) window + splash  -> desktop/main.py
    launcher: lifecycle orchestration             -> desktop/launcher.py
    process : deeptutor subprocess + health check -> desktop/process.py
    runtime : Python/Node/deeptutor resolution     -> desktop/runtime.py
"""

# 打包时由 build.ps1 用 Python 脚本替换为 "<引擎版本>+<时间戳>" 字面量（如 "1.6.14+10131200"），
# PyInstaller 打包时固化进 exe。开发态/源码态保持 0.1.3。
__version__ = "0.1.3"
APP_NAME = "ThinkBuddy"
