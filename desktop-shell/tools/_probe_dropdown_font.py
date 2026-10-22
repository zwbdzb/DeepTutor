"""探针：菜单栏下拉字体与顶栏一致性（统一字号 Microsoft YaHei UI）。

背景：下拉（ToolStripDropDownMenu）此前未显式设 Font，继承 WinForms 默认
8.25pt「Microsoft Sans Serif」——中文字形回退宋体，比顶栏明显小一圈。
统一字号由模块常量 ``native_menu_backend._CHROME_FONT_PT`` 单点控制
（当前 10.5pt，对齐 Web 页正文视觉尺寸）。本探针构建真实菜单条，断言每个
下拉（含嵌套子菜单）与顶栏字体一致，并打开「帮助」下拉截图供人眼确认。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop import native_menu_backend as nmb

assert nmb.install_windows_shell_menu(), "winforms backend unavailable"
from System.Drawing import Point, Size          # noqa: E402
from System.Windows.Forms import Application    # noqa: E402
from webview.menu import Menu, MenuAction, MenuSeparator  # noqa: E402

EXPECTED_NAME = "Microsoft YaHei UI"
# 期望字号从被测模块常量读取——字号调整只改一处，探针自动跟随，不再漂移。
EXPECTED_SIZE = float(nmb._CHROME_FONT_PT)


def noop() -> None:
    pass


def build_menu():
    return [
        Menu("文件", items=[
            MenuAction("打开 ThinkBuddy 网站", noop),
            MenuSeparator(),
            MenuAction("检查更新...", noop),
        ]),
        Menu("编辑", items=[
            MenuAction("撤销", noop),
            MenuAction("复制", noop),
            Menu("更多", items=[MenuAction("全选", noop)]),  # 嵌套子菜单
        ]),
        Menu("帮助", items=[
            MenuAction("配置使用指南", noop),
            MenuAction("打开日志目录", noop),
            MenuSeparator(),
            MenuAction("关于 ThinkBuddy", noop),
        ]),
    ]


def font_sig(font) -> str:
    return f"{font.Name} {font.Size}pt"


def main() -> int:
    nmb.configure_account_chip(on_login=lambda: None, actions={})

    form = nmb._WF.Form()
    form.StartPosition = nmb._WF.FormStartPosition.Manual
    form.Location = Point(120, 120)
    form.Size = Size(900, 400)
    strip = nmb._build_menu_strip(form, build_menu())
    form.Show()
    for _ in range(20):
        Application.DoEvents()
        time.sleep(0.02)

    strip_sig = font_sig(strip.Font)
    report = {"strip_font": strip_sig, "dropdowns": [], "pass": True}
    for it in strip.Items:
        try:
            txt = str(it.Text)
        except Exception:  # noqa: BLE001
            continue
        dd = getattr(it, "DropDown", None)
        if dd is None:
            continue  # 顶栏分隔线等
        dd_sig = font_sig(dd.Font)
        item_count = int(dd.Items.Count)
        item_sig = font_sig(dd.Items[0].Font) if item_count else None
        first_h = int(dd.Items[0].Height) if item_count else None
        ok = dd_sig == f"{EXPECTED_NAME} {EXPECTED_SIZE}pt"
        report["dropdowns"].append({
            "owner": txt,
            "dropdown_font": dd_sig,
            "first_item_font": item_sig,
            "first_item_height_px": first_h,
            "pass": ok,
        })
        report["pass"] = report["pass"] and ok

    # 嵌套子菜单（编辑 → 更多）
    edit_menu = None
    for it in strip.Items:
        if str(it.Text) == "编辑":
            edit_menu = it
            break
    if edit_menu is not None:
        for child in edit_menu.DropDownItems:
            nested = getattr(child, "DropDown", None)
            if nested is not None and int(nested.Items.Count) > 0:
                nested_sig = font_sig(nested.Font)
                report["nested_font"] = nested_sig
                report["pass"] = report["pass"] and (
                    nested_sig == f"{EXPECTED_NAME} {EXPECTED_SIZE}pt"
                )

    # 打开「帮助」下拉截图（人眼确认：字号与顶栏一致、无宋体衬线感）
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetWindowPos(
            int(form.Handle.ToInt64()), -1, 0, 0, 0, 0, 0x0003
        )  # HWND_TOPMOST | SWP_NOMOVE|SWP_NOSIZE
        for it in strip.Items:
            if str(it.Text) == "帮助":
                it.ShowDropDown()
                break
        for _ in range(25):
            Application.DoEvents()
            time.sleep(0.03)
        from PIL import ImageGrab

        rect = nmb._RECT()
        user32.GetWindowRect(int(form.Handle.ToInt64()), ctypes.byref(rect))
        out = ROOT / "assets" / "probe-dropdown-font.png"
        ImageGrab.grab(
            bbox=(rect.left, rect.top, rect.right, rect.bottom)
        ).save(out)
        print(f"screenshot: {out}")
        # 收起下拉，避免留场
        nmb._close_all_dropdowns()
        for _ in range(10):
            Application.DoEvents()
            time.sleep(0.02)
    except Exception as exc:  # noqa: BLE001  截图失败不影响判定
        print(f"screenshot skipped: {exc}")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("RESULT:", "PASS" if report["pass"] else "FAIL")

    form.Close()
    for _ in range(10):
        Application.DoEvents()
        time.sleep(0.02)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
