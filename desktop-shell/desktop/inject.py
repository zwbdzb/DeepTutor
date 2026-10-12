"""页面注入器：toast 浮层（全平台）+ macOS 壳原生账号 chip（仅 darwin）。

历史说明：本模块曾负责往页面注入左下角「登录/账号」悬浮按钮与自绘下拉菜单
（隐藏 input 事件槽 + Python ``evaluate_js`` 轮询通道，见 ADR-002）。
2026-09-23 起账号入口迁到**原生标题栏**（ADR-004），点击与菜单改由
WinForms 直接回调（``desktop/native_menu_backend.py`` 的 ``AccountChip``），
「按钮 + 菜单 + 事件槽轮询」整套页面注入随之拆除。

2026-10 起 macOS 用**壳原生标题栏注入**（ADR-003）：
Windows 账号入口仍用原生标题栏 ``AccountChip``，mac 上没有 WinForms 标题
栏条带。直接改屏幕顶部菜单栏的「账号」菜单标题（依赖 AppKit）虽可打包，
但位置与 Windows 账号区完全不同（屏幕顶部 vs 窗口内右上角），且
``import AppKit`` + ``AppHelper.callAfter`` 在某些时机下静默失败。

改用 ``NSWindow.titlebarAccessoryViewController``（macOS 10.10+）：
在壳原生红绿灯**左边**加自定义 ``NSView`` + ``NSTextField`` 显示账号
文案，位置与 Windows 账号区几乎一致（标题栏右侧）。pywebview 的 Cocoa
后端已在顶层 ``import AppKit``（见 webview/platforms/cocoa.py），打包
必然含 pyobjc，无需额外 hiddenimports。

保留 toast 的原因：菜单动作（刷新模型 / 复制地址等）需要一条轻量操作反馈，
而原生壳里最贴近旧体验的就是页面底部的短暂浮层。SPA 整页刷新（登录成功、
刷新模型后）会重建 DOM 把 toast 元素清掉，所以仍用「周期 ensure」的老办法
把它补回来。

通道本身（evaluate_js）不依赖 js_api 桥，跨 pywebview 版本稳定，已在
tools/_tmp_verify/probe_js_bridge.py 里单独验证过。
"""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("dt.inject")

# --------------------------------------------------------------------------- #
# toast 浮层（全平台）------------------------------------------------------ #
# --------------------------------------------------------------------------- #
TOAST_ID = "edubuddy-toast"

# 确保 toast 浮层元素存在（幂等；SPA 路由/整页刷新后由定时器补挂）。
TOAST_ENSURE_JS = """
(function () {
  if (!document.body) { return 'no-body'; }
  var t = document.getElementById('%(id)s');
  var result = 'exists';
  if (!t) {
    t = document.createElement('div');
    t.id = '%(id)s';
    t.textContent = '';
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);' +
      'background:rgba(40,40,40,0.88);color:#fff;padding:8px 18px;border-radius:8px;' +
      'font:500 14px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;' +
      'z-index:2147483000;pointer-events:none;opacity:0;transition:opacity .2s ease;';
    document.body.appendChild(t);
    result = 'created';
  }
  return result;
})();
"""

TOAST_SHOW_JS = """
(function () {
  var t = document.getElementById('%(id)s');
  if (!t) return;
  t.textContent = %(msg)s;
  t.style.opacity = '1';
  clearTimeout(window.__edubuddyToastTimer);
  window.__edubuddyToastTimer = setTimeout(function () {
    t.style.opacity = '0';
  }, %(duration)d);
})();
"""


