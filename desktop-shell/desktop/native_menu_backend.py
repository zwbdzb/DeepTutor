"""Windows 自定义标题栏 + 原生菜单（EduBuddy 桌面壳）。

设计目标（对齐 WorkBuddy 桌面端观感）：
* 菜单栏位于窗口**第一排**（顶到窗口最上沿），不再挤在系统标题栏下方；
* 第一排右侧内嵌 最小化 / 最大化 / 关闭 三个窗口按钮（自绘，悬停高亮，
  关闭钮悬停红色）；紧邻其左是**账号区**（未登录「登录」、已登录用户名，
  点击弹原生账号菜单，见 ``AccountChip`` 与 docs/adr/ADR-004）；
* 保留系统级体验：边缘拖拽缩放（WS_THICKFRAME）、Aero Snap（Win+方向键、
  拖到屏幕边缘）、最小化/还主动画、Alt+F4、任务栏预览；
* 菜单条空白处：按下拖动窗口、双击最大化/还原、右键弹系统菜单。

实现要点：
* 用 ctypes 给 BrowserForm 装轻量 WndProc 钩子，仅拦截 ``WM_NCCALCSIZE``：
  把客户区扩展到整个窗口矩形 → 系统标题栏在窗口可见**之前**就消失
  （无闪烁）。窗口样式里的 WS_CAPTION / WS_THICKFRAME / WS_MAXIMIZEBOX
  全部保留，因此缩放、贴靠、动画这些系统行为原封不动。
* 菜单走 pywebview 的 ``Menu`` / ``MenuAction`` / ``MenuSeparator`` 通道，
  渲染成 ``MenuStrip``（Dock=Top）——与 pywebview 原版 ``set_window_menu``
  同一条久经验证的路径；客户区扩大后菜单条自然顶到第一排。
* 菜单条自身的 WndProc 钩子只处理 ``WM_NCHITTEST``：顶部/左右 8px 边框
  区域返回 HT*，让被菜单条盖住的窗口边缘仍可拖拽缩放（按钮区除外——
  按钮区返回 HTCLIENT，保证整块按钮可点，与系统原生一致）。
* 窗口按钮**不是** ToolStripMenuItem：右对齐项的视觉序与集合序相反
  （曾出现 ✕□─ 颠倒），尺寸/贴边也不受控。按钮矩形按 DPI 换算
  （46×36 逻辑像素，对齐 VS Code / Chrome / Win11 惯例）直接绘制在
  菜单条画布右上角，鼠标事件手动命中测试，悬停高亮、关闭钮红色。

历史教训：此前用 ``Form.Menu = MainMenu()``（经典菜单）+ 在 loaded 事件里
做 ctypes 调用清理图标，冻结版中会引发 Python 工作线程集体冻结
（引导/尾随线程静默死亡，见 2026-09-23 日志）——本模块不再触碰这两条路径。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time

from webview.menu import Menu, MenuAction, MenuSeparator

log = logging.getLogger("dt.native_menu")

# 延迟绑定的 WinForms / System.Drawing 类型（install_windows_shell_menu 里赋值）。
# 不能在模块顶部 import —— pythonnet 的 System 命名空间要先 import clr，
# 而顶层 clr 导入会破坏非 Windows 平台兼容性。
_WF = None
_Color = _Font = _Pen = _SolidBrush = _Size = _SmoothingMode = None
_Point = _Action = None
_Func = _Type = None
_GraphicsPath = _LinearGradientBrush = _Region = None

# --------------------------------------------------------------------------- #
# Win32 常量与 ctypes 原型                                                      #
# --------------------------------------------------------------------------- #
GWL_WNDPROC = -4
WM_DESTROY = 0x0002
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONDBLCLK = 0x00A3
WM_SYSCOMMAND = 0x0112
HTCAPTION = 2
HTCLIENT = 1
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
SM_CXSIZEFRAME = 32
SM_CXPADDEDBORDER = 92
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
# SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
SWP_FRAME_RECALC = 0x0001 | 0x0002 | 0x0004 | 0x0020


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_size_t, ctypes.c_ssize_t,
)

_user32 = ctypes.windll.user32
_user32.CallWindowProcW.restype = ctypes.c_ssize_t
_user32.CallWindowProcW.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_size_t, ctypes.c_ssize_t,
]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
if not hasattr(_user32, "SetWindowLongPtrW"):  # 32 位 Python 兜底
    _user32.SetWindowLongPtrW = _user32.SetWindowLongW
    _user32.GetWindowLongPtrW = _user32.GetWindowLongW

# hwnd → (原窗口过程, 回调引用)；全局持有，防止 ctypes 回调被 GC
_HOOKS: dict[int, tuple] = {}


def _frame_thickness() -> int:
    """可缩放窗口的边框厚度（含扩展边），用于贴边命中测试。"""
    try:
        return _user32.GetSystemMetrics(SM_CXSIZEFRAME) + _user32.GetSystemMetrics(
            SM_CXPADDEDBORDER
        )
    except Exception:  # noqa: BLE001
        return 8


def _install_hook(hwnd: int, handler) -> None:
    """替换 hwnd 的窗口过程；handler(msg, wp, lp) 返回非 None 表示已处理。"""
    original = _user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)

    def _cb(h, msg, wp, lp):
        try:
            result = handler(msg, wp, lp)
        except Exception:  # noqa: BLE001  钩子绝不能把异常抛回消息循环
            log.exception("wndproc handler error (msg=0x%X)", msg)
            result = None
        if result is not None:
            return result
        return _user32.CallWindowProcW(original, h, msg, wp, lp)

    keepalive = _WNDPROC(_cb)
    _HOOKS[hwnd] = (original, keepalive)
    _user32.SetWindowLongPtrW(
        hwnd, GWL_WNDPROC, ctypes.cast(keepalive, ctypes.c_void_p)
    )


def _unhook(hwnd: int) -> None:
    entry = _HOOKS.pop(hwnd, None)
    if entry:
        _user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, entry[0])


# --------------------------------------------------------------------------- #
# 下拉菜单圆角：Win11 走 DWM 原生圆角，Win10 退化为 Region 裁剪                   #
# --------------------------------------------------------------------------- #
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUND = 2
_dwmapi = None


def _dwm_round_ok(hwnd: int) -> bool:
    """请求 DWM 把窗口画成圆角；仅 Windows 11 生效（其余返回 False）。"""
    global _dwmapi
    try:
        if _dwmapi is None:
            _dwmapi = ctypes.windll.dwmapi
        pref = ctypes.c_int(_DWMWCP_ROUND)
        hr = _dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), _DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref),
        )
        return int(hr) == 0
    except Exception:  # noqa: BLE001
        return False


def _round_path(rect, radius):
    """(x, y, w, h) → 圆角矩形 GraphicsPath（四角 90° 圆弧）。"""
    x, y, w, h = rect
    d = max(2, 2 * radius)
    p = _GraphicsPath()
    p.AddArc(x, y, d, d, 180, 90)
    p.AddArc(x + w - d, y, d, d, 270, 90)
    p.AddArc(x + w - d, y + h - d, d, d, 0, 90)
    p.AddArc(x, y + h - d, d, d, 90, 90)
    p.CloseFigure()
    return p


def _apply_dropdown_shape(dd, scale: float) -> str:
    """给（已显示的）下拉窗口套圆角；返回实际生效模式 dwm / region。

    DWM 路径（Win11）：系统级圆角 + 原生投影，观感与系统菜单完全一致；
    Region 路径（Win10）：窗口像素按圆角路径裁剪，必须先关掉 WinForms 的
    方形投影（DropShadowEnabled），否则阴影四角会从裁剪缺口里露出来。
    """
    hwnd = int(dd.Handle.ToInt64())
    if _dwm_round_ok(hwnd):
        try:
            dd.Region = None
        except Exception:  # noqa: BLE001
            pass
        return "dwm"
    radius = max(4, round(8 * scale))
    cr = dd.ClientRectangle
    path = _round_path(
        (cr.X, cr.Y, cr.Width - 1, cr.Height - 1),
        min(radius, min(cr.Width, cr.Height) // 2),
    )
    dd.Region = _Region(path)
    return "region"


def _make_dropdown_shape_handler(dd, scale: float):
    """Opened 事件处理器：套圆角并把生效模式记到 Tag（供测试断言）。"""

    def on_opened(sender, e):
        try:
            dd.Tag = _apply_dropdown_shape(dd, scale)
        except Exception:  # noqa: BLE001
            log.exception("dropdown shape failed")

    return on_opened


# --------------------------------------------------------------------------- #
# 窗体钩子：WM_NCCALCSIZE 抹掉标题栏                                             #
# --------------------------------------------------------------------------- #
def _make_form_handler(hwnd: int):
    def handler(msg, wp, lp):
        if msg == WM_DESTROY:
            _unhook(hwnd)
            return None
        if msg != WM_NCCALCSIZE or not wp:
            return None
        # 客户区 = 整个窗口矩形；最大化时按边框厚度内缩，避免内容溢出屏幕
        if _user32.IsZoomed(hwnd):
            ins = _frame_thickness()
            if ins > 0 and lp:
                rect = ctypes.cast(lp, ctypes.POINTER(_RECT)).contents
                rect.left += ins
                rect.top += ins
                rect.right -= ins
                rect.bottom -= ins
        return 0

    return handler


# --------------------------------------------------------------------------- #
# 菜单条钩子：让被菜单条盖住的窗口边缘仍可缩放                                    #
# --------------------------------------------------------------------------- #
def _make_strip_handler(hwnd: int, caption_hit=None):
    def handler(msg, wp, lp):
        if msg == WM_DESTROY:
            _unhook(hwnd)
            return None
        if msg != WM_NCHITTEST:
            return None
        cursor = _POINT()
        if not _user32.GetCursorPos(ctypes.byref(cursor)):
            return None
        rect = _RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        m = _frame_thickness()
        x = cursor.x - rect.left
        y = cursor.y - rect.top
        width = rect.right - rect.left
        # 标题栏按钮区优先于缩放带：整块按钮可点（原生标题栏同样如此）
        if caption_hit is not None and caption_hit(x, y):
            return HTCLIENT
        if y < m:
            if x < m:
                return HTTOPLEFT
            if x >= width - m:
                return HTTOPRIGHT
            return HTTOP
        if x < m:
            return HTLEFT
        if x >= width - m:
            return HTRIGHT
        return None

    return handler


# --------------------------------------------------------------------------- #
# 菜单条行为：拖动 / 双击 / 右键系统菜单                                          #
# --------------------------------------------------------------------------- #
def _show_system_menu(form) -> None:
    try:
        hwnd = int(form.Handle.ToInt64())
        hmenu = _user32.GetSystemMenu(hwnd, False)
        if not hmenu:
            return
        pt = _POINT()
        if not _user32.GetCursorPos(ctypes.byref(pt)):
            return
        cmd = _user32.TrackPopupMenu(
            hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
            pt.x, pt.y, 0, hwnd, None,
        )
        if cmd:
            _user32.PostMessageW(hwnd, WM_SYSCOMMAND, cmd, 0)
    except Exception:  # noqa: BLE001
        log.exception("system menu failed")


def _attach_strip_behaviors(form, strip, caption_hit=None) -> None:
    mb = _WF.MouseButtons

    def on_mouse_down(sender, e):
        try:
            on_button = caption_hit is not None and caption_hit(e.X, e.Y)
            hit = strip.GetItemAt(e.X, e.Y)
            if e.Button == mb.Right:
                if hit is None and not on_button:
                    _show_system_menu(form)
                return
            if e.Button != mb.Left:
                return
            if hit is not None or on_button:
                return  # 菜单项 / 标题栏按钮：交给各自的处理逻辑
            hwnd = int(form.Handle.ToInt64())
            _user32.ReleaseCapture()
            msg = WM_NCLBUTTONDBLCLK if e.Clicks == 2 else WM_NCLBUTTONDOWN
            _user32.SendMessageW(hwnd, msg, HTCAPTION, 0)
        except Exception:  # noqa: BLE001
            log.exception("strip mouse handling failed")

    strip.MouseDown += on_mouse_down


# --------------------------------------------------------------------------- #
# 标题栏窗口按钮：直接绘制在菜单条右上角画布上                                    #
# --------------------------------------------------------------------------- #
# 第一性原理：窗口按钮是窗口 chrome，不是菜单项。ToolStripMenuItem 的右对齐
# 布局视觉序与集合序相反（曾出现 ✕□─ 颠倒）、尺寸/贴边不受布局器摆布，
# 因此改为：按钮矩形按 DPI 换算（46×36 逻辑像素，对齐 VS Code / Chrome /
# Win11 惯例，全高贴右上角），手动命中测试 + 自绘悬停态。
_CAPTION_H_DIP = 36     # 标题栏高度（逻辑像素，96 DPI 基准）
_BTN_W_DIP = 46         # 单个按钮宽度（逻辑像素）
_GLYPH_BOX_DIP = 10.0   # 符号盒边长（─ □ ✕ 共用）

_BTN_ORDER = ("min", "max", "close")


def _dpi_scale(form) -> float:
    """窗口当前 DPI / 96。进程为 system-DPI-aware，该值会话内恒定。"""
    try:
        hwnd = int(form.Handle.ToInt64())
        getter = getattr(_user32, "GetDpiForWindow", None)
        dpi = int(getter(hwnd)) if getter else 0
        if not dpi:
            dpi = int(_user32.GetDpiForSystem()) if hasattr(
                _user32, "GetDpiForSystem"
            ) else 96
        return max(dpi, 96) / 96.0
    except Exception:  # noqa: BLE001
        return 1.0


def _attach_window_buttons(form, strip, right_pad_fn=None):
    """在菜单条上绘制并接管 最小化/最大化/关闭 按钮；返回命中测试函数。

    ``right_pad_fn``：可选，无参可调用，返回账号区占用的额外右侧宽度
    （逻辑像素 0 基准的物理像素值）——条带右缩进必须为它让位，菜单项才
    不会钻到账号区/窗口按钮下面。
    """
    state = {"scale": _dpi_scale(form), "hover": None}

    def rects():
        """右上角三个按钮矩形（strip 客户区坐标，顺序即视觉序 ─ □ ✕）。"""
        s = state["scale"]
        h = max(1, round(_CAPTION_H_DIP * s))
        w = max(1, round(_BTN_W_DIP * s))
        right = strip.Width
        return {
            "min": (right - 3 * w, 0, right - 2 * w, h),
            "max": (right - 2 * w, 0, right - w, h),
            "close": (right - w, 0, right, h),
        }

    def hit(x, y):
        for name, (l, t, r, b) in rects().items():
            if l <= x < r and t <= y < b:
                return name
        return None

    def on_paint(sender, e):
        try:
            g = e.Graphics
            s = state["scale"]
            smax = form.WindowState == _WF.FormWindowState.Maximized
            for kind in _BTN_ORDER:
                l, t, r, b = rects()[kind]
                clip = e.ClipRectangle
                if (clip.Right <= l or clip.Left >= r
                        or clip.Bottom <= t or clip.Top >= b):
                    continue
                hot = state["hover"] == kind
                bg = None
                if kind == "close" and hot:
                    bg = _Color.FromArgb(0xE8, 0x11, 0x23)
                elif hot:
                    bg = _Color.FromArgb(0xE9, 0xE9, 0xE9)
                if bg is not None:
                    g.FillRectangle(_SolidBrush(bg), l, t, r - l, b - t)
                g.SmoothingMode = _SmoothingMode.AntiAlias
                fg = (
                    _Color.White
                    if (kind == "close" and hot)
                    else _Color.FromArgb(32, 32, 32)
                )
                pen = _Pen(fg, max(1.0, round(s * 2) / 2.0))
                cx, cy = (l + r) / 2.0, (t + b) / 2.0
                half = _GLYPH_BOX_DIP * s / 2.0
                if kind == "min":
                    g.DrawLine(pen, cx - half, cy, cx + half, cy)
                elif kind == "max":
                    side = half * 0.9
                    if smax:  # 还原态：前矩形 + 右上偏移的后矩形
                        off = max(2.0, 2.0 * s)
                        back_l, back_t = cx - side + off / 2, cy - side - off / 2
                        front_l, front_t = cx - side - off / 2, cy - side + off / 2
                        g.DrawRectangle(pen, int(back_l), int(back_t),
                                        int(side * 2), int(side * 2))
                        under = bg if bg is not None else _Color.White
                        g.FillRectangle(_SolidBrush(under), int(front_l),
                                        int(front_t), int(side * 2), int(side * 2))
                        g.DrawRectangle(pen, int(front_l), int(front_t),
                                        int(side * 2), int(side * 2))
                    else:
                        g.DrawRectangle(pen, int(cx - side), int(cy - side),
                                        int(side * 2), int(side * 2))
                else:  # close
                    g.DrawLine(pen, cx - half, cy - half, cx + half, cy + half)
                    g.DrawLine(pen, cx + half, cy - half, cx - half, cy + half)
        except Exception:  # noqa: BLE001
            log.exception("caption buttons paint failed")

    def on_click(sender, e):
        try:
            if e.Button != _WF.MouseButtons.Left:
                return
            kind = hit(e.X, e.Y)
            if kind is None:
                return
            _close_all_dropdowns()   # 点窗口按钮前先收起打开的下拉（与原生标题栏一致）
            if kind == "min":
                form.WindowState = _WF.FormWindowState.Minimized
            elif kind == "max":
                form.WindowState = (
                    _WF.FormWindowState.Normal
                    if form.WindowState == _WF.FormWindowState.Maximized
                    else _WF.FormWindowState.Maximized
                )
            elif kind == "close":
                form.Close()
        except Exception:  # noqa: BLE001
            log.exception("caption button action failed")

    def on_move(sender, e):
        try:
            kind = hit(e.X, e.Y)
            if kind != state["hover"]:
                state["hover"] = kind
                strip.Invalidate()  # MenuStrip 默认双缓冲，整条重绘无闪烁
        except Exception:  # noqa: BLE001
            pass

    def on_leave(sender, e):
        try:
            if state["hover"] is not None:
                state["hover"] = None
                strip.Invalidate()
        except Exception:  # noqa: BLE001
            pass

    strip.Paint += on_paint
    strip.MouseClick += on_click
    strip.MouseMove += on_move
    strip.MouseLeave += on_leave

    def sync(_s=None, _e=None):
        """窗口尺寸/状态变化时：按钮区随宽度重算 + 重绘（含最大化图标切换）。"""
        try:
            h = max(1, round(_CAPTION_H_DIP * state["scale"]))
            w = max(1, round(_BTN_W_DIP * state["scale"]))
            if abs(strip.Height - h) > 1:
                strip.Height = h
            extra = right_pad_fn() if right_pad_fn else 0
            strip.Padding = _WF.Padding(round(6 * state["scale"]), 0, 3 * w + extra, 0)
            strip.Invalidate()
        except Exception:  # noqa: BLE001
            pass

    sync()
    strip.Resize += sync
    return hit


# --------------------------------------------------------------------------- #
# 标题栏账号区（登录 / 账号 chip）                                               #
# --------------------------------------------------------------------------- #
# 第一性原理：与窗口按钮一样，账号入口是**窗口 chrome**，不是网页内容。
# 旧方案往页面注入悬浮按钮（inject.py，ADR-002），永远浮在 DeepTutor 内容
# 上方，还要靠定时器对抗 SPA 重建 DOM。改为画在标题栏上（窗口按钮左侧）：
# 不遮内容、不随页面刷新丢失、点击走原生回调，零页面注入。
# 状态模型由 titlebar_account.AccountStatusSync 推送（见 ADR-004）。
_ACCOUNT_CFG: dict = {}
_ACCOUNT_CHIP: "AccountChip | None" = None


def configure_account_chip(on_login=None, actions: dict | None = None) -> None:
    """登记标题栏账号区回调。窗口创建前由 main() 调用（仅 Windows 生效）。

      on_login : 未登录点击账号区时触发（发起 OAuth）
      actions  : {动作名: 无参可调用}，见 titlebar_account.MENU_ACTIONS
    """
    _ACCOUNT_CFG.clear()
    _ACCOUNT_CFG["on_login"] = on_login
    _ACCOUNT_CFG["actions"] = dict(actions or {})


def account_chip() -> "AccountChip | None":
    """当前窗口的账号区实例；条带未构建（非 Windows / 构建失败）时为 None。"""
    return _ACCOUNT_CHIP


def set_account_chip_hidden(hidden: bool) -> None:
    """显示/隐藏标题栏账号区（会话循环导航时调用；跨线程安全）。

    chip 未构建（非 Windows / 构建失败）时为 no-op。
    """
    chip = _ACCOUNT_CHIP
    if chip is not None:
        chip.set_hidden(hidden)


class AccountChip:
    """标题栏右侧的账号区：未登录「登录」直发 OAuth，已登录弹原生账号菜单。

    与窗口按钮同一套自绘哲学：矩形命中测试 + Paint 自绘，不占用 ToolStrip
    布局器（ToolStripMenuItem 右对齐的视觉序与集合序相反，曾踩坑）。模型
    来自后台同步线程（titlebar_account.AccountStatusSync），跨线程经
    BeginInvoke marshal 到 UI 线程。
    """

    _PAD_X_DIP = 12.0      # 文字两侧留白（逻辑像素）
    _MIN_W_DIP = 28.0      # 最小宽度（逻辑像素），避免「登录」两字太挤
    _CARET = " ▾"          # 有下拉菜单时附加的指示符
    _FG = None             # 延迟初始化的正文色

    def __init__(self, form, strip, on_login=None, actions: dict | None = None) -> None:
        self._form = form
        self._strip = strip
        self._on_login = on_login
        self._actions = dict(actions or {})
        self._font = _Font("Microsoft YaHei UI", 9.0)
        self._model: dict = {"label": "登录", "title": "登录 Tokengine 账号",
                             "logged_in": False, "menu": None}
        self._hover = False
        self._open = False
        self._closed_at = 0.0     # 上次下拉关闭时刻（monotonic 秒）——「再点一下=收起」守卫
        # 初始隐藏：splash 启动 / 登录门控页阶段不显示账号区（登录入口归
        # 门控页页面按钮，标题栏只做「已登录身份」指示）。进入应用页时由
        # 会话循环 set_hidden(False) 显示；注销回门控页时 set_hidden(True)。
        self._hidden = True
        self._width = 0
        self._dd = None
        strip.MouseMove += self._on_move
        strip.MouseLeave += self._on_leave
        strip.MouseClick += self._on_click
        strip.Paint += self._on_paint
        self._apply_metrics()

    # ---- 状态推送（跨线程入口）------------------------------------------ #
    def set_model(self, model: dict) -> None:
        """同步线程推送新模型；marshal 到 UI 线程执行。

        状态跃迁（登录/退出/切换账号）时顺手关掉还开着的旧下拉——它引用的
        是上一个状态的菜单项，继续展示会误导（旧页面菜单同样如此处理）。
        """
        def apply():
            self._model = dict(model or {})
            self.close_dropdown()
            self._dd = None
            self._apply_metrics()

        try:
            if self._strip.InvokeRequired:
                dlg = _Action(apply) if _Action is not None else _Func[_Type](apply)
                self._strip.BeginInvoke(dlg)
            else:
                apply()
        except Exception:  # noqa: BLE001
            log.exception("account chip update failed")

    def close_dropdown(self) -> None:
        """收起当前下拉（幂等；无下拉或已关闭时为空操作）。"""
        dd = self._dd
        if dd is not None:
            try:
                dd.Close()
            except Exception:  # noqa: BLE001
                pass

    def set_hidden(self, hidden: bool) -> None:
        """显示/隐藏账号区（跨线程入口；隐藏时不占宽度、不命中、不绘制）。

        时序语义（ADR-004）：splash 启动与登录门控页阶段隐藏——登录入口
        由门控页页面按钮承担；仅「已登录进入应用页」时显示用户名。
        """
        def apply():
            self._hidden = bool(hidden)
            if self._hidden:
                self.close_dropdown()
                self._dd = None
            self._apply_metrics()

        try:
            if self._strip.InvokeRequired:
                dlg = _Action(apply) if _Action is not None else _Func[_Type](apply)
                self._strip.BeginInvoke(dlg)
            else:
                apply()
        except Exception:  # noqa: BLE001
            log.exception("account chip visibility failed")

    # ---- 几何 ----------------------------------------------------------- #
    def _scale(self) -> float:
        return _dpi_scale(self._form)

    def _btn_w(self, s: float | None = None) -> int:
        s = s if s is not None else self._scale()
        return max(1, round(_BTN_W_DIP * s))

    def _display_text(self) -> str:
        label = str(self._model.get("label") or "")
        return label + self._CARET if self._model.get("menu") else label

    def _apply_metrics(self) -> None:
        """按当前文案重算宽度，并让条带右缩进为账号区 + 窗口按钮让位。"""
        try:
            s = self._scale()
            if getattr(self, "_hidden", False):
                self._width = 0
            else:
                size = _WF.TextRenderer.MeasureText(
                    self._display_text(), self._font)
                self._width = max(
                    round(self._MIN_W_DIP * s),
                    int(size.Width) + round(2 * self._PAD_X_DIP * s),
                )
            extra = self.extra_right_padding()
            w = self._btn_w(s)
            self._strip.Padding = _WF.Padding(round(6 * s), 0, 3 * w + extra, 0)
            self._strip.Invalidate()
        except Exception:  # noqa: BLE001
            log.exception("account chip metrics failed")

    def extra_right_padding(self) -> int:
        """账号区占用的右侧宽度（含与窗口按钮之间 2px 呼吸缝）。"""
        return self._width + round(2 * self._scale())

    def rect(self):
        """账号区矩形（strip 客户区坐标）；宽度未就绪或隐藏时返回 None。"""
        if getattr(self, "_hidden", False) or self._width <= 0:
            return None
        s = self._scale()
        h = max(1, round(_CAPTION_H_DIP * s))
        right = self._strip.Width - 3 * self._btn_w(s)
        return (right - self._width, 0, right, h)

    def hit(self, x, y) -> bool:
        r = self.rect()
        return bool(r and r[0] <= x < r[2] and r[1] <= y < r[3])

    # ---- 交互 ------------------------------------------------------------ #
    def _on_move(self, sender, e) -> None:
        try:
            hot = self.hit(e.X, e.Y)
            if hot != self._hover:
                self._hover = hot
                self._strip.Invalidate()
        except Exception:  # noqa: BLE001
            pass

    def _on_leave(self, sender, e) -> None:
        try:
            if self._hover:
                self._hover = False
                self._strip.Invalidate()
        except Exception:  # noqa: BLE001
            pass

    def _on_click(self, sender, e) -> None:
        try:
            if e.Button != _WF.MouseButtons.Left or not self.hit(e.X, e.Y):
                return
            menu = self._model.get("menu")
            if menu and (menu.get("items") or menu.get("header")):
                if self._dd is not None and self._dd.Visible:
                    self._dd.Close()   # 再点一下账号区 = 收起（兜底，AutoClose 通常已先行关闭）
                    return
                if time.monotonic() - self._closed_at < 0.45:
                    # 本次点击刚把下拉关掉（ContextMenuStrip 的外部点击关闭
                    # 发生在 MouseUp 之前）→ 视为「收起」，不再立刻重开
                    return
                self._open_dropdown()
            else:
                self._run(self._on_login)
        except Exception:  # noqa: BLE001
            log.exception("account chip click failed")

    def _run(self, fn) -> None:
        """动作在独立线程执行（回调里可能开浏览器/弹框/刷新页面，别占 UI 线程）。"""
        if fn is None:
            return

        def wrap():
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("account chip action failed")

        threading.Thread(target=wrap, daemon=True).start()

    # ---- 原生下拉菜单 ----------------------------------------------------- #
    def _open_dropdown(self) -> None:
        menu = self._model.get("menu") or {}
        dd = _WF.ContextMenuStrip()
        dd.Font = self._font
        dd.ShowImageMargin = False
        header = menu.get("header") or {}
        head_text = " · ".join(
            p for p in (str(header.get("title") or ""), str(header.get("sub") or "")) if p
        )
        if head_text:
            head = _WF.ToolStripMenuItem(head_text)
            head.Enabled = False
            dd.Items.Add(head)
            dd.Items.Add(_WF.ToolStripSeparator())
        for it in menu.get("items") or []:
            if not isinstance(it, dict):
                continue
            if it.get("type") == "sep":
                dd.Items.Add(_WF.ToolStripSeparator())
                continue
            action = str(it.get("action") or "")
            item = _WF.ToolStripMenuItem(str(it.get("label") or action))
            if it.get("danger"):
                item.ForeColor = _Color.FromArgb(0xD9, 0x30, 0x25)
            handler = self._actions.get(action)
            if handler is not None:
                item.Click += (lambda _s, _e, fn=handler: self._run(fn))
            else:
                item.Enabled = False
            dd.Items.Add(item)
        dd.Closed += self._on_dd_closed
        dd.Opened += self._on_dd_opened
        dd.DropShadowEnabled = False   # Win10 Region 路径必需；Win11 由 DWM 出原生投影
        self._dd = dd
        r = self.rect()
        self._open = True
        self._strip.Invalidate()
        # 菜单展开在账号区正下方（贴标题栏下缘），左缘对齐账号区
        dd.Show(self._strip, _Point(r[0], self._strip.Height))

    def _on_dd_opened(self, sender, e) -> None:
        try:
            self._open = True
            dd = self._dd
            if dd is not None:
                dd.Tag = _apply_dropdown_shape(dd, self._scale())
            self._strip.Invalidate()
        except Exception:  # noqa: BLE001
            log.exception("account dropdown shape failed")

    def _on_dd_closed(self, sender, e) -> None:
        self._open = False
        self._closed_at = time.monotonic()
        try:
            self._strip.Invalidate()
        except Exception:  # noqa: BLE001
            pass

    # ---- 自绘 -------------------------------------------------------------- #
    def _on_paint(self, sender, e) -> None:
        try:
            r = self.rect()
            if r is None:
                return
            clip = e.ClipRectangle
            if (clip.Right <= r[0] or clip.Left >= r[2]
                    or clip.Bottom <= r[1] or clip.Top >= r[3]):
                return
            g = e.Graphics
            if self._hover or self._open:
                s = self._scale()
                # 内缩药丸形高亮（与菜单条圆角项一致），命中区仍是完整矩形
                x = r[0] + round(1 * s)
                y = r[1] + round(3 * s)
                w = (r[2] - r[0]) - round(2 * s)
                h = (r[3] - r[1]) - round(6 * s)
                if w > 2 and h > 2:
                    path = _round_path(
                        (x, y, w, h), min(max(2, round(5 * s)), h // 2)
                    )
                    g.SmoothingMode = _SmoothingMode.AntiAlias
                    g.FillPath(
                        _SolidBrush(_Color.FromArgb(0xE9, 0xE9, 0xE9)), path
                    )
            text = self._display_text()
            size = _WF.TextRenderer.MeasureText(text, self._font)
            tx = r[0] + ((r[2] - r[0]) - int(size.Width)) // 2
            ty = r[1] + ((r[3] - r[1]) - int(size.Height)) // 2
            if AccountChip._FG is None:
                AccountChip._FG = _Color.FromArgb(17, 17, 17)
            _WF.TextRenderer.DrawText(
                g, text, self._font, _Point(max(r[0], tx), max(r[1], ty)),
                AccountChip._FG,
            )
        except Exception:  # noqa: BLE001
            log.exception("account chip paint failed")


# --------------------------------------------------------------------------- #
# 共享状态                                                                      #
# --------------------------------------------------------------------------- #
_CURRENT_STRIP = None       # 当前菜单条（WebView2 获焦收起下拉时用）

# 注（2026-09-24）：曾尝试「子类化 ToolStripProfessionalRenderer」实现菜单项
# 圆角悬停，但本机 pythonnet 3.1.0 的 .NET 子类化失效——连
# ``class C(Control): pass`` 的 CLR 运行时类型都是基类本身，虚方法重写
# 永远不会被调用（当日日志有探针记录），该方案已删除。
# 现状：下拉面板圆角由 DWM（Win11）/ Region（Win10）完成；菜单项悬停保持
# 原生方形高亮——与 VS Code / Chrome「圆角面板 + 方形 hover」惯例一致；
# 账号区悬停由 AccountChip 自绘圆角药丸（不经子类化，Paint 事件自绘）。


# --------------------------------------------------------------------------- #
# 点页面收回下拉：WebView2 获焦兜底钩子                                          #
# --------------------------------------------------------------------------- #
def _close_all_dropdowns() -> None:
    """收起账号区下拉 + 菜单条上所有打开的下拉（幂等，异常不外抛）。"""
    try:
        chip = _ACCOUNT_CHIP
        if chip is not None:
            chip.close_dropdown()
        strip = _CURRENT_STRIP
        if strip is not None:
            for it in strip.Items:
                dd = getattr(it, "DropDown", None)
                if dd is not None and dd.Visible:
                    dd.Close()
    except Exception:  # noqa: BLE001
        log.exception("close dropdowns failed")


def _attach_webview_focus_hook(form) -> None:
    """WebView2 获得焦点（= 用户点进页面）时收起所有打开的下拉。

    菜单的原生「点外部收回」走模态菜单过滤器（同线程消息泵），通常有效；
    但 WebView2 的输入由 Chromium 子窗口自管，是「菜单点开 → 点页面 →
    不收回」最可疑的通路。获焦钩子作兜底：正常时它只是冗余第二保险，
    异常时（Chromium 吃掉点击/抢走捕获）它是唯一通路。
    """
    def on_focus(sender, e):
        _close_all_dropdowns()

    def wire(ctrl) -> bool:
        try:
            if "WebView2" in str(ctrl.GetType().Name):
                ctrl.GotFocus += on_focus
                log.info("chrome: webview focus hook attached")
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def scan(container):
        try:
            for c in container.Controls:
                if not wire(c):
                    scan(c)
        except Exception:  # noqa: BLE001
            pass

    def on_added(sender, e):
        try:
            if not wire(e.Control):
                scan(e.Control)
        except Exception:  # noqa: BLE001
            pass

    try:
        form.ControlAdded += on_added
        form.Shown += lambda s, e: scan(form)
    except Exception:  # noqa: BLE001
        log.exception("webview focus hook wiring failed")


# --------------------------------------------------------------------------- #
# 「点外部收回」消息过滤器：任何鼠标按下落在打开的下拉之外 → 收起                 #
# --------------------------------------------------------------------------- #
# 第一性原理：AutoClose 依赖下拉自己的捕获/菜单模式；WebView2 的输入由
# Chromium 子窗口自管，「菜单点开 → 点页面」可能绕开那条通路（实测复现过
# 编程式打开时外部点击不收回）。消息过滤器挂在 UI 线程消息泵上，**所有**
# 派发消息都会路过（含被捕获的、含 WebView2 子窗口的），在这里判断与收起
# 是确定性兜底。只处理按钮按下消息，其余秒回，不增加每消息开销。
_OUTSIDE_FILTER = None


def _any_dropdown_visible() -> bool:
    chip = _ACCOUNT_CHIP
    if chip is not None and chip._dd is not None and chip._dd.Visible:
        return True
    strip = _CURRENT_STRIP
    if strip is not None:
        for it in strip.Items:
            dd = getattr(it, "DropDown", None)
            if dd is not None and dd.Visible:
                return True
    return False


def _net_rect_has(r, x: int, y: int) -> bool:
    """判定屏幕坐标 (x, y) 是否落在 .NET Rectangle 内。"""
    try:
        return bool(r.Left <= x < r.Right and r.Top <= y < r.Bottom)
    except Exception:  # noqa: BLE001
        return False


def _close_dropdowns_outside(x: int, y: int) -> None:
    """收起所有「点击点不在其内部/其触发区」的打开下拉。

    豁免两处，把「再点一下」的收合交给各自的原生逻辑，避免双击竞争：
      * 打开中的菜单项标题矩形（MenuStrip 原生 toggle = 收起）；
      * 账号区矩形（chip 内部有 toggle 守卫）。
    """
    strip = _CURRENT_STRIP
    chip = _ACCOUNT_CHIP
    try:
        if strip is not None:
            for it in strip.Items:
                dd = getattr(it, "DropDown", None)
                if dd is None or not dd.Visible:
                    continue
                if _net_rect_has(dd.Bounds, x, y):
                    continue
                try:
                    if _net_rect_has(strip.RectangleToScreen(it.Bounds), x, y):
                        continue
                except Exception:  # noqa: BLE001
                    pass
                dd.Close()
        if chip is not None and chip._dd is not None and chip._dd.Visible:
            if _net_rect_has(chip._dd.Bounds, x, y):
                return
            r = chip.rect()
            if r is not None:
                sp = strip.PointToClient(_Point(x, y))
                if chip.hit(sp.X, sp.Y):
                    return
            chip.close_dropdown()
    except Exception:  # noqa: BLE001
        log.exception("outside-click close failed")


def _install_outside_click_filter() -> None:
    """UI 线程安装「点外部收回」看门狗（_build_menu_strip 时调用，幂等）。

    实现选型：本想用 Application.AddMessageFilter(IMessageFilter)，但
    pythonnet 在此环境实现 .NET 接口不可用（实测实例化报 TypeError:
    interface takes exactly one argument）。改用 UI 线程 Timer 轮询：

      * 每 60ms 查 GetAsyncKeyState(VK_LBUTTON)：0x8000 位 = 此刻按着，
        0x0001 位 = 自上次查询以来按下过——再快的点击也不会漏；
      * 有下拉打开且按下点在所有下拉及其触发区之外 → 收起。

    跑在 UI 线程（WinForms Timer），无跨线程封送问题；WebView2 子窗口
    的点击同样逃不过（光标位置是全局的）。
    """
    global _OUTSIDE_FILTER
    if _OUTSIDE_FILTER is not None:
        return
    try:
        from System.Windows.Forms import Timer

        timer = Timer()
        timer.Interval = 60

        def on_tick(s, e):
            try:
                vk = int(_user32.GetAsyncKeyState(0x01)) & 0xFFFF  # VK_LBUTTON
                if not (vk & 0x8000) and not (vk & 0x0001):
                    return
                if not _any_dropdown_visible():
                    return
                pt = _POINT()
                if _user32.GetCursorPos(ctypes.byref(pt)):
                    _close_dropdowns_outside(pt.x, pt.y)
            except Exception:  # noqa: BLE001
                log.exception("outside-click watchdog failed")

        timer.Tick += on_tick
        timer.Start()
        _OUTSIDE_FILTER = timer
        log.info("chrome: outside-click watchdog installed")
    except Exception:  # noqa: BLE001
        log.exception("outside-click watchdog install failed")


# --------------------------------------------------------------------------- #
# 菜单条构建                                                                    #
# --------------------------------------------------------------------------- #
def _convert_entry(entry):
    """pywebview Menu 项 → WinForms ToolStripMenuItem（递归）。"""
    if entry is None or isinstance(entry, MenuSeparator):
        return _WF.ToolStripSeparator()
    if isinstance(entry, MenuAction):
        item = _WF.ToolStripMenuItem(entry.title)
        item.Click += (
            lambda _s, _e, fn=entry.function: threading.Thread(
                target=fn, daemon=True
            ).start()
        )
        return item
    if isinstance(entry, Menu):
        item = _WF.ToolStripMenuItem(entry.title)
        for child in entry.items:
            converted = _convert_entry(child)
            if converted is not None:
                item.DropDownItems.Add(converted)
        return item
    return _WF.ToolStripMenuItem(str(entry))


def _chip_right_pad() -> int:
    """账号区当前占用的右侧宽度（供条带右缩进让位；未建区时为 0）。"""
    return _ACCOUNT_CHIP.extra_right_padding() if _ACCOUNT_CHIP else 0


def _build_menu_strip(form, menu_list):
    scale = _dpi_scale(form)
    cap_h = max(1, round(_CAPTION_H_DIP * scale))
    btn_w = max(1, round(_BTN_W_DIP * scale))

    strip = _WF.MenuStrip()
    strip.Dock = _WF.DockStyle.Top
    strip.GripStyle = _WF.ToolStripGripStyle.Hidden
    # AutoSize=True 时条带高度被字体行高决定（约 30 物理像素），视觉过窄；
    # 显式按 DPI 设为标准标题栏高度（Win11 风 36 逻辑像素）。
    strip.AutoSize = False
    strip.Height = cap_h
    strip.ShowItemToolTips = False
    strip.BackColor = _Color.White
    strip.Font = _Font("Microsoft YaHei UI", 9.0)
    # 右侧让出 3 个按钮宽度，菜单项永远不会钻到按钮下面
    strip.Padding = _WF.Padding(round(6 * scale), 0, 3 * btn_w, 0)

    for entry in menu_list or []:
        converted = _convert_entry(entry)
        if converted is not None:
            # 锚定上下边缘 → 条带加高后菜单文字垂直居中，而不是贴顶
            try:
                converted.Anchor = (
                    _WF.AnchorStyles.Top | _WF.AnchorStyles.Bottom
                )
            except Exception:  # noqa: BLE001  布局兜底不拦构建
                pass
            strip.Items.Add(converted)
        try:
            dd = getattr(converted, "DropDown", None)
            if dd is not None:
                # 样式与账号下拉（ContextMenuStrip）保持一致：
                # 无图标菜单关掉左侧空白图列；DropShadowEnabled=False 是
                # Win10 Region 圆角路径的前置（方形阴影四角会外露）；
                # 圆角由 Opened 事件统一挂 _apply_dropdown_shape。
                dd.ShowImageMargin = False
                dd.DropShadowEnabled = False
                dd.Opened += _make_dropdown_shape_handler(dd, scale)
        except Exception:  # noqa: BLE001  圆角失败退回原生方角，不拦构建
            log.exception("dropdown rounding wire failed")

    # 绘制顺序 = 订阅顺序：白底 → 窗口按钮 → 底部分隔线（最后覆盖保证连贯）
    brush = _SolidBrush(_Color.White)

    def on_strip_paint(sender, e):
        try:
            e.Graphics.FillRectangle(brush, e.ClipRectangle)
        except Exception:  # noqa: BLE001
            pass

    strip.Paint += on_strip_paint

    caption_hit = _attach_window_buttons(form, strip, right_pad_fn=_chip_right_pad)

    # 标题栏账号区（窗口按钮左侧）：点击路由在 AccountChip 内部——
    # 未登录直发 OAuth，已登录弹原生账号菜单。模型由后台线程推送。
    global _ACCOUNT_CHIP
    _ACCOUNT_CHIP = AccountChip(
        form, strip,
        on_login=_ACCOUNT_CFG.get("on_login"),
        actions=_ACCOUNT_CFG.get("actions") or {},
    )
    # 组合「窗口按钮/标题拖拽」与「账号区」两套命中判定。注意必须先把原
    # 函数捕获到独立名字——若 lambda 直接引用 caption_hit，解析到的是它
    # 自己，鼠标划过标题栏（WM_NCHITTEST）即无限自递归（实测打满栈）。
    _btn_caption_hit = caption_hit
    _chip_ref = _ACCOUNT_CHIP
    caption_hit = (lambda x, y: (
        bool(_btn_caption_hit and _btn_caption_hit(x, y))
        or bool(_chip_ref and _chip_ref.hit(x, y))))
    _attach_strip_behaviors(form, strip, caption_hit)

    line_brush = _SolidBrush(_Color.FromArgb(0xE0, 0xE0, 0xE0))

    def on_border_paint(sender, e):
        try:
            g = e.Graphics
            y = strip.Height - 1
            g.FillRectangle(
                line_brush, e.ClipRectangle.Left, y, e.ClipRectangle.Width, 1
            )
        except Exception:  # noqa: BLE001
            pass

    strip.Paint += on_border_paint

    form.Controls.Add(strip)

    # WebView2 获焦（点进页面）时收起所有打开的下拉——「点外部收回」兜底
    global _CURRENT_STRIP
    _CURRENT_STRIP = strip
    _attach_webview_focus_hook(form)
    _install_outside_click_filter()

    # 菜单条自己的句柄钩子：顶边/左右边缘保持系统缩放（按钮区除外）
    try:
        hwnd_strip = int(strip.Handle.ToInt64())
        _install_hook(
            hwnd_strip, _make_strip_handler(hwnd_strip, caption_hit)
        )
    except Exception:  # noqa: BLE001
        log.exception("strip hook failed (top-edge resize may be limited)")

    return strip


def install_windows_shell_menu() -> bool:
    """替换 pywebview 的菜单安装路径，并给窗体装自定义标题栏钩子。

    Returns whether the backend was patched. On non-Windows platforms this is
    an inert no-op; the shell currently targets Windows.
    """
    global _WF, _Color, _Font, _Pen, _SolidBrush, _Size, _SmoothingMode, _Func, _Type
    global _Point, _Action, _GraphicsPath, _LinearGradientBrush, _Region
    if os.name != "nt":
        return False

    try:
        import webview.platforms.winforms as winforms
    except Exception:  # noqa: BLE001 - shell menu remains optional
        log.warning("WinForms backend unavailable; using pywebview default menu")
        return False

    # pythonnet 的 System 命名空间此刻已由 winforms 模块激活，可安全导入
    from System import Func as _Func_tmp, Type as _Type_tmp
    from System import Action as _Action_tmp
    from System.Drawing import (  # noqa: F401
        Color as _Color_tmp,
        Font as _Font_tmp,
        Pen as _Pen_tmp,
        Point as _Point_tmp,
        SolidBrush as _SolidBrush_tmp,
        Size as _Size_tmp,
        Region as _Region_tmp,
    )
    from System.Drawing.Drawing2D import SmoothingMode as _SmoothingMode_tmp
    from System.Drawing.Drawing2D import GraphicsPath as _GP_tmp
    from System.Drawing.Drawing2D import LinearGradientBrush as _LGB_tmp

    _WF = winforms.WinForms
    _Func, _Type = _Func_tmp, _Type_tmp
    _Action = _Action_tmp
    _Color, _Font, _Point = _Color_tmp, _Font_tmp, _Point_tmp
    _Pen, _SolidBrush, _Size = _Pen_tmp, _SolidBrush_tmp, _Size_tmp
    _SmoothingMode = _SmoothingMode_tmp
    _GraphicsPath, _LinearGradientBrush, _Region = _GP_tmp, _LGB_tmp, _Region_tmp

    def set_window_menu(self, menu_list):
        def _build():
            try:
                strip = _build_menu_strip(self, menu_list)
                log.info(
                    "chrome: menu strip ready (%d top items + window buttons)",
                    len(menu_list or []),
                )
            except Exception:  # noqa: BLE001
                log.exception("menu strip build failed")

        if self.InvokeRequired:
            self.Invoke(_Func[_Type](_build))
        else:
            _build()

    original_form_init = winforms.BrowserView.BrowserForm.__init__

    def browser_form_init(form, *args, **kwargs):
        original_form_init(form, *args, **kwargs)
        try:
            form.ShowIcon = False
            if not form.Text:
                form.Text = "EduBuddy"  # Alt-Tab / 任务栏悬停显示
            hwnd = int(form.Handle.ToInt64())
            _install_hook(hwnd, _make_form_handler(hwnd))
            # 窗口可见前重算非客户区 → 标题栏无声消失，无闪烁
            _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FRAME_RECALC)
            log.info("chrome: custom frame installed (hwnd=0x%X)", hwnd)
        except Exception:  # noqa: BLE001  失败则退回系统标题栏，不影响启动
            log.exception("custom frame install failed; system title bar kept")

    winforms.BrowserView.BrowserForm.__init__ = browser_form_init
    winforms.BrowserView.BrowserForm.set_window_menu = set_window_menu
    return True
