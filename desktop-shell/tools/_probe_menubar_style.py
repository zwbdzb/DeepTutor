"""探针：菜单栏顶层项「文 件」字间空隙的来源 + 顶层/chip 样式参数采集。

定性问题：Text 字符串里有没有空格？间隙来自项 Padding、字体度量还是渲染？
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop import native_menu_backend as nmb

assert nmb.install_windows_shell_menu(), "winforms backend unavailable"
from System.Drawing import Point, Size          # noqa: E402
from System.Windows.Forms import Application    # noqa: E402
from System.Windows.Forms import VisualStyles   # noqa: E402  noqa-guard
from webview.menu import Menu, MenuAction       # noqa: E402


def noop() -> None:
    pass


def main() -> int:
    nmb.configure_account_chip(on_login=lambda: None, actions={})

    menu_list = [
        Menu("文件", items=[MenuAction("打开 Tokengine 平台", noop)]),
        Menu("编辑", items=[MenuAction("复制", noop)]),
    ]
    form = nmb._WF.Form()
    form.StartPosition = nmb._WF.FormStartPosition.Manual
    form.Location = Point(80, 80)
    form.Size = Size(900, 400)
    strip = nmb._build_menu_strip(form, menu_list)
    form.Show()
    for _ in range(20):
        Application.DoEvents()
        time.sleep(0.02)

    font = strip.Font
    print(f"strip.Font: {font.Name} {font.Size}pt (dpi-scaled)")
    for it in strip.Items:
        try:
            txt = str(it.Text)
        except Exception:  # noqa: BLE001
            continue
        w = int(it.Width)
        tw = int(nmb._WF.TextRenderer.MeasureText(txt, font).Width)
        p = it.Padding
        print(f"item Text={txt!r}  Width={w}  MeasureText={tw}  "
              f"Padding=({p.Left},{p.Top},{p.Right},{p.Bottom})  "
              f"AutoSize={it.AutoSize}  extra={w - tw}")

    # 下拉字体 / image margin 现状
    dd0 = strip.Items[0].DropDown
    print(f"menubar dd: Font={dd0.Font.Name} {dd0.Font.Size}pt  "
          f"ShowImageMargin={dd0.ShowImageMargin}  "
          f"ShowCheckMargin={dd0.ShowCheckMargin}")

    chip = nmb.account_chip()
    chip.set_model({"label": "paidaxing", "title": "", "logged_in": True,
                    "menu": {"header": {"title": "paidaxing", "sub": ""},
                              "items": [{"label": "个人中心", "action": "x"}]}})
    for _ in range(20):
        Application.DoEvents()
        time.sleep(0.02)
    r = chip.rect()
    print(f"chip rect={r}  display_text={chip._display_text()!r}")

    # 自证渲染：TopMost 推 z 序后截自己的窗口（宿主悬浮层可能污染，仅参考）
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.SetWindowPos(int(form.Handle.ToInt64()), -1, 0, 0, 0, 0, 0x0003)
        for _ in range(10):
            Application.DoEvents()
            time.sleep(0.03)
        from PIL import ImageGrab
        rect = nmb._RECT()
        user32.GetWindowRect(int(form.Handle.ToInt64()), ctypes.byref(rect))
        out = ROOT / "assets" / "probe-menubar-render.png"
        ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom)).save(out)
        print(f"screenshot: {out}")
    except Exception as exc:  # noqa: BLE001
        print(f"screenshot skipped: {exc}")

    form.Close()
    for _ in range(10):
        Application.DoEvents()
        time.sleep(0.02)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