class ToastInjector:
    """全平台页面底部 toast 注入器（仅 DOM 维持，不负责触发时机）。"""

    def __init__(self, window, expect_url: str | None = None,
                 interval: float = 3.0) -> None:
        self._w = window
        self._expect_url = expect_url
        self._interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            if self._on_app_page():
                try:
                    self._w.evaluate_js(TOAST_ENSURE_JS)
                except Exception:  # noqa: BLE001  页面未就绪，下一轮再试
                    pass
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()

    def show(self, message: str, duration_ms: int = 2000) -> None:
        try:
            self._w.evaluate_js(
                TOAST_SHOW_JS % {
                    "id": TOAST_ID,
                    "msg": json.dumps(message, ensure_ascii=False),
                    "duration": duration_ms,
                }
            )
        except Exception:  # noqa: BLE001
            log.warning("toast 显示失败")

    def _on_app_page(self) -> bool:
        if not self._expect_url:
            return True
        try:
            cur = self._w.get_current_url() or ""
        except Exception:  # noqa: BLE001
            return False
        return cur.startswith(self._expect_url)


# --------------------------------------------------------------------------- #
# macOS 壳原生账号 chip（仅 darwin）---------------------------------------- #
# --------------------------------------------------------------------------- #
# 壳原生方案：用 NSWindow.titlebarAccessoryViewController 在红绿灯左边加
# 自定义 NSView + NSTextField 显示账号文案。位置和 Windows 账号区几乎一致。
# 点击 chip 弹出 NSPopover 显示下拉菜单（壳原生，不走页面内注入）。
#
# pywebview 的 Cocoa 后端已在顶层 import AppKit / Foundation / WebKit /
# objc / PyObjCTools.AppHelper（见 webview/platforms/cocoa.py 第 11-15
# 行），所以打包后 pyobjc 必然存在，这里只做延迟 import 避免非 mac 环境
# 跑起来崩掉。


