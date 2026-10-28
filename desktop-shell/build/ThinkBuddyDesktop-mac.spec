# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ThinkBuddy.app (macOS arm64 + x86_64, onedir + BUNDLE).

与 Windows 版 ``ThinkBuddyDesktop.spec`` 平行：
  * 入口仍是 ``desktop/main.py``（壳代码已平台化，mac 上 install_windows_shell_menu
    返回 False，走 pywebview Cocoa 后端 + 系统标准标题栏）
  * hiddenimports 改用 ``webview.platforms.cocoa``（WKWebView）
  * icon 用 ``.icns``（CI inline 生成，不签入仓库；本地开发可手动生成）
  * 产出 ``dist/ThinkBuddy.app``（BUNDLE，onedir；onefile 在 mac 上不支持 BUNDLE）
  * runtime.zip 仍内嵌（mac 上 build_runtime.py 不带 --no-zip，shell 首启自解压）
  * 架构由 CI runner + build_runtime.py --arch 决定：
      arm64 job → macos-15 (Apple Silicon) + --arch arm64
      x86_64 job → macos-15-intel (Intel) + --arch x86_64
    PyInstaller 自动跟随 runner 架构，spec 内无需显式 target_arch。

不在 excludes 里去掉 tkinter：mac 上 dialogs.py 的 fallback 用 tkinter.messagebox。
"""
import os
import re
from pathlib import Path

ROOT = Path(SPECPATH)          # build/  (this spec lives in build/)
PROJECT = ROOT.parent
ASSETS = PROJECT / "assets"
DIST = PROJECT / "dist"

# 壳版本（inject_version 在编译前写入 desktop/__init__.py，格式 1.6.14.MMddHHmm）。
# 写进 Info.plist 的 CFBundleShortVersionString，更新器用它做「本机已装版本」。
SHELL_VERSION = "1.0"
try:
    _m = re.search(r'__version__\s*=\s*"([^"]+)"',
                   (PROJECT / "desktop" / "__init__.py").read_text(encoding="utf-8"))
    if _m:
        SHELL_VERSION = _m.group(1)
except Exception:
    pass

datas = [
    (str(ASSETS / "icon.png"), "assets"),
]
# icon.icns 在 CI 里 inline 生成（sips + iconutil），本地开发可手动生成：
#   mkdir -p /tmp/icon.iconset && sips -z 1024 1024 icon.png --out /tmp/icon.iconset/icon_512x512@2x.png && ...
#   iconutil -c icns /tmp/icon.iconset -o assets/icon.icns
icon_path = ASSETS / "icon.icns"
if icon_path.exists():
    datas.append((str(icon_path), "assets"))
runtime_zip = DIST / "runtime.zip"
if runtime_zip.exists():
    datas.append((str(runtime_zip), "."))

a = Analysis(
    [str(PROJECT / "desktop" / "main.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "webview",
        "webview.platforms.cocoa",
        # pystray 的后端在运行时按 sys.platform 动态 import，静态分析扫不到。
        "pystray._darwin",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["PyQt5", "PySide2", "PySide6", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir 模式：二进制放进 .app/Contents/MacOS
    name="ThinkBuddy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # mac 上 upx 破坏 dylib 签名，导致启动即崩
    console=False,            # no terminal window（.app GUI 应用）
    disable_windowed_traceback=False,
    icon=str(icon_path) if icon_path.exists() else None,
)

app = BUNDLE(
    exe,
    a.binaries,
    a.datas,
    name="ThinkBuddy.app",
    icon=str(icon_path) if icon_path.exists() else None,
    bundle_identifier="com.thinkbuddy.desktop",
    info_plist={
        "CFBundleName": "ThinkBuddy",
        "CFBundleDisplayName": "ThinkBuddy",
        "CFBundleShortVersionString": SHELL_VERSION,
        "CFBundleExecutable": "ThinkBuddy",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
