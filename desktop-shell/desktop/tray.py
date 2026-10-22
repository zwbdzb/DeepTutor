"""系统托盘（Windows 通知区域 / macOS 菜单栏）。

两端共用一套实现：pystray 在 macOS 上自带 AppKit 后端（NSStatusBar），
``run_detached()`` 正是为「寄生在别人的主循环里」设计的——pywebview 已经
跑着 NSApplication 的主循环，这里只要把图标挂上去即可，不需要自己碰
NSStatusBar / NSMenu，macOS 也就没有单独的托盘代码要维护。

行为（两端一致）：

* 关闭窗口 = 收进托盘，进程和 DeepTutor 后端继续在后台跑；
* 托盘「退出」（或文件菜单退出 / Cmd+Q）= **真正退出**：销毁窗口 ->
  events.closed -> 停掉 deeptutor start 整棵进程树；
* 托盘「打开」/ 点图标 = 重新显示窗口；
* 再次启动客户端 = 把已收起的窗口叫回来；
* 托盘起不来时不进入「隐藏」状态——关窗退化为旧行为（直接退出），
  免得把窗口藏进一个没有图标的黑洞。

macOS 只有两件 Windows 没有的事，用 pyobjc 补十几行（``_install_mac_hooks``）：
Cmd+Q 走正常退出链、点 Dock 图标唤回窗口。pyobjc 随 pywebview 的 Cocoa
后端已在包里，不新增依赖。
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import sys

from desktop import APP_NAME
from desktop.runtime import APP_DIR

log = logging.getLogger("dt.tray")

# macOS：第二个实例要求托盘把窗口叫回来的分布式通知名（发送端见
# single_instance._ask_tray_to_show）。
SHOW_NOTIFICATION = "com.thinkbuddy.desktop.show-window"


class SystemTray:
    """托盘门面：Windows 通知区域 / macOS 菜单栏，统一走 pystray。"""

    def __init__(self, window, on_quit: Callable[[], None]) -> None:
        self._window = window
        self._on_quit = on_quit
        self._icon = None          # pystray.Icon
        self._exiting = False
        self._mac = None           # macOS: (delegate, center, observer_token)

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        # macOS 上 pystray 会直接操作 NSStatusBar，必须在主线程建图标：
        # main() 里是在 webview.start() 之前调用，正好是主线程。
        if sys.platform == "darwin":
            self._install_mac_hooks()
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
            # 两端都是「寄生」到宿主主循环：macOS 是 pywebview 的
            # NSApplication，Windows 是自带的 win32 消息循环。
            self._icon.run_detached(self._setup)
        except Exception:  # noqa: BLE001
            log.exception("system tray unavailable; closing the window will exit")
            self.stop()

    def _setup(self, icon) -> None:
        try:
            if not self._exiting:
                icon.visible = True
        except Exception:  # noqa: BLE001
            log.exception("could not show system tray icon")

    # -- window events --------------------------------------------------- #
    def _can_restore(self) -> bool:
        """托盘当前是否真能把窗口叫回来（决定关窗是隐藏还是退出）。"""
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
        if mac is not None:
            try:
                mac[1].removeObserver_(mac[2])
            except Exception:  # noqa: BLE001
                log.exception("could not remove macOS activation observer")
        if icon is not None:
            try:
                icon.visible = False
                # pystray 的 darwin stop() 会调 NSApp.stop_()（它假设自己拥有
                # 主循环）。这里的主循环归 pywebview，退不退出由关窗链决定。
                if sys.platform != "darwin":
                    icon.stop()
            except Exception:  # noqa: BLE001
                log.exception("could not stop system tray")

    # -- macOS only ------------------------------------------------------ #
    def _install_mac_hooks(self) -> None:
        """接管 Cmd+Q 与 Dock 唤回（其余托盘逻辑与 Windows 完全共用）。

        必须在主线程、pywebview 建完窗口之后调用：``create_window`` 里
        pywebview 已装过自己的 delegate，这里在其之后覆盖。
        """
        try:
            import AppKit
            import Foundation

            delegate = _mac_delegate_class().alloc().init()
            delegate._tray = self
            AppKit.NSApplication.sharedApplication().setDelegate_(delegate)

            # 应用已在前台、窗口收着时，激活不会触发 DidBecomeActive，
            # 第二个实例改用显式通知（发送端见 single_instance）。
            center = Foundation.NSDistributedNotificationCenter.defaultCenter()
            token = center.addObserverForName_object_queue_usingBlock_(
                SHOW_NOTIFICATION,
                None,
                Foundation.NSOperationQueue.mainQueue(),
                lambda _note: self.show(),
            )
            self._mac = (delegate, center, token)
            log.info("macOS app hooks installed")
        except Exception:  # noqa: BLE001
            log.exception("macOS app hooks unavailable; Cmd+Q will only hide the window")


_MAC_DELEGATE_CLASS = None


def _mac_delegate_class():
    """构造（并缓存）托盘用的 NSApplication delegate 类。

    pyobjc 在类定义时就注册 ObjC 类，重复定义会告警，所以进程内只建一次；
    回调通过实例属性 ``_tray`` 指回 SystemTray。
    """
    global _MAC_DELEGATE_CLASS
    if _MAC_DELEGATE_CLASS is not None:
        return _MAC_DELEGATE_CLASS

    import AppKit
    import Foundation

    class ThinkBuddyTrayDelegate(AppKit.NSObject):
        def applicationShouldTerminate_(self, app):  # noqa: N802
            # Cmd+Q / 应用菜单「退出」：pywebview 的 delegate 会把它当成一次
            # 关窗（随后被 on_closing 收进托盘）。这里取消 AppKit 的硬退，
            # 改成下一轮跑正常退出链：销毁窗口 -> closed -> 停后端进程树。
            from PyObjCTools import AppHelper

            AppHelper.callAfter(self._tray.quit)
            return Foundation.NO

        def applicationShouldHandleReopen_hasVisibleWindows_(  # noqa: N802
            self, app, has_visible_windows
        ):
            # 点 Dock 图标 / LaunchServices 再次打开时唤回窗口；pywebview 不实现。
            self._tray.show()
            return Foundation.YES

        def applicationSupportsSecureRestorableState_(self, app):  # noqa: N802
            return Foundation.YES

    _MAC_DELEGATE_CLASS = ThinkBuddyTrayDelegate
    return _MAC_DELEGATE_CLASS


__all__ = ["SHOW_NOTIFICATION", "SystemTray"]