class AccountChipInjector:
    """macOS 壳原生标题栏账号 chip（titlebarAccessoryViewController）。

    仅 darwin 用（Windows 账号入口在原生标题栏 ``AccountChip``）。
    模型由 :class:`desktop.titlebar_account.AccountStatusSync` 通过
    :meth:`set_model` 推送（``titlebar_account.chip_model()`` 的输出），
    文案/菜单结构与 Windows 账号区逐字一致。点击动作分发到
    ``main._chip_actions`` 的同一套回调。

    所有 AppKit UI 操作必须在主线程，通过 ``AppHelper.callAfter`` 调度。

    参数
      window      : pywebview Window（mac 上 window.native 是 NSWindow）
      on_login    : 无参可调用；chip 被点击且无菜单（未登录/已配置令牌）时触发
      menu_actions: 可选；{动作名: 无参可调} 分发表，供下拉菜单项分发。
                    键见 titlebar_account.MENU_ACTIONS
    """

    def __init__(self, window, on_login, menu_actions: dict | None = None) -> None:
        self._w = window
        self._on_login = on_login
        self._menu_actions = dict(menu_actions or {})
        self._model: dict | None = None

        # 延迟初始化：等 webview.start 事件循环跑起来后在主线程创建 AppKit 对象
        self._initialized = threading.Event()
        self._chip_label = None  # NSTextField
        self._popover = None    # NSPopover
        self._menu_populating = False

    # ---- 对外 ---------------------------------------------------------- #
    def set_model(self, model: dict) -> None:
        """AccountStatusSync 调用：把 chip 模型推给壳原生标题栏。"""
        self._model = model or None
        self._update_chip()

    def run(self) -> None:
        """阻塞壳原生 UI 初始化（必须在主线程）。

        在 bootstrap 线程调用，内部通过 AppHelper.callAfter 把实际 UI 创建
        丢回主线程，然后等 _initialized 事件。webview.start 事件循环起来
        后 callAfter 才能生效。
        """
        try:
            import AppKit  # type: ignore
            from PyObjCTools import AppHelper  # type: ignore
        except Exception:  # noqa: BLE001
            log.exception("mac 壳原生账号 chip 无法 import AppKit")
            return

        def _do_init() -> None:
            try:
                self._init_chip()
                self._initialized.set()
                log.info("mac 壳原生账号 chip 已挂载到标题栏")
                # 推送最新模型（init 时可能已有模型）
                self._update_chip()
            except Exception:  # noqa: BLE001
                log.exception("mac 壳原生账号 chip 初始化失败")
                self._initialized.set()  # 让 run() 不要卡死

        AppHelper.callAfter(_do_init)
        # 等最多 5s（主线程事件循环应该已经起来了）
        if not self._initialized.wait(5.0):
            log.warning("mac 壳原生账号 chip 初始化超时")

    # ---- 壳原生 UI 初始化（主线程） -------------------------------------- #
    def _init_chip(self) -> None:
        """创建 titlebarAccessoryViewController + chip 视图 + NSPopover 菜单。"""
        import AppKit  # type: ignore

        ns_window = self._w.native  # NSWindow（pywebview.cocoa.py 第 595 行赋值）
        if ns_window is None:
            log.warning("mac 壳原生账号 chip：window.native 为 None")
            return

        # ---- 创建 chip NSView ----
        chip_view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 160, 28))
        chip_view.setWantsLayer_(True)
        chip_view.layer().setCornerRadius_(6)
        chip_view.layer().setBackgroundColor_(
            AppKit.NSColor.colorWithRed_green_blue_alpha_(0.96, 0.96, 0.96, 0.85).CGColor()
        )

        # 账号文案 NSTextField
        label = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(8, 6, 144, 16))
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setBordered_(False)
        label.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        label.setTextColor_(AppKit.NSColor.colorWithRed_green_blue_alpha_(0.15, 0.15, 0.15, 1.0))
        label.setAlignment_(AppKit.NSTextAlignmentLeft)
        label.setStringValue_("登录")
        chip_view.addSubview_(label)
        self._chip_label = label

        # 下拉箭头
        arrow = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(148, 6, 12, 16))
        arrow.setBezeled_(False)
        arrow.setDrawsBackground_(False)
        arrow.setEditable_(False)
        arrow.setSelectable_(False)
        arrow.setBordered_(False)
        arrow.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        arrow.setTextColor_(AppKit.NSColor.colorWithRed_green_blue_alpha_(0.5, 0.5, 0.5, 1.0))
        arrow.setStringValue_("▾")
        chip_view.addSubview_(arrow)

        # 点击手势
        click = AppKit.NSClickGestureRecognizer.alloc().initWithTarget_action_(self, "_chip_clicked:")
        chip_view.addGestureRecognizer_(click)

        # ---- 创建 NSViewController ----
        vc = AppKit.NSViewController.alloc().init()
        vc.setView_(chip_view)
        ns_window.setTitlebarAccessoryViewController_(vc)
        ns_window.titlebarAccessoryViewController().setLayoutAttribute_(
            AppKit.NSLayoutAttributeLeading
        )

        # ---- 创建 NSPopover 下拉菜单 ----
        self._popover = AppKit.NSPopover.alloc().init()
        self._popover.setBehaviors_(AppKit.NSPopoverBehaviorTransient)
        self._popover.setAnimates_(True)

        # popover 关闭时清理
        self._popover.setDelegate_(self)

    # ---- AppKit 回调（主线程） ------------------------------------------ #
    def _chip_clicked_(self, sender) -> None:  # noqa: N802  (pyobjc selector 命名)
        """点击 chip：有菜单则 toggle popover，无菜单则发登录。"""
        import AppKit  # type: ignore

        model = self._model or {}
        menu = model.get("menu") or []

        if self._popover.isShown():
            self._popover.performClose_(sender)
            return

        if not menu:
            # 未登录：发起 OAuth
            self._safe_login()
            return

        # 构建 popover 内容 NSView + NSStackView + NSButtons
        self._populate_popover(menu)

        # 定位到 chip 视图下方
        chip_view = self._popover.contentViewController().view().superview()
        if chip_view is None:
            return
        self._popover.showRelativeToRect_ofView_preferredEdge_(
            chip_view.frame(), chip_view, AppKit.NSRectEdgeMinY
        )

    def popoverDidClose_(self, notification) -> None:  # noqa: N802
        """NSPopover delegate：关闭时清理。"""
        pass

    # ---- NSPopover 菜单构建（主线程） ----------------------------------- #
    def _populate_popover(self, menu_items: list) -> None:
        """根据 chip 模型的 menu 列表构建 NSPopover 内容。"""
        import AppKit  # type: ignore

        container = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 220, 36))
        y = 0
        row_h = 32

        for item in menu_items:
            if item.get("type") == "sep":
                sep = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(12, y + 15, 196, 1))
                sep.setBoxType_(AppKit.NSBoxSeparator)
                sep.setBorderWidth_(0)
                container.addSubview_(sep)
                y += row_h
                continue

            label = str(item.get("label") or "")
            action = str(item.get("action") or "")
            danger = bool(item.get("danger"))

            btn = AppKit.NSButton.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, y, 220, row_h)
            )
            btn.setBezelStyle_(AppKit.NSBezelStyleRegularSquare)
            btn.setBordered_(False)
            btn.setTitle_(label)
            btn.setFont_(AppKit.NSFont.systemFontOfSize_(13))
            btn.setAlignment_(AppKit.NSTextAlignmentLeft)
            btn.contentEdgeInsets = AppKit.NSEdgeInsets(0, 14, 0, 14)
            btn.setTarget_(self)
            btn.setAction_(self._make_menu_selector(action))

            if danger:
                btn.setTitleColor_(
                    AppKit.NSColor.colorWithRed_green_blue_alpha_(0.85, 0.2, 0.2, 1.0)
                )

            container.addSubview_(btn)
            y += row_h

        container.setFrame_(AppKit.NSMakeRect(0, 0, 220, y))

        # 创建新的 contentViewController
        vc = AppKit.NSViewController.alloc().init()
        vc.setView_(container)
        self._popover.setContentViewController_(vc)
        self._popover.setContentSize_(AppKit.NSSize(220, y))

    def _make_menu_selector(self, action: str):
        """为菜单项创建一个可调用的 selector（_menu_handler_<action>:）。"""
        selector_name = f"_menu_handler_{action}:"
        # 动态绑定方法到 self（pyobjc 允许这种 monkey-patch）
        import AppKit  # type: ignore

        def handler(sender=None):
            self._dispatch_menu(action)

        setattr(self, selector_name, handler)
        # 返回 selector
        return AppKit.NSSelectorFromString(selector_name)

    # ---- 模型推送（线程安全：主线程调度） ------------------------------- #
    def _update_chip(self) -> None:
        """把最新模型推到壳原生 chip（必须在主线程做 UI 更新）。"""
        try:
            from PyObjCTools import AppHelper  # type: ignore
        except Exception:  # noqa: BLE001
            return

        def _do_update() -> None:
            import AppKit  # type: ignore

            if self._chip_label is None:
                return
            model = self._model or {}
            label = str(model.get("label") or "登录").strip()
            self._chip_label.setStringValue_(label[:20] + ("…" if len(label) > 20 else ""))

        AppHelper.callAfter(_do_update)

    # ---- 动作分发 ------------------------------------------------------ #
    def _dispatch_menu(self, action: str) -> None:
        """分发菜单项动作到 main._chip_actions（后台线程，避免阻塞主线程）。"""
        handler = self._menu_actions.get(action)
        if handler is None:
            log.warning("未知/未注册的菜单动作：%s", action)
            return
        # 关闭 popover（在主线程）
        try:
            from PyObjCTools import AppHelper  # type: ignore
            AppHelper.callAfter(lambda: self._popover.performClose_(None) if self._popover else None)
        except Exception:  # noqa: BLE001
            pass
        # 执行动作（后台线程）
        try:
            handler()
        except Exception:  # noqa: BLE001
            log.exception("菜单动作 %s 执行失败", action)

    def _safe_login(self) -> None:
        try:
            self._on_login()
        except Exception:  # noqa: BLE001
            log.exception("发起登录失败")
