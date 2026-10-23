"""ThinkBuddy Desktop — 开机自启动（Windows）。

实现为 HKCU Run 键（per-user 安装，``PrivilegesRequired=lowest``，无需管理员）：

    HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
        "ThinkBuddy" = "C:\\...\\ThinkBuddyDesktop.exe"

单一真相源 = 该 Run 值：安装器 ``[Registry]`` 段写入（「开机自启动」任务），
桌面端「文件 → 开机自启动」读写同一键，两处天然同步，卸载时清除。
"""
from __future__ import annotations

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_APPROVED_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
)
VALUE_NAME = "ThinkBuddy"


def _winreg():
    """延迟导入 winreg：非 Windows 平台无此模块，且本模块在 mac 上也会被导入。"""
    import winreg

    return winreg


def target_command() -> str:
    """Run 键指向的启动命令（引号包裹的当前 exe 路径）。

    PyInstaller frozen 下 ``sys.executable`` 即 exe 本体路径（非 _MEIPASS 临时目录）。
    """
    return '"{}"'.format(sys.executable)


def is_enabled() -> bool:
    """Run 值是否存在（存在即视为已开启；路径漂移由下次 set_enabled(True) 自愈）。"""
    if os.name != "nt":
        return False
    try:
        winreg = _winreg()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """写入 / 删除 Run 值；返回是否成功（供菜单 toast 反馈）。"""
    if os.name != "nt":
        return False
    winreg = _winreg()
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        )
    except FileNotFoundError:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
    try:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, target_command())
            _clear_startup_approved(winreg)
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False
    finally:
        winreg.CloseKey(key)


def _clear_startup_approved(winreg) -> None:
    """清掉任务管理器「启动」里记录的禁用状态，避免 Run 值写了却不生效。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except OSError:
        pass
