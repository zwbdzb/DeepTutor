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
在原生标题栏**右侧**（红绿灯对侧、与 Windows 账号区同位）加自定义
``NSView`` + ``NSTextField`` 显示账号文案。pywebview 的 Cocoa
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
TOAST_ID = "thinkbuddy-toast"

# 确保 toast 浮层元素存在（幂等；SPA 路由/整页刷新后由定时器补挂）。
# 样式主题化（2026-10）：颜色/阴影/字体全部走页面 CSS 变量（globals.css 的
# Cream/Dark/glass 等四套主题 token），自动跟随前端主题；变量未定义时回退
# 内置深色值。语义圆点用 ::before 按 data-kind 变色：info=蓝 / success=绿 /
# error=红。注意：本模板经 Python % 格式化下发，JS 里的字面 % 必须写成 %%。
TOAST_ENSURE_JS = """
(function () {
  if (!document.body) { return 'no-body'; }
  var t = document.getElementById('%(id)s');
  var result = 'exists';
  if (!t) {
    t = document.createElement('div');
    t.id = '%(id)s';
    t.textContent = '';
    // cssText 用双引号 JS 字符串：内含单引号字体名，避免转义地狱。
    t.style.cssText = "position:fixed; bottom:24px; left:50%%;" +
      "transform:translateX(-50%%);" +
      "display:flex; align-items:center; gap:8px;" +
      "max-width:min(520px, calc(100vw - 48px));" +
      "padding:9px 16px 9px 14px;" +
      "background:var(--popover, rgba(36,34,32,0.92));" +
      "color:var(--popover-foreground, #fff);" +
      "border:1px solid var(--border, rgba(127,127,127,0.25));" +
      "border-radius:8px;" +
      "box-shadow:var(--shadow-raised, 0 8px 24px rgba(0,0,0,0.18));" +
      "font:500 14px/1.5 var(--font-sans, -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif);" +
      "z-index:2147483000; pointer-events:none;" +
      "opacity:0; transition:opacity .18s ease;";
    document.body.appendChild(t);
    result = 'created';
  }
  // 语义圆点样式：幂等注入一次（style 元素与 toast div 生命周期一致，
  // SPA 整页刷新后 head 一并重建，由同一轮 ensure 补挂）。
  if (document.head && !document.getElementById('thinkbuddy-toast-style')) {
    var s = document.createElement('style');
    s.id = 'thinkbuddy-toast-style';
    s.textContent = '#thinkbuddy-toast::before { content:""; width:8px; height:8px;' +
      'border-radius:50%%; background:var(--info, #2563eb); flex:none; }' +
      '#thinkbuddy-toast[data-kind="success"]::before { background:var(--success, #237a4b); }' +
      '#thinkbuddy-toast[data-kind="error"]::before { background:var(--destructive, #c53a2c); }';
    document.head.appendChild(s);
  }
  // 挂载全局 toast 函数，供 menubar._toast / main.toast 通过 evaluate_js 调用。
  // SPA 路由切换 / 整页刷新后 window 上下文重建，需由定时器重新挂载。
  // 第三参 kind 可选（'info' | 'success' | 'error'），缺省 'info'——
  // 旧两参调用（menubar/main 缓存的旧 JS 通道）依然有效。
  if (!window.__thinkbuddyToast) {
    window.__thinkbuddyToast = function (msg, duration, kind) {
      var el = document.getElementById('%(id)s');
      if (!el) { return; }
      el.textContent = msg;
      if (el.dataset) { el.dataset.kind = kind || 'info'; }
      el.style.opacity = '1';
      clearTimeout(window.__thinkbuddyToastTimer);
      window.__thinkbuddyToastTimer = setTimeout(function () {
        el.style.opacity = '0';
      }, duration || 2000);
    };
  }
  return result;
})();
"""

