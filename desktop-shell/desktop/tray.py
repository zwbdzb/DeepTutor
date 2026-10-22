"""系统托盘 / 菜单栏常驻（Windows 通知区域 + macOS 菜单栏）。

两端行为一致：

* 关闭窗口 = 收进托盘，进程和 DeepTutor 后端继续在后台跑；
* 托盘「退出」（或文件菜单退出 / Cmd+Q）= **真正退出**：销毁窗口 ->
  events.closed -> 停掉 deeptutor start 整棵进程树；
* 托盘「打开」/ 点图标 = 重新显示窗口；
* 再次启动客户端 = 把已收起的窗口叫回来；
* 托盘起不来时不进入「隐藏」状态——关窗退化为旧行为（直接退出），
  免得把窗口藏进一个没有图标的黑洞。

实现：Windows 用 pystray；macOS 用系统原生 NSStatusBar（pyobjc 随
pywebview 的 Cocoa 后端已在包里，不新增依赖），图标复用 assets/icon.png。
macOS 还必须接管 NSApplication 的 delegate，否则 Cmd+Q 会被当成
「关窗」而不是「退出」。
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import sys

from desktop import APP_NAME
from desktop.runtime import APP_DIR

log = logging.getLogger("dt.tray")

# 第二个实例要求托盘把窗口叫回来的 macOS 分布式通知名。
# 应用已经在前台、窗口收在菜单栏时，激活不会再触发 DidBecomeActive，
# 只能靠这条通知（发送端见 single_instance.activate_existing_pid）。
SHOW_NOTIFICATION = "com.thinkbuddy.desktop.show-window"


class SystemTray:
    """托盘门面：Windows 走 pystray，macOS 走原生 NSStatusBar。"""

    def __init__(self, window, on_quit: Callable[[], None]) -> None:
        self._window = window
        self._on_quit = on_quit
        self._icon = None          # Windows: pystray.Icon
        self._mac = None           # macOS: _MacTray
        self._exiting = False

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        if sys.platform == "win32":
            self._start_windows()
        elif sys.platform == "darwin":
            self._start_mac()
        else:
            log.info("system tray is not enabled on %s", sys.platform)

    def _start_windows(self) -> None:
        try:
            from PIL import Image
            import pystray

            with Image.open(APP_DIR / "assets" / "icon.png") as image:
                icon_image = image.convert("RGBA")
            self._icon = pystray.Icon(
                "ThinkBuddy",
                icon_image,
                APP_NAME,
                menu=pystray.Menu(
                    pystray.MenuItem("打开", self.show, default=True),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("退出", self.quit),
                ),
            )
            self._icon.run_detached(self._setup)
        except Exception:  # noqa: BLE001
            log.exception("system tray unavailable; closing the window will exit")
            self.stop()

    def _start_mac(self) -> None:
        tray = _MacTray(self)
        self._mac = tray
        try:
            # start() 在 webview.start() 之前调用，Cocoa 事件循环还没跑。
            # 主窗口 shown 事件在 Cocoa 主线程内触发、NSApp 已就绪，在那里装最稳。
            shown = self._window.events.shown
            if shown.is_set():
                tray.start()                  # 热重启/测试：循环已经在跑
            else:
                shown += tray.start_once
        except Exception:  # noqa: BLE001
            log.exception("could not schedule macOS tray install")
            tray.start()

    def _setup(self, icon) -> None:
        try:
            if not self._exiting:
                icon.visible = True
        except Exception:  # noqa: BLE001
            log.exception("could not show system tray icon")

    # -- window events --------------------------------------------------- #
    def _can_restore(self) -> bool:
        """托盘当前是否真能把窗口叫回来（决定关窗是隐藏还是退出）。"""
        mac = self._mac
        if mac is not None:
            return mac.available
        return self._icon is not None and self._icon.visible

    def on_closing(self) -> bool:
        if self._exiting or not self._can_restore():
            return True
        try:
            self._window.hide()
            return False
        except Exception:  # noqa: BLE001
            log.exception("could not hide window; allowing close")
            return True

    def show(self) -> None:
        if self._exiting:
            return
        try:
            self._window.show()
            self._window.restore()
        except Exception:  # noqa: BLE001
            log.exception("could not restore window from tray")

    def allow_exit(self) -> None:
        self._exiting = True

    def quit(self) -> None:
        self.allow_exit()
        self._on_quit()

    def stop(self) -> None:
        self.allow_exit()
        icon, self._icon = self._icon, None
        mac, self._mac = self._mac, None
        if icon is not None:
            try:
                icon.visible = False
                icon.stop()
            except Exception:  # noqa: BLE001
                log.exception("could not stop system tray")
        if mac is not None:
            try:
                mac.stop()
            except Exception:  # noqa: BLE001
                log.exception("could not stop macOS tray")


# --------------------------------------------------------------------------- #
# macOS 菜单栏实现                                                             #
# --------------------------------------------------------------------------- #
class _MacTray:
    """菜单栏状态项：NSStatusBar + NSMenu，所有操作都排到主线程执行。

    ``available`` 为 True 才允许「关窗隐藏」——状态项装好之前关窗照旧退出。
    """

    def __init__(self, owner: SystemTray) -> None:
        self._owner = owner
        self._item = None
        self._delegate = None
        self._image = None
        self._centers = []
        self._install_requested = False

    @property
    def available(self) -> bool:
        return self._item is not None

    # -- 主线程调度 ------------------------------------------------------ #
    def start_once(self, *_args) -> None:
        """window.events.shown 回调：只装一次。"""
        if self._install_requested:
            return
        self._install_requested = True
        self.start()

    def start(self) -> None:
        try:
            from PyObjCTools import AppHelper
        except Exception:  # noqa: BLE001
            log.exception("PyObjC unavailable; macOS tray disabled")
            return
        try:
            AppHelper.callAfter(self._install)
        except Exception:  # noqa: BLE001
            log.exception("could not schedule macOS tray install")
            self._install()

    def stop(self) -> None:
        try:
            from PyObjCTools import AppHelper
        except Exception:  # noqa: BLE001
            return
        try:
            AppHelper.callAfter(self._uninstall)
        except Exception:  # noqa: BLE001
            log.exception("could not schedule macOS tray teardown")

    # -- 主线程：安装 ---------------------------------------------------- #
    def _install(self) -> None:
        try:
            import AppKit

            self._delegate = _mac_delegate_class().alloc().init()
            self._delegate._owner = self._owner
            # Cmd+Q / 应用菜单「退出」/ 注销必须放行真正退出（默认会被
            # 当作关窗）。pywebview 在 create_window 里装过它自己的
            # delegate，本函数在其之后执行（shown 事件后），以我们的为准。
            AppKit.NSApplication.sharedApplication().setDelegate_(self._delegate)

            item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
                AppKit.NSVariableStatusItemLength
            )
            item.setMenu_(self._build_menu(AppKit))
            button = item.button()
            button.setToolTip_(APP_NAME)
            self._apply_icon(AppKit, button)
            self._item = item

            self._observe_activation(AppKit)
            log.info("macOS menu bar tray ready")
        except Exception:  # noqa: BLE001
            log.exception("macOS tray init failed; closing the window will exit")
            self._uninstall()

    def _build_menu(self, AppKit):
        menu = AppKit.NSMenu.alloc().init()
        # 启用状态自己管：关掉自动校验，免得 target 判定把菜单项置灰。
        menu.setAutoenablesItems_(False)

        show_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"打开 {APP_NAME}", AppKit.NSSelectorFromString("thinkbuddyShow:"), ""
        )
        show_item.setTarget_(self._delegate)
        show_item.setEnabled_(True)
        menu.addItem_(show_item)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"退出 {APP_NAME}", AppKit.NSSelectorFromString("thinkbuddyQuit:"), "q"
        )
        quit_item.setTarget_(self._delegate)
        quit_item.setEnabled_(True)
        menu.addItem_(quit_item)
        return menu

    def _apply_icon(self, AppKit, button) -> None:
        """菜单栏图标：直接用 Windows 通知区域那张 assets/icon.png。"""
        import Foundation

        image = None
        path = APP_DIR / "assets" / "icon.png"
        try:
            if path.is_file():
                image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(path))
        except Exception:  # noqa: BLE001
            log.exception("could not load tray icon: %s", path)

        if image is None:
            button.setTitle_("ThinkBuddy")    # 资源缺失也别裸奔
            log.warning("tray icon missing: %s", path)
            return

        image.setSize_(Foundation.NSMakeSize(18.0, 18.0))
        button.setImage_(image)
        self._image = image                   # 持有引用，避免被回收成空白

    def _observe_activation(self, AppKit) -> None:
        """应用被激活（Dock 点击 / Cmd+Tab / 再次启动）时把窗口叫回来。"""
        import Foundation

        center = Foundation.NSNotificationCenter.defaultCenter()
        token = center.addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationDidBecomeActiveNotification,
            None,
            Foundation.NSOperationQueue.mainQueue(),
            self._on_app_activated,
        )
        self._centers.append((center, token))

        # 应用已在前台、窗口收着时激活不会发 DidBecomeActive，
        # 第二个实例改用显式通知（发送端见 single_instance）。
        distributed = Foundation.NSDistributedNotificationCenter.defaultCenter()
        token = distributed.addObserverForName_object_queue_usingBlock_(
            SHOW_NOTIFICATION,
            None,
            Foundation.NSOperationQueue.mainQueue(),
            self._on_app_activated,
        )
        self._centers.append((distributed, token))

    def _on_app_activated(self, _note=None) -> None:
        try:
            native = getattr(self._owner._window, "native", None)
            if native is not None and not native.isVisible():
                self._owner.show()
        except Exception:  # noqa: BLE001
            log.exception("could not restore window after activation")

    # -- 主线程：卸载 ---------------------------------------------------- #
    def _uninstall(self) -> None:
        centers, self._centers = self._centers, []
        for center, token in centers:
            try:
                center.removeObserver_(token)
            except Exception:  # noqa: BLE001
                pass

        item, self._item = self._item, None
        if item is not None:
            try:
                import AppKit

                AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(item)
            except Exception:  # noqa: BLE001
                log.exception("could not remove menu bar item")


_MAC_DELEGATE_CLASS = None


def _mac_delegate_class():
    """构造（并缓存）接管 NSApplication 的 delegate 类。

    pyobjc 在类定义时就注册 ObjC 类，重复定义会告警，所以进程内只建一次；
    回调通过实例属性 ``_owner`` 指回 SystemTray。
    """
    global _MAC_DELEGATE_CLASS
    if _MAC_DELEGATE_CLASS is not None:
        return _MAC_DELEGATE_CLASS

    import AppKit
    import Foundation

    class _ThinkBuddyAppDelegate(AppKit.NSObject):
        # ---- NSApplicationDelegate ------------------------------------ #
        def applicationShouldTerminate_(self, app):  # noqa: N802
            # 真正的退出：先标记 exiting，窗口的 closing 处理才不会把它收回去。
            try:
                self._owner.allow_exit()
            except Exception:  # noqa: BLE001
                log.exception("could not mark tray as exiting")
            return Foundation.YES

        def applicationShouldHandleReopen_hasVisibleWindows_(  # noqa: N802
            self, app, has_visible_windows
        ):
            try:
                self._owner.show()
            except Exception:  # noqa: BLE001
                log.exception("could not restore window from dock")
            return Foundation.YES

        def applicationSupportsSecureRestorableState_(self, app):  # noqa: N802
            return Foundation.YES

        # ---- NSMenuItem target / action ------------------------------- #
        def thinkbuddyShow_(self, sender):  # noqa: N802
            try:
                self._owner.show()
            except Exception:  # noqa: BLE001
                log.exception("could not restore window from tray")

        def thinkbuddyQuit_(self, sender):  # noqa: N802
            try:
                self._owner.quit()
            except Exception:  # noqa: BLE001
                log.exception("could not quit from tray")

    _MAC_DELEGATE_CLASS = _ThinkBuddyAppDelegate
    return _MAC_DELEGATE_CLASS


__all__ = ["SHOW_NOTIFICATION", "SystemTray"]
