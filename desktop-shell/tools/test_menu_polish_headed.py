"""Headed 冒烟：菜单下拉「圆角 + 点外部收回」。

取证策略（对抗性审查结论）：本机 WorkBuddy 宿主是置顶 Chromium 悬浮层，
会盖住测试窗、吞掉物理点击的窗口派发、污染像素截图。因此：

  * 主证据一律程序化断言：看门狗状态（_OUTSIDE_FILTER）、下拉圆角模式
    （dd.Tag，由 Opened 事件里的 _apply_dropdown_shape 写入）、chip toggle
    守卫时序（伪造 MouseEventArgs 驱动 _on_click，与真实 MouseClick 同一
    处理函数）；
  * 真实输入通路作为增强：z 序抢到（A0）时优先真实点击；
  * 看门狗靠 GetAsyncKeyState + GetCursorPos（全局）感知点击，物理点击
    即使被悬浮层吞掉也能驱动收回（前轮已实证）——A3 无论可达与否都走
    真实点击，失败才程序化兜底。

覆盖（对应用户两点的验收标准）：
  1. 看门狗已安装；下拉开着时点击外部 → 收回（菜单下拉与 chip 下拉）；
  2. 下拉面板圆角（DWM 或 Region 模式）；
  3. 账号区 toggle：开着再点 = 收起；收起后 0.45s 内不重开（守卫）；过期可再开。

运行（自动开一个测试窗口，全程 ~10 秒）：
    .venv\\Scripts\\python tools\\test_menu_polish_headed.py
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

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


def send_click(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.06)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def item_center_screen(strip, item):
    """菜单项中心点的屏幕坐标（真实点击用）。"""
    b = item.Bounds
    scr = strip.RectangleToScreen(b)
    return (int((scr.Left + scr.Right) / 2), int((scr.Top + scr.Bottom) / 2))


def shot(form, out: Path) -> None:
    from desktop import native_menu_backend as nmb
    from PIL import ImageGrab
    rect = nmb._RECT()
    user32.GetWindowRect(int(form.Handle.ToInt64()), ctypes.byref(rect))
    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom)).save(out)
    print(f"    screenshot: {out}")


HWND_TOPMOST = -1
SWP_NOMOVE_NOSIZE_NOACTIVATE = 0x0001 | 0x0002 | 0x0010


def ensure_clickable(form, target_xy) -> bool:
    """把测试窗推到 z 序最顶（后台进程禁抢焦点但可改 z 序），并确认
    目标点下的窗口树根就是本窗口。WorkBuddy 等置顶悬浮层会盖住测试窗。"""
    hwnd = int(form.Handle.ToInt64())

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    for _ in range(5):
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE_NOSIZE_NOACTIVATE)
        time.sleep(0.08)
        pt = POINT(int(target_xy[0]), int(target_xy[1]))
        hit = user32.WindowFromPoint(pt)
        root = user32.GetAncestor(hit, 2)  # GA_ROOT
        if root == hwnd:
            return True
    return False


def main() -> int:
    from desktop import native_menu_backend as nmb
    assert nmb.install_windows_shell_menu(), "winforms backend unavailable"
    from System.Drawing import Point, Size
    from System.Windows.Forms import Application
    from System.Windows.Forms import MouseEventArgs, MouseButtons

    nmb.configure_account_chip(
        on_login=lambda: None,
        actions={
            "platform": lambda: None, "about": lambda: None,
            "refresh": lambda: None, "logout": lambda: None,
        },
    )

    from webview.menu import Menu, MenuAction

    def noop() -> None:
        pass

    menu_list = [
        Menu("文件", items=[MenuAction("新建", noop), MenuAction("打开", noop)]),
        Menu("编辑", items=[MenuAction("剪切", noop), MenuAction("复制", noop)]),
        Menu("视图", items=[MenuAction("放大", noop), MenuAction("缩小", noop)]),
        Menu("帮助", items=[MenuAction("关于", noop)]),
    ]

    form = nmb._WF.Form()
    form.Text = "menu-polish-test"
    form.StartPosition = nmb._WF.FormStartPosition.Manual
    form.Location = Point(80, 80)
    form.Size = Size(900, 640)
    form.TopMost = True
    strip = nmb._build_menu_strip(form, menu_list)

    def pump(sec: float) -> None:
        end = time.monotonic() + sec
        while time.monotonic() < end:
            Application.DoEvents()
            time.sleep(0.03)

    def wait_visible(get_visible, timeout=1.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            pump(0.03)
            if get_visible():
                return True
        return False

    def wait_hidden(get_visible, timeout=1.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            pump(0.03)
            if not get_visible():
                return True
        return False

    form.Show()
    form.Activate()
    pump(0.4)

    chip = nmb.account_chip()

    # ---- A0.5: 账号区显示时序 + 两套菜单样式一致性 ---------------------------
    # splash / 门控页阶段 chip 必须隐藏（用户要求：登录入口在页面中间按钮，
    # 标题栏只显示已登录用户名）；进应用页由会话循环显式 set_hidden(False)。
    check("A0.5a chip starts hidden (splash state)", chip.rect() is None)
    check("A0.5b menubar dropdown matches chip (no image margin)",
          not bool(strip.Items[0].DropDown.ShowImageMargin))
    dd_font = strip.Items[0].DropDown.Font
    check("A0.5c menubar dropdown font == chip font",
          str(dd_font.Name) == "Microsoft YaHei UI" and float(dd_font.Size) == 9.0,
          f"{dd_font.Name} {dd_font.Size}pt")

    chip.set_hidden(False)   # 模拟「已登录进入应用页」后的显示
    pump(0.15)
    check("A0.5d chip visible after set_hidden(False)", chip.rect() is not None)
    chip.set_model({
        "label": "paidaxing", "title": "", "logged_in": True,
        "menu": {
            "header": {"title": "paidaxing", "sub": "155****3365"},
            "items": [
                {"label": "个人中心", "action": "platform"},
                {"label": "关于", "action": "about"},
                {"type": "sep"},
                {"label": "退出登录", "action": "logout", "danger": True},
            ],
        },
    })
    pump(0.2)

    def chip_dd_visible() -> bool:
        return chip._dd is not None and bool(chip._dd.Visible)

    def open_chip_dropdown() -> bool:
        """程序化打开 chip 下拉并等待可见（不经过守卫，与 _on_click 的
        打开分支共用同一入口 _open_dropdown）。"""
        chip._open_dropdown()
        return wait_visible(chip_dd_visible)

    # ---- A0: 点击可达性（z 序强推后，目标点必须属于本窗口） ----------------
    # 本机 WorkBuddy 是置顶 Chromium 悬浮层，会盖住 TopMost 测试窗；
    # 后台进程不能抢前台焦点，但可以改 z 序（SetWindowPos HWND_TOPMOST）。
    item0 = strip.Items[0]
    cx, cy = item_center_screen(strip, item0)
    clickable = ensure_clickable(form, (cx, cy))
    # A0 是环境探测而非被测行为：宿主悬浮层（WorkBuddy）盖窗时必然不可达，
    # 此时真实点击通路禁用、断言走程序化路径 + 看门狗全局光标（A3 背书）。
    # 不计入 FAILS，避免回归门禁误报。
    if clickable:
        print("[INFO] A0 clicks land on test window  (real-input path enabled)")
    else:
        print("[INFO] A0 clicks land on test window  -> NO "
              "(host overlay blocks; programmatic assertions in effect)")

    # ---- A1: 「点外部收回」看门狗已随菜单条安装 ------------------------------
    wd = nmb._OUTSIDE_FILTER
    check("A1 outside-click watchdog installed",
          wd is not None and bool(wd.Enabled),
          "timer running" if wd is not None else "missing")

    # ---- A2: 打开「文件」下拉（真实点击优先）+ 圆角模式 ---------------------
    dd0 = item0.DropDown
    if clickable:
        send_click(cx, cy)
        ok_vis = wait_visible(lambda: dd0.Visible)
        check("A2a dropdown opens (real click)", ok_vis)
    else:
        item0.ShowDropDown()
        pump(0.35)
        check("A2a dropdown opens", bool(dd0.Visible), "programmatic")
    tag0 = str(dd0.Tag)
    check("A2b dropdown rounded", tag0 in ("dwm", "region"), f"mode={tag0}")

    try:
        shot(form, ROOT / "assets" / "menu-polish-dropdown.png")
    except Exception as exc:  # noqa: BLE001
        print(f"    screenshot skipped: {exc}")

    # ---- A3: 真实点击页面空白处 → 看门狗收回 --------------------------------
    # 物理点击即使被悬浮层吞掉（窗口收不到），看门狗也能靠全局光标位置感知；
    # 仅在不可达且真实点击失败时才程序化兜底（同一收合函数）。
    scr = form.PointToScreen(Point(450, 330))
    send_click(scr.X, scr.Y)
    ok_a3 = wait_hidden(lambda: dd0.Visible)
    a3_path = "real click (watchdog)"
    if not ok_a3 and not clickable:
        nmb._close_dropdowns_outside(scr.X, scr.Y)
        pump(0.15)
        ok_a3 = not dd0.Visible
        a3_path = "programmatic fallback"
    check("A3 outside click closes menu dropdown", ok_a3,
          a3_path if ok_a3 else "FAILED")

    # ---- A4: chip 菜单：打开 → 圆角 → toggle 收起 → 0.45s 守卫 → 可再开 -----
    r = chip.rect()
    ccx, ccy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2   # chip 中心（客户区）
    ctr = strip.PointToScreen(Point(ccx, ccy))

    if clickable:
        send_click(ctr.X, ctr.Y)   # 真实点击：顺带验证看门狗豁免 chip 触发区
        ok_open = wait_visible(chip_dd_visible)
        a4_path = "real click"
    else:
        ok_open = open_chip_dropdown()
        a4_path = "programmatic"
    check("A4a chip click opens dropdown", ok_open, a4_path)

    if ok_open:
        tag = str(chip._dd.Tag)
        check("A4b chip dropdown rounded", tag in ("dwm", "region"), f"mode={tag}")
        try:
            shot(form, ROOT / "assets" / "menu-polish-chip.png")
        except Exception as exc:  # noqa: BLE001
            print(f"    screenshot skipped: {exc}")

        # toggle 收起：伪造 MouseEventArgs 直接驱动 _on_click（与真实
        # MouseClick 同一处理函数，绕开悬浮层对物理点击的吞没）。
        ev = MouseEventArgs(MouseButtons.Left, 1, ccx, ccy, 0)
        chip._on_click(None, ev)
        pump(0.15)                       # Closed 事件跑完，_closed_at 落盘
        ok_close = not chip_dd_visible()
        check("A4c1 chip second click closes", ok_close)

        ok_guard = False
        if ok_close:
            chip._on_click(None, ev)     # 守卫窗口内再点 → 不应重开
            pump(0.2)
            ok_guard = not chip_dd_visible()
        check("A4c2 reopen guarded within 0.45s", ok_guard)

        pump(0.6)                        # 守卫过期
        chip._on_click(None, ev)
        pump(0.25)
        ok_reopen = chip_dd_visible()
        check("A4c3 chip reopens after guard expiry", ok_reopen)
        if ok_reopen:
            chip._on_click(None, ev)     # 收尾：再点收起
            pump(0.15)
    else:
        check("A4b chip dropdown rounded", False, "skipped: open failed")
        check("A4c1 chip second click closes", False, "skipped: open failed")
        check("A4c2 reopen guarded within 0.45s", False, "skipped")
        check("A4c3 chip reopens after guard expiry", False, "skipped")

    # ---- A5: 看门狗收合函数：外部点收回；下拉内部 / chip 区豁免 --------------
    far = form.PointToScreen(Point(40, 600))
    ok_a5a = False
    if open_chip_dropdown():
        nmb._close_dropdowns_outside(far.X, far.Y)
        pump(0.15)
        ok_a5a = not chip_dd_visible()
    check("A5a outside point closes chip dropdown", ok_a5a,
          "skipped: open failed" if not ok_a5a and not chip_dd_visible() and FAILS else "")

    ok_a5b = False
    if ok_a5a and open_chip_dropdown():
        b = chip._dd.Bounds              # ContextMenuStrip 顶级窗口：屏幕坐标
        nmb._close_dropdowns_outside((b.Left + b.Right) // 2,
                                     (b.Top + b.Bottom) // 2)
        pump(0.15)
        ok_a5b = chip_dd_visible()       # 点在下拉内部 → 豁免不收
        chip.close_dropdown()
        pump(0.1)
    check("A5b point inside dropdown is exempt", ok_a5b,
          "skipped" if not ok_a5a else "")

    ok_a5c = False
    if ok_a5b and open_chip_dropdown():
        nmb._close_dropdowns_outside(ctr.X, ctr.Y)   # chip 中心 → 豁免
        pump(0.15)
        ok_a5c = chip_dd_visible()
        chip.close_dropdown()
        pump(0.1)
    check("A5c point on chip is exempt", ok_a5c,
          "skipped" if not ok_a5b else "")

    nmb._close_all_dropdowns()
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
