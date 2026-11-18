"""原生信息框（跨平台）。

Windows：纯 ctypes MessageBoxW（PyInstaller 安全，无第三方依赖）。
macOS：osascript（系统自带，外部进程弹窗）。
  ⚠️ 不要用 tkinter：壳进程里已有 pywebview 的 NSApplication，tk.Tk()
  会在同进程再起一个 Tcl/NSApplication，AppKit 直接 SIGILL 崩掉主线程
  （2026-10-16 真机「关于」弹窗崩溃）。osascript 是独立进程，零干扰。

图标常量沿用 Win32 定义：
  MB_ICONINFORMATION = 0x40      MB_OK = 0x0
  MB_ICONWARNING     = 0x30      MB_OKCANCEL = 0x1
返回值对齐 Win32 按钮ID：1=IDOK，2=IDCANCEL。
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess

log = logging.getLogger("dt.dialogs")


def _mac_message_box(title: str, text: str, icon: int) -> int:
    """macOS 信息框：osascript display dialog（独立进程，不动本进程 AppKit）。

    返回值对齐 Win32：1=IDOK，2=IDCANCEL。
    """
    is_okcancel = bool(icon & 0x01)  # MB_OKCANCEL

    esc_title = title.replace("\\", "\\\\").replace('"', '\\"')
    esc_text = text.replace("\\", "\\\\").replace('"', '\\"')
    if is_okcancel:
        script = (
            f'display dialog "{esc_text}" with title "{esc_title}" '
            f'buttons {{"Cancel", "OK"}} default button "OK" '
            f'cancel button "Cancel"'
        )
        try:
            res = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=60, check=False,
            )
            return 1 if res.returncode == 0 else 2
        except Exception:  # noqa: BLE001
            log.exception("message box (osascript) failed")
            return 0
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

    Windows：MessageBoxW；macOS：osascript。返回值对齐 Win32（1=OK,2=CANCEL）。
    """
    if os.name == "nt":
        try:
            return ctypes.windll.user32.MessageBoxW(0, text, title, icon)
        except Exception as exc:  # noqa: BLE001
            log.exception("message box failed: %s", exc)
            return 0
    return _mac_message_box(title, text, icon)


def confirm(title: str, text: str, warning: bool = False) -> bool:
    """是/否二选一确认框：返回 True 表示用户点了「确定」。

    页面里的二次确认（删除对话等）走这里，替掉 WebView2 自带的
    「127.0.0.1:3782 显示」提示框——那是宿主浏览器 chrome，既没品牌也不
    符合桌面端观感。

    默认**不带图标**（MB_OKCANCEL 纯按钮，观感最轻，删除类确认就是这种）；
    warning=True 才加 MB_ICONWARNING（黄色三角），留给「退出登录」这类
    需要更强提示的动作。
    """
    icon = 0x01 | (0x30 if warning else 0x00)  # MB_OKCANCEL [| MB_ICONWARNING]
    return message_box(title, text, icon) == 1
