# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ThinkBuddyDesktop.exe (onefile, windowed).

The exe bundles:
  * desktop shell (pywebview + launcher)  — the actual app
  * assets/icon.ico                        — exe icon
  * dist/runtime.zip (if present)          — offline deeptutor runtime,
                                             extracted on first launch
  * UCRT DLLs (ucrtbase.dll + api-ms-win-crt-*.dll)  — Universal C Runtime,
                                             so the shell starts on machines
                                             that lack it (Win7/8 without
                                             KB2999226, stripped LTSC, etc.)
The shell uses the embedded runtime when present, else the system
deeptutor/node on PATH.
"""
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH)          # build/  (this spec lives in build/)
PROJECT = ROOT.parent
ASSETS = PROJECT / "assets"
DIST = PROJECT / "dist"


def _collect_ucrt_binaries() -> list[tuple[str, str]]:
    """Locate the Universal C Runtime DLLs and return (src, dest) pairs.

    python312.dll imports the api-ms-win-crt-*.dll API-set forwarders, which
    in turn forward to ucrtbase.dll. PyInstaller deliberately excludes all of
    them (treats them as system DLLs), so on a machine without the UCRT the
    onefile dies at boot with "Failed to load Python DLL". We bundle them
    explicitly next to python312.dll (dest=".") so the loader resolves them
    from the _MEI extraction directory.

    Source locations:
      * ucrtbase.dll            -> %SystemRoot%\\System32\\ucrtbase.dll
      * api-ms-win-crt-*.dll    -> WinSxS amd64_microsoft-windows-...namespace-downlevel
                                   (the downlevel API-set forwarder component)
    Non-Windows / not-found -> empty list (build succeeds; UCRT assumed present).
    """
    if sys.platform != "win32":
        return []
    win_dir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    found: list[tuple[str, str]] = []
    # 1. ucrtbase.dll — the actual UCRT implementation.
    ucrtbase = win_dir / "System32" / "ucrtbase.dll"
    if ucrtbase.is_file():
        found.append((str(ucrtbase), "."))
    # 2. api-ms-win-crt-*.dll forwarders (newest matching component first).
    winsxs = win_dir / "WinSxS"
    components = sorted(
        winsxs.glob("amd64_microsoft-windows-m..namespace-downlevel_*"),
        reverse=True,
    )
    seen: set[str] = set()
    for comp in components:
        for dll in comp.glob("api-ms-win-crt-*.dll"):
            if dll.name in seen:
                continue
            seen.add(dll.name)
            found.append((str(dll), "."))
        if seen:
            break  # one component is enough; they're all forwarders.
    return found


datas = [
    (str(ASSETS / "icon.ico"), "assets"),
    (str(ASSETS / "icon.png"), "assets"),
]
runtime_zip = DIST / "runtime.zip"
if runtime_zip.exists():
    datas.append((str(runtime_zip), "."))

a = Analysis(
    [str(PROJECT / "desktop" / "main.py")],
    pathex=[str(PROJECT)],
    binaries=_collect_ucrt_binaries(),
    datas=datas,
    hiddenimports=[
        "webview",
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PySide2", "PySide6", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ThinkBuddyDesktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # off: UPX 压缩每个 DLL 费时数分钟，且易触发杀软误报；
                                    # 安装包侧 Inno lzma2 已做整体压缩，这里压不压收益趋零
    console=False,                  # no console window (WorkBuddy-like)
    disable_windowed_traceback=False,
    icon=str(ASSETS / "icon.ico"),
)
