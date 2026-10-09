"""原生信息框（跨平台）。

Windows：纯 ctypes MessageBoxW（PyInstaller 安全，无第三方依赖）。
macOS：优先 tkinter.messagebox（python-build-standalone 带 tkinter），
       fallback 到 osascript（系统自带）。

图标常量沿用 Win32 定义：
  MB_ICONINFORMATION = 0x40      MB_OK = 0x0
  MB_ICONWARNING     = 0x30      MB_OKCANCEL = 0x1
返回值对齐 Win32 按钮ID：1=IDOK，2=IDCANCEL。
"""
from __future__ import annotations

import ctypes
import logging
import os

log = logging.getLogger("dt.dialogs")


def _mac_message_box(title: str, text: str, icon: int) -> int:
    """macOS 信息框：优先 tkinter，fallback osascript。

    返回值对齐 Win32：1=IDOK，2=IDCANCEL。
    """
    is_okcancel = bool(icon & 0x01)  # MB_OKCANCEL
    is_warning = (icon & 0xF0) == 0x30
    is_error = (icon & 0xF0) == 0x10

    try:
        import tkinter as tk
        from tkinter import messagebox as mb

        root = tk.Tk()
        root.withdraw()
        if is_okcancel:
            ok = mb.askokcancel(title, text, parent=root)
            result = 1 if ok else 2
        elif is_error:
            mb.showerror(title, text, parent=root)
            result = 1
        elif is_warning:
            mb.showwarning(title, text, parent=root)
            result = 1
        else:
            mb.showinfo(title, text, parent=root)
            result = 1
        root.destroy()
        return result
    except Exception:  # noqa: BLE001  tkinter 不可用（install_only flavor 可能无 Tcl/Tk）
        # fallback: osascript（macOS 自带，无需 Python 依赖）
        import subprocess

        esc_title = title.replace("\\", "\\\\").replace('"', '\\"')
        esc_text = text.replace("\\", "\\\\").replace('"', '\\"')
        if is_okcancel:
            script = (
                f'display dialog "{esc_text}" with title "{esc_title}" '
                f'buttons {{"Cancel", "OK"}} default button "OK" '
                f'cancel button "Cancel"'
            )
            res = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=60, check=False,
            )
            return 1 if res.returncode == 0 else 2
        script = (
            f'display dialog "{esc_text}" with title "{esc_title}" '
            f'buttons {{"OK"}} default button "OK"'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                timeout=60, check=False,
            )
        except Exception:  # noqa: BLE001
            log.warning("message box fallback failed; printing to stderr")
            print(f"{title}: {text}", file=__import__("sys").stderr, flush=True)
        return 1


def message_box(title: str, text: str, icon: int = 0x40) -> int:
    """弹一个模态信息框；owner=None（无父窗口）。返回用户点击的按钮 ID。

    Windows：MessageBoxW；macOS：tkinter/osascript。返回值对齐 Win32（1=OK,2=CANCEL）。
    """
    if os.name == "nt":
        try:
            return ctypes.windll.user32.MessageBoxW(0, text, title, icon)
        except Exception as exc:  # noqa: BLE001
            log.exception("message box failed: %s", exc)
            return 0
    return _mac_message_box(title, text, icon)
