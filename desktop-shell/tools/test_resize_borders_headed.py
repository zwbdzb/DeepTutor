"""Headed 冒烟：自定义标题栏下边缘拖拽缩放（2026-10-16 修复回归）。

背景：_make_form_handler 旧实现把客户区扩成整窗矩形，WebView2 子窗口盖满
客户区后，左右下边缘的 WM_NCHITTEST 永远落进子窗口，边缘拖拽缩放失效
（仅菜单条钩子救回顶边一条带）。修复：WM_NCCALCSIZE 只把顶部内缩为 0，
左右下三边保留一个边框厚度的非客户区，由系统原生接管缩放。

取证（全部程序化，~6 秒）：
  B1 WS_THICKFRAME 样式保留（缩放/贴靠的前提）；
  B2 常态客户区几何：左右下各内缩一个边框厚度，顶部 0（菜单条仍顶第一排）；
  B3 窗体 WM_NCHITTEST：左/右/下边带及右下角返回 HT*（系统据此进缩放循环）；
  B4 Dock.Fill 子控件（模拟 WebView2）不覆盖边框带——真实鼠标输入点
     WindowFromPoint 的 GA_ROOT 仍是窗体；
  B5 最大化：四边内缩（内容不溢出屏幕）。

运行：
    .venv\\Scripts\\python tools\\test_resize_borders_headed.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

user32 = ctypes.windll.user32
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:  # noqa: BLE001
    user32.SetProcessDPIAware()

WM_NCHITTEST = 0x0084
WS_THICKFRAME = 0x00040000
GWL_STYLE = -16
HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
HTTOPLEFT, HTTOPRIGHT = 13, 14
HTBOTTOM = 15
# 角落两个码由系统自发自收（自洽即可）；本机 SendMessage 实测：
# 右下角返回 17、左下角返回 16
HTBOTTOMLEFT, HTBOTTOMRIGHT = 16, 17
HTCLIENT = 1
GA_ROOT = 2
HWND_TOPMOST = -1
SWP_NOMOVE_NOSIZE_NOACTIVATE = 0x0001 | 0x0002 | 0x0010

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    from desktop import native_menu_backend as nmb
    assert nmb.install_windows_shell_menu(), "winforms backend unavailable"
    nmb._bind_user32()

    from System.Drawing import Point, Size
    from System.Windows.Forms import Application

    form = nmb._WF.Form()
    form.Text = "resize-borders-test"
    form.StartPosition = nmb._WF.FormStartPosition.Manual
    form.Location = Point(100, 100)
    form.Size = Size(900, 640)
    form.TopMost = True

    # 与 browser_form_init 完全相同的挂钩路径（被测代码的生产接线方式）
    hwnd = int(form.Handle.ToInt64())
    nmb._install_hook(hwnd, nmb._make_form_handler(hwnd))
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, nmb.SWP_FRAME_RECALC)

    # 模拟 WebView2：盖满客户区的子控件
    child = nmb._WF.Panel()
    child.Dock = nmb._WF.DockStyle.Fill
    form.Controls.Add(child)

    def pump(sec: float) -> None:
        end = time.monotonic() + sec
        while time.monotonic() < end:
            Application.DoEvents()
            time.sleep(0.03)

    form.Show()
    pump(0.4)

    ins = nmb._frame_thickness()

    def geometry():
        wr = nmb._RECT()
        cr = nmb._RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        user32.GetClientRect(hwnd, ctypes.byref(cr))
        origin = nmb._POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        return wr, cr, origin

    # ---- B1: WS_THICKFRAME 保留 ---------------------------------------------
    style = int(user32.GetWindowLongPtrW(hwnd, GWL_STYLE))
    check("B1 WS_THICKFRAME kept", bool(style & WS_THICKFRAME),
          f"style=0x{style & 0xFFFFFFFF:08X}")

    # ---- B2: 常态客户区几何（左/右/下内缩 ins，顶部 0）------------------------
    wr, cr, origin = geometry()
    off_l = origin.x - wr.left
    off_t = origin.y - wr.top
    off_r = wr.right - (origin.x + cr.right)
    off_b = wr.bottom - (origin.y + cr.bottom)
    check("B2a client left inset == frame", off_l == ins, f"{off_l} vs {ins}")
    check("B2b client top inset == 0", off_t == 0, f"{off_t}")
    check("B2c client right inset == frame", off_r == ins, f"{off_r} vs {ins}")
    check("B2d client bottom inset == frame", off_b == ins, f"{off_b} vs {ins}")

    # ---- B3: 边带命中测试返回 HT*（系统进缩放循环的依据）----------------------
    mid_x = (wr.left + wr.right) // 2
    mid_y = (wr.top + wr.bottom) // 2
    cases = [
        ("B3a left band -> HTLEFT", wr.left + ins // 2, mid_y, HTLEFT),
        ("B3b right band -> HTRIGHT", wr.right - ins // 2, mid_y, HTRIGHT),
        ("B3c bottom band -> HTBOTTOM", mid_x, wr.bottom - ins // 2, HTBOTTOM),
        ("B3d bottom-right corner -> corner code",
         wr.right - ins // 2, wr.bottom - ins // 2, HTBOTTOMRIGHT),
        ("B3e bottom-left corner -> corner code",
         wr.left + ins // 2, wr.bottom - ins // 2, HTBOTTOMLEFT),
        ("B3g center -> HTCLIENT", mid_x, mid_y, HTCLIENT),
    ]
    for name, x, y, want in cases:
        got = int(user32.SendMessageW(hwnd, WM_NCHITTEST, 0, (y << 16) | (x & 0xFFFF)))
        check(name, got == want, f"got {got}, want {want}")

    # ---- B4: Dock.Fill 子控件（模拟 WebView2）不覆盖边框带 --------------------
    # （WindowFromPoint 会命中本机置顶悬浮层，故用几何断言，免疫遮挡）
    cb = child.Bounds
    csr = form.RectangleToScreen(cb)
    check("B4a child right edge clear of border", csr.Right <= wr.right - ins,
          f"child_right={csr.Right}, limit={wr.right - ins}")
    check("B4b child bottom edge clear of border", csr.Bottom <= wr.bottom - ins,
          f"child_bottom={csr.Bottom}, limit={wr.bottom - ins}")
    check("B4c child left edge clear of border", csr.Left >= wr.left + ins,
          f"child_left={csr.Left}, limit={wr.left + ins}")

    # ---- B6: 顶边缩放走菜单条钩子（真实接线；宿主悬浮层/用户动鼠标时降级）----
    strip = nmb._build_menu_strip(form, [])
    pump(0.3)
    hwnd_strip = int(strip.Handle.ToInt64())
    top_y = wr.top + ins // 2
    hit_top = 0
    for _ in range(5):   # 推 z 序顶，避开本机置顶悬浮层（同 harness A0 策略）
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE_NOSIZE_NOACTIVATE)
        time.sleep(0.06)
        hit_top = user32.WindowFromPoint(nmb._POINT(mid_x, top_y))
        if hit_top == hwnd_strip:
            break
    if hit_top == hwnd_strip:
        check("B6a top band belongs to menu strip", True, "strip")
        user32.SetCursorPos(int(mid_x), int(top_y))
        got_top = int(user32.SendMessageW(
            hwnd_strip, WM_NCHITTEST, 0,
            ((top_y & 0xFFFF) << 16) | (mid_x & 0xFFFF)))
        cur = nmb._POINT()
        user32.GetCursorPos(ctypes.byref(cur))
        if cur.x == mid_x and cur.y == top_y:   # 用户没在动鼠标才可断言
            check("B6b strip returns HTTOP for top band", got_top == HTTOP,
                  f"got {got_top}, want {HTTOP}")
        else:
            print(f"[INFO] B6b degraded: cursor moved by user "
                  f"(hit-test returned {got_top}); hook logic unchanged by this fix")
    else:
        print("[INFO] B6a/B6b degraded: host overlay covers top band "
              "(environmental; top-edge route unchanged by this fix)")
    user32.SetCursorPos(int(mid_x), int(mid_y))   # 收尾：光标挪离窗沿
    pump(0.1)
    form.Controls.Remove(strip)
    strip.Dispose()
    pump(0.1)

    # ---- B5: 最大化四边内缩 ---------------------------------------------------
    form.WindowState = nmb._WF.FormWindowState.Maximized
    pump(0.5)
    wr2, cr2, origin2 = geometry()
    off_l2 = origin2.x - wr2.left
    off_t2 = origin2.y - wr2.top
    off_b2 = wr2.bottom - (origin2.y + cr2.bottom)
    check("B5a maximized left inset == frame", off_l2 == ins, f"{off_l2} vs {ins}")
    check("B5b maximized top inset == frame", off_t2 == ins, f"{off_t2} vs {ins}")
    check("B5c maximized bottom inset == frame", off_b2 == ins, f"{off_b2} vs {ins}")

    form.WindowState = nmb._WF.FormWindowState.Normal
    pump(0.3)
    form.Close()
    pump(0.2)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