TOAST_SHOW_JS = """
(function () {
  var t = document.getElementById('%(id)s');
  if (!t) return;
  t.textContent = %(msg)s;
  if (t.dataset) { t.dataset.kind = %(kind)s; }
  t.style.opacity = '1';
  clearTimeout(window.__thinkbuddyToastTimer);
  window.__thinkbuddyToastTimer = setTimeout(function () {
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
                    # TOAST_ENSURE_JS 是含 %(id)s 的模板，必须先格式化再下发；
                    # 裸 evaluate 会在页面里挂出 id 为字面 '%(id)s' 的脏节点，
                    # 且 show()（按真实 id 格式化的 TOAST_SHOW_JS）永远找不到元素。
                    self._w.evaluate_js(TOAST_ENSURE_JS % {"id": TOAST_ID})
                except Exception:  # noqa: BLE001  页面未就绪，下一轮再试
                    pass
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()

    def show(self, message: str, duration_ms: int = 2000,
             kind: str = "info") -> None:
        """直接驱动 toast 显示（kind: 'info' | 'success' | 'error'）。"""
        try:
            self._w.evaluate_js(
                TOAST_SHOW_JS % {
                    "id": TOAST_ID,
                    "msg": json.dumps(message, ensure_ascii=False),
                    "duration": duration_ms,
                    "kind": json.dumps(kind, ensure_ascii=False),
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
# 壳原生方案：用 NSWindow.titlebarAccessoryViewController 在原生标题栏
# 右侧加自定义 NSView + NSButton 显示账号文案。位置和 Windows 账号区一致。
# 点击 chip 弹出 NSPopover 显示下拉菜单（壳原生，不走页面内注入）。
# chip 用 NSButton（标题栏可点击控件的标准做法）而非 NSClickGestureRecognizer
# ——标题栏区域的鼠标事件优先被窗口拖拽逻辑消费，手势识别器不可靠。
#
# pywebview 的 Cocoa 后端已在顶层 import AppKit / Foundation / WebKit /
# objc / PyObjCTools.AppHelper（见 webview/platforms/cocoa.py 第 11-15
# 行），所以打包后 pyobjc 必然存在，这里只做延迟 import 避免非 mac 环境
# 跑起来崩掉。

import sys

if sys.platform == "darwin":
    import objc  # noqa: PLC0415  仅 darwin 存在 pyobjc

    import AppKit  # noqa: PLC0415

    from Foundation import NSObject  # noqa: PLC0415

    class _ChipActionTarget(NSObject):  # noqa: N801  (pyobjc 类命名)
        """chip 与菜单项按钮共享的 ObjC target。

        action 选择器必须定义在 NSObject 子类的**类上**，ObjC 运行时才
        派发得到（曾把 handler 挂在普通 Python 对象实例上 + 手动
        NSSelectorFromString，真机点击静默无响应）。所有按钮共用一个
        target，动作名经 NSButton.representedObject 携带。
        """

        def initWithInjector_(self, injector):  # noqa: N802
            self = objc.super(_ChipActionTarget, self).init()
            if self is None:
                return None
            self._injector = injector
            return self

        def chipClicked_(self, sender):  # noqa: N802
            log.debug("chip action chipClicked: 派发进入")
            self._injector.handle_chip_click()

        def menuItemClicked_(self, sender):  # noqa: N802
            action = str(sender.representedObject() or "")
            log.info("chip 菜单项动作：%s", action)
            self._injector._dispatch_menu(action)

        def headerRefreshClicked_(self, sender):  # noqa: N802
            """菜单头行 ⟳ 按钮：先收起弹出菜单再派发 refresh_user。

            视图化菜单项里的 NSButton 点击不会自动结束菜单追踪（普通
            NSMenuItem 才会），需显式 cancelTracking——否则余额在背后更新
            了，用户看到的还是打开时的旧菜单。动作经 enclosingMenuItem()
            反查所属菜单，无需持有引用。
            """
            try:
                item = sender.enclosingMenuItem()
                menu = item.menu() if item is not None else None
                if menu is not None:
                    menu.cancelTracking()
            except Exception:  # noqa: BLE001  收起失败不影响动作派发
                log.exception("cancel menu tracking failed")
            action = str(sender.representedObject() or "refresh_user")
            log.info("chip 头行刷新动作：%s", action)
            self._injector._dispatch_menu(action)

        # ---- 标题栏药丸（更新·黄 / 签到·绿）------------------------------ #
        # 与 Windows AccountChip 的动作名严格一致（见 titlebar_account
        # .MENU_ACTIONS）：药丸主体 = 执行动作，✕ = 收起。
        def checkinPillClicked_(self, sender):  # noqa: N802
            log.info("签到药丸点击：执行签到")
            self._injector._dispatch_menu("checkin")

        def checkinDismissClicked_(self, sender):  # noqa: N802
            log.info("签到药丸点击：今天不再提示")
            self._injector._dispatch_menu("checkin_dismiss")

        def updatePillClicked_(self, sender):  # noqa: N802
            log.info("更新药丸点击：安装并重启")
            self._injector._dispatch_menu("update_restart")

        def updateDismissClicked_(self, sender):  # noqa: N802
            log.info("更新药丸点击：收起角标")
            self._injector._dispatch_menu("update_dismiss")


# --------------------------------------------------------------------------- #
# 标题栏药丸几何与配色（DIP / 0-255 RGB）------------------------------------- #
# --------------------------------------------------------------------------- #
# 与 Windows AccountChip 的 _CHECKIN_* / _UPDATE_* 常量保持同一套比例，
# 保证两个平台看起来是同一个控件：
#   [黄·更新] gap [绿·签到] gap [chip]
_PILL_GAP = 10.0          # 药丸之间 / 药丸与 chip 之间
_PILL_PAD_L = 10.0        # 药丸内左留白
_PILL_PAD_R = 6.0         # 药丸内右留白（✕ 之后）
_PILL_CLOSE_W = 16.0      # ✕ 命中盒边长
_PILL_CLOSE_GAP = 4.0     # 文案与 ✕ 之间
_PILL_H = 24.0            # 药丸高度
_CHIP_H = 28.0            # 标题栏控件高度（历史值）
_CHIP_PAD_X = 12.0        # chip 文案两侧留白
_CHIP_MIN_W = 80.0        # chip 最小宽度（避免短用户名太挤）

# (底色, 边框, 文案色, ✕ 色)；色值对齐 Windows 自绘药丸与 Tailwind 调色板
_PILL_STYLE = {
    "update": ((0xFE / 255, 0xF3 / 255, 0xC7 / 255), (0xE9 / 255, 0xD9 / 255, 0x9E / 255),
               (0xB4 / 255, 0x53 / 255, 0x09 / 255), (0xA1 / 255, 0x62 / 255, 0x07 / 255)),
    "checkin": ((0xDC / 255, 0xFC / 255, 0xE7 / 255), (0xBB / 255, 0xF7 / 255, 0xD0 / 255),
                (0x15 / 255, 0x80 / 255, 0x3D / 255), (0x16 / 255, 0x65 / 255, 0x34 / 255)),
}
# 药丸主体 / ✕ 按钮的 action 选择器（见 _ChipActionTarget）
_PILL_SELECTORS = {
    "update": ("updatePillClicked:", "updateDismissClicked:"),
    "checkin": ("checkinPillClicked:", "checkinDismissClicked:"),
}


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
        self._target = None     # _ChipActionTarget（chip + 菜单项共享）
        self._chip_button = None  # NSButton（chip 本体）
        # 标题栏药丸：{"update": (body_btn, close_btn), "checkin": (...)}；
        # 构建失败时为空表 → 保持历史单按钮 chip 行为（降级）。
        self._pill_views: dict = {}

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

        # ---- 创建 chip NSView + NSButton ----
        # 共享 ObjC target（选择器在类上，运行时可派发，见 _ChipActionTarget）
        self._target = _ChipActionTarget.alloc().initWithInjector_(self)

        container = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 160, 28))
        container.setWantsLayer_(True)
        container.layer().setCornerRadius_(6)
        container.layer().setBackgroundColor_(
            AppKit.NSColor.colorWithRed_green_blue_alpha_(0.96, 0.96, 0.96, 0.85).CGColor()
        )

        # borderless NSButton：标题栏里可点击控件的标准做法，原生 hit-test 可靠
        btn = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 160, 28))
        btn.setBordered_(False)
        btn.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        btn.setAlignment_(AppKit.NSTextAlignmentCenter)
        btn.setTitle_("登录 ▾")
        btn.setTarget_(self._target)
        btn.setAction_(AppKit.NSSelectorFromString("chipClicked:"))
        container.addSubview_(btn)
        self._chip_button = btn

        # ---- 药丸层（黄·更新 / 绿·签到）：chip 按钮左侧 ----
        # 顺序与 Windows 一致：[黄·更新][绿·签到][chip]；无绿色药丸时
        # [黄][chip]。构建失败只记日志并降级为单按钮 chip —— 标题栏绝不能
        # 因为药丸崩掉。
        try:
            self._build_pill_views(AppKit, container)
        except Exception:  # noqa: BLE001
            log.exception("mac 标题栏药丸构建失败，降级为单按钮 chip")
            self._pill_views = {}

        # ---- 创建 NSViewController ----
        # 必须是 NSTitlebarAccessoryViewController（layoutAttribute 在它身上），
        # 普通 NSViewController 没有 setLayoutAttribute_。
        vc = AppKit.NSTitlebarAccessoryViewController.alloc().init()
        vc.setView_(container)
        # 挂载用数组版 API：单一 `setTitlebarAccessoryViewController_` 已被
        # Apple 弃用并从新版 SDK 移除，pyobjc 12 绑定里不存在（曾致
        # AttributeError: NSKVONotifying_WindowHost has no attribute ...，
        # KVO isa-swap 只是报错里的表象）。layoutAttribute 须在挂载前设置：
        # Trailing = 标题栏右侧，与 Windows 账号区位置一致。
        vc.setLayoutAttribute_(AppKit.NSLayoutAttributeTrailing)
        ns_window.addTitlebarAccessoryViewController_(vc)

    # ---- 药丸视图（主线程，仅在 _init_chip 内调用） ------------------------ #
    def _build_pill_views(self, AppKit, container) -> None:
        """在 chip 按钮左侧创建两枚药丸视图（更新·黄 / 签到·绿）。

        每枚药丸 = 一个无边框 NSButton（主体，wantsLayer 自绘底色/圆角，
        attributedTitle 上色）+ 一个叠在它右端之上的独立小 NSButton（✕）。
        后添加的子视图在上层，所以 ✕ 区域的点击优先命中关闭按钮，其余区域
        命中药丸主体——与 Windows 的矩形命中测试等价。

        初始一律隐藏，实际可见性与宽度由 :meth:`_layout_pills` 按模型决定。
        """
        for key, (body_sel, close_sel) in _PILL_SELECTORS.items():
            bg, border, _fg, close_fg = _PILL_STYLE[key]
            body = AppKit.NSButton.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, 0, _PILL_H)
            )
            body.setBordered_(False)
            body.setWantsLayer_(True)
            body.setFont_(AppKit.NSFont.systemFontOfSize_(12))
            body.setAlignment_(AppKit.NSTextAlignmentCenter)
            body.setTarget_(self._target)
            body.setAction_(AppKit.NSSelectorFromString(body_sel))
            self._style_pill_layer(AppKit, body, bg, border)
            body.setHidden_(True)
            container.addSubview_(body)

            close = AppKit.NSButton.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, _PILL_CLOSE_W, _PILL_CLOSE_W)
            )
            close.setBordered_(False)
            close.setWantsLayer_(True)
            close.setFont_(AppKit.NSFont.systemFontOfSize_(12))
            close.setAlignment_(AppKit.NSTextAlignmentCenter)
            close.setTarget_(self._target)
            close.setAction_(AppKit.NSSelectorFromString(close_sel))
            close.setAttributedTitle_(
                self._attr_title(AppKit, "✕", close_fg, AppKit.NSFont.systemFontOfSize_(11))
            )
            close.setHidden_(True)
            container.addSubview_(close)

            self._pill_views[key] = (body, close)

    @staticmethod
    def _style_pill_layer(AppKit, view, bg, border) -> None:
        """给视图铺药丸底色 + 全圆角 + 1px 同色系边框。"""
        view.setWantsLayer_(True)
        layer = view.layer()
        layer.setBackgroundColor_(
            AppKit.NSColor.colorWithRed_green_blue_alpha_(
                bg[0], bg[1], bg[2], 1.0
            ).CGColor()
        )
        layer.setCornerRadius_(_PILL_H / 2.0)
        layer.setBorderWidth_(1)
        layer.setBorderColor_(
            AppKit.NSColor.colorWithRed_green_blue_alpha_(
                border[0], border[1], border[2], 1.0
            ).CGColor()
        )

    @staticmethod
    def _attr_title(AppKit, text: str, rgb, font):
        """带前景色的 attributed title（NSButton 没有 setTitleColor_）。"""
        return AppKit.NSAttributedString.alloc().initWithString_attributes_(
            text,
            AppKit.NSDictionary.dictionaryWithObjects_forKeys_(
                [
                    AppKit.NSColor.colorWithRed_green_blue_alpha_(
                        rgb[0], rgb[1], rgb[2], 1.0
                    ),
                    font,
                ],
                [AppKit.NSForegroundColorAttributeName, AppKit.NSFontAttributeName],
            ),
        )

    @staticmethod
    def _text_width(AppKit, text: str, font) -> float:
        """按字体实测文本宽度（标题栏不自作换行，宽度必须贴字）。"""
        attrs = {AppKit.NSFontAttributeName: font}
        size = (AppKit.NSAttributedString.alloc()
                .initWithString_attributes_(str(text or ""), attrs).size())
        try:
            return float(size.width)
        except Exception:  # noqa: BLE001  老版本 pyobjc 返回 NSSize 结构
            return float(size[0])

    def _pill_model(self, key: str) -> dict | None:
        """取某枚药丸的模型（None = 不显示）。"""
        model = self._model or {}
        value = model.get(key)
        if isinstance(value, dict) and value.get("label"):
            return value
        return None

    def _layout_pills(self, container, chip_title: str) -> None:
        """按当前模型重排 [黄·更新][绿·签到][chip] 并同步容器宽度。

        Trailing 布局下只改宽度即可——右侧贴齐由 AppKit 负责，宽度变小时
        左缘自动右移。任何异常都吞掉（标题栏宁可少画药丸也不能崩）。
        """
        try:
            import AppKit  # type: ignore
        except Exception:  # noqa: BLE001
            return
        try:
            if not self._pill_views:
                return      # 药丸层未建成（降级）：保持历史 160x28 单按钮 chip
            font = self._chip_button.font() or AppKit.NSFont.systemFontOfSize_(12)
            x = 0.0
            for key in ("update", "checkin"):
                width = self._place_pill(AppKit, key, font, x)
                if width > 0:
                    x += width + _PILL_GAP
            chip_w = max(
                _CHIP_MIN_W,
                self._text_width(AppKit, chip_title, font) + 2 * _CHIP_PAD_X,
            )
            self._chip_button.setFrame_(
                AppKit.NSMakeRect(x, 0.0, chip_w, _CHIP_H)
            )
            container.setFrameSize_(AppKit.NSMakeSize(x + chip_w, _CHIP_H))
        except Exception:  # noqa: BLE001
            log.exception("mac 标题栏药丸布局失败")

    def _place_pill(self, AppKit, key: str, font, x: float) -> float:
        """摆放一枚药丸（文案 + ✕），返回占用宽度（隐藏时 0）。"""
        views = self._pill_views.get(key)
        if views is None:
            return 0.0
        body, close = views
        pill_model = self._pill_model(key)
        label = str((pill_model or {}).get("label") or "").strip()
        if not label:
            body.setHidden_(True)
            close.setHidden_(True)
            return 0.0
        _bg, _border, fg, _close_fg = _PILL_STYLE[key]
        width = (_PILL_PAD_L + self._text_width(AppKit, label, font)
                 + _PILL_CLOSE_GAP + _PILL_CLOSE_W + _PILL_PAD_R)
        top = (_CHIP_H - _PILL_H) / 2.0
        body.setAttributedTitle_(
            self._attr_title(AppKit, label, fg, font)
        )
        body.setFrame_(AppKit.NSMakeRect(x, top, width, _PILL_H))
        body.setHidden_(False)
        # ✕ 叠在药丸右端之上（后添加的子视图在上层 → 命中优先）
        close.setFrame_(
            AppKit.NSMakeRect(
                x + width - _PILL_PAD_R - _PILL_CLOSE_W,
                top + (_PILL_H - _PILL_CLOSE_W) / 2.0,
                _PILL_CLOSE_W, _PILL_CLOSE_W,
            )
        )
        close.setHidden_(False)
        return width

    # ---- AppKit 回调（主线程） ------------------------------------------ #
    def handle_chip_click(self) -> None:
        """点击 chip：有菜单则弹出 NSMenu，无菜单则发登录。

        用 NSMenu.popUpMenu...（鼠标全局位置弹出）而非 NSPopover：popover
        需要窗口内锚点视图且受标题栏布局影响，两轮真机验证都弹不出来；
        NSMenu 原生下拉观感一致、零锚定依赖。
        """
        import AppKit  # type: ignore

        log.info("chip 点击事件进入 handle_chip_click")
        model = self._model or {}
        # menu 是 dict：{"header": {title, sub}, "items": [...]}（见
        # titlebar_account.account_menu_model），不是列表——曾直接遍历它，
        # 迭代出的是键字符串，item.get 炸出 AttributeError（真机菜单从未弹出）。
        menu_model = model.get("menu") or {}
        menu_items = menu_model.get("items") or []

        if not menu_items:
            # 未登录：发起 OAuth
            log.info("mac 壳账号 chip 点击：无菜单（未登录），触发登录")
            self._safe_login()
            return

        try:
            menu = AppKit.NSMenu.alloc().init()
            menu.setAutoenablesItems_(False)

            # 菜单头（账号名 + 余额），禁用态展示，与 Windows 下拉一致；
            # 带 refresh_icon 标志时升级为「文字 + ⟳ 刷新按钮」视图化条目
            # （NSMenuItem.setView_），构建失败回退普通禁用头行。
            header = menu_model.get("header") or {}
            htext = "  ".join(
                part for part in (str(header.get("title") or "").strip(),
                                  str(header.get("sub") or "").strip()) if part
            )
            if htext:
                added = False
                if (header.get("refresh_icon")
                        and "refresh_user" in self._menu_actions):
                    try:
                        menu.addItem_(self._build_header_view_item(htext))
                        added = True
                    except Exception:  # noqa: BLE001
                        log.exception("mac header view item failed; fallback")
                if not added:
                    hmi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                        htext, None, ""
                    )
                    hmi.setEnabled_(False)
                    menu.addItem_(hmi)
                menu.addItem_(AppKit.NSMenuItem.separatorItem())

            for item in menu_items:
                if item.get("type") == "sep":
                    menu.addItem_(AppKit.NSMenuItem.separatorItem())
                    continue
                label = str(item.get("label") or "")
                action = str(item.get("action") or "")
                mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    label, AppKit.NSSelectorFromString("menuItemClicked:"), ""
                )
                mi.setTarget_(self._target)
                mi.setRepresentedObject_(action)
                # 危险项红色文字：NSButton/NSMenuItem 都没有 setTitleColor_，
                # 走 attributed title（曾致 AttributeError）。
                if item.get("danger"):
                    from AppKit import (  # noqa: PLC0415
                        NSAttributedString, NSColor, NSDictionary, NSFont,
                        NSFontAttributeName, NSForegroundColorAttributeName,
                    )
                    mi.setAttributedTitle_(
                        NSAttributedString.alloc().initWithString_attributes_(
                            label,
                            NSDictionary.dictionaryWithObjects_forKeys_(
                                [NSColor.colorWithRed_green_blue_alpha_(0.85, 0.2, 0.2, 1.0),
                                 NSFont.systemFontOfSize_(13)],
                                [NSForegroundColorAttributeName, NSFontAttributeName],
                            ),
                        )
                    )
                menu.addItem_(mi)
        except Exception:  # noqa: BLE001
            log.exception("chip 下拉菜单构建失败")
            return

        # 弹在鼠标当前位置（全局屏幕坐标，inView=None），与 chip 视图层级解耦
        loc = AppKit.NSEvent.mouseLocation()
        log.info("mac 壳账号 chip 点击：%d 个菜单项，弹出 NSMenu @ (%.0f, %.0f)",
                 len(menu_items), loc.x, loc.y)
        AppKit.NSMenu.popUpMenuPositioningItem_atLocation_inView_(menu, None, loc, None)

    # ---- 菜单头行视图化条目（主线程调用） --------------------------------- #
    def _build_header_view_item(self, htext):
        """「用户名 · 余额 ＋ ⟳ 刷新按钮」的视图化菜单头行。

        NSMenuItem.setView_ 换成自定义 NSView：左侧 NSTextField 只读文本
        （secondaryLabelColor 弱化成头行观感），右侧 borderless NSButton
        挂 headerRefreshClicked:。刷新符号优先用系统 SF Symbol
        ``arrow.clockwise``（macOS 11+），取不到回退 ⟳ 字符。
        所有 AppKit 调用已处于主线程（NSMenu 弹出追踪循环内）。
        """
        import AppKit  # type: ignore

        font = (AppKit.NSFont.menuFontOfSize_(13.0)
                or AppKit.NSFont.systemFontOfSize_(13.0))
        attrs = {AppKit.NSFontAttributeName: font}
        text_w = float(
            AppKit.NSAttributedString.alloc()
            .initWithString_attributes_(htext, attrs).size().width
        )
        pad_l, gap, btn_w, pad_r = 12.0, 4.0, 24.0, 10.0
        w = pad_l + text_w + gap + btn_w + pad_r
        h = 22.0

        container = AppKit.NSView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, w, h)
        )

        label = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(pad_l, 3.0, text_w + 8.0, h - 6.0)
        )
        label.setStringValue_(htext)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(font)
        try:
            label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        except Exception:  # noqa: BLE001  旧系统取不到弱化色则用默认
            pass
        container.addSubview_(label)

        btn = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(pad_l + text_w + gap, 1.0, btn_w, h - 2.0)
        )
        btn.setBordered_(False)
        img = None
        try:
            img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "arrow.clockwise", None
            )
        except Exception:  # noqa: BLE001  macOS <11 无系统符号图
            img = None
        if img is not None:
            btn.setImage_(img)
            btn.setImagePosition_(AppKit.NSImageOnly)
        else:
            btn.setTitle_("⟳")
        btn.setTarget_(self._target)
        btn.setAction_(AppKit.NSSelectorFromString("headerRefreshClicked:"))
        btn.setRepresentedObject_("refresh_user")
        container.addSubview_(btn)

        item = AppKit.NSMenuItem.alloc().init()
        item.setView_(container)
        return item

    # ---- 模型推送（线程安全：主线程调度） ------------------------------- #
    def _update_chip(self) -> None:
        """把最新模型推到壳原生 chip（必须在主线程做 UI 更新）。"""
        try:
            from PyObjCTools import AppHelper  # type: ignore
        except Exception:  # noqa: BLE001
            return

        def _do_update() -> None:
            if self._chip_button is None:
                return
            model = self._model or {}
            logged_in = bool(model.get("logged_in"))

            # chip 容器（按钮的 superview）
            container = self._chip_button.superview()
            if container is None:
                return

            if not logged_in:
                # 未登录：隐藏整个 chip 容器（从标题栏移除视觉占用）
                container.setHidden_(True)
                return

            # 已登录：显示 chip + 更新文案（文案后跟 ▾ 表示有下拉菜单）
            container.setHidden_(False)
            label = str(model.get("label") or "").strip()
            label = label[:20] + ("…" if len(label) > 20 else "")
            caret = " ▾" if ((model.get("menu") or {}).get("items")) else ""
            title = label + caret
            self._chip_button.setTitle_(title)
            # 两枚药丸（黄·更新 / 绿·签到）+ chip 一起重排：宽度按文案实测，
            # 容器宽度 = 各视图 + 间距之和，右侧贴齐由 Trailing 布局保证。
            self._layout_pills(container, title)

        AppHelper.callAfter(_do_update)

    # ---- 动作分发 ------------------------------------------------------ #
    def _dispatch_menu(self, action: str) -> None:
        """分发菜单项动作到 main._chip_actions（后台线程，避免阻塞主线程）。

        菜单项动作在主线程的 NSMenu 追踪循环里触发；动作里可能有弹窗
        （osascript 子进程、最长 60s）或网络请求，内联执行会冻住 UI。
        """
        handler = self._menu_actions.get(action)
        if handler is None:
            log.warning("未知/未注册的菜单动作：%s", action)
            return
        threading.Thread(
            target=self._run_action, args=(action, handler),
            name=f"chip-action-{action}", daemon=True,
        ).start()

    def _run_action(self, action: str, handler) -> None:
        try:
            handler()
        except Exception:  # noqa: BLE001
            log.exception("菜单动作 %s 执行失败", action)

    def _safe_login(self) -> None:
        try:
            self._on_login()
        except Exception:  # noqa: BLE001
            log.exception("发起登录失败")
