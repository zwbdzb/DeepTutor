#!/usr/bin/env python3
"""把 desktop/__init__.py 的 __version__ 注入为 "<引擎版本>.<MMDDHHmm>"（mac 打包用）。

与 Windows build.ps1 的版本注入同源（见 desktop/updater.py 版本语义）：
引擎版本取仓库根 deeptutor/__version__.py，时间戳为构建时刻的北京时间 月日时分
（如 2026-10-16 15:54 → 10161554）。PyInstaller 打包时固化进二进制，
「关于」与检查更新据此显示/比对。打包完成后用 --restore 还原开发态版本。

用法：
    python tools/inject_version.py            # 注入（PyInstaller 之前执行）
    python tools/inject_version.py --restore  # 还原为 0.1.3（打包之后执行）
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHELL_DIR = Path(__file__).resolve().parent.parent  # desktop-shell/
ENGINE_VERSION_FILE = SHELL_DIR.parent / "deeptutor" / "__version__.py"
DESKTOP_INIT = SHELL_DIR / "desktop" / "__init__.py"
DEV_VERSION = "0.1.3"  # 与 desktop/__init__.py 开发态保持一致

_BJT = timezone(timedelta(hours=8))


def engine_version() -> str:
    match = re.search(
        r'__version__\s*=\s*"([^"]+)"',
        ENGINE_VERSION_FILE.read_text(encoding="utf-8"),
    )
    if not match:
        raise SystemExit(f"无法从 {ENGINE_VERSION_FILE} 解析引擎版本")
    return match.group(1)


def main() -> None:
    version = (
        DEV_VERSION if "--restore" in sys.argv
        else f"{engine_version()}.{datetime.now(_BJT).strftime('%m%d%H%M')}"
    )
    text = DESKTOP_INIT.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'__version__\s*=\s*"[^"]*"',
        f'__version__ = "{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise SystemExit(f"在 {DESKTOP_INIT} 未找到 __version__ 赋值")
    DESKTOP_INIT.write_text(new_text, encoding="utf-8")
    print(f"desktop/__init__.py __version__ -> {version}")


if __name__ == "__main__":
    main()
