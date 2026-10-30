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

平台钩子统一在 ``on_window_ready()`` 里装（等 ``events.shown``，即
pywebview 建完原生窗口之后），早了会白装或摸到空句柄：

* macOS：替换 app delegate 接管 Cmd+Q（走正常退出链）与 Dock 唤回。
  pywebview 建窗口时才装自己的 delegate（cocoa.py
  ``BrowserView.app.setDelegate_``），在其之前 setDelegate_ 会被覆盖。
* Windows：关机/注销/Restart Manager 触发的关窗（CloseReason 非
  UserClosing）不收托盘，直接放行退出链——否则系统报「应用阻止关机」，
  被 RM 强杀时后端进程树无人清理、残留占端口。
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import sys

from desktop import APP_NAME
from desktop.runtime import APP_DIR, EXE_DIR

log = logging.getLogger("dt.tray")

# macOS：第二个实例要求托盘把窗口叫回来的分布式通知名（发送端见
# single_instance._ask_tray_to_show）。
SHOW_NOTIFICATION = "com.thinkbuddy.desktop.show-window"


if sys.platform == "darwin":
    import AppKit
    import Foundation

    class TrayAppDelegate(AppKit.NSObject):
        """托盘用的 NSApplication delegate（替换 pywebview 的）。

        pyobjc 在类定义时就注册 ObjC 类，模块级定义一次即可；回调通过
        实例属性 ``_tray`` 指回 SystemTray。
        """

        def applicationShouldTerminate_(self, app):  # noqa: N802
            # Cmd+Q / 应用菜单「退出」：取消 AppKit 的硬退，改成下一轮跑
            # 正常退出链：销毁窗口 -> closed -> 停后端进程树。
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


class SystemTray:
    """托盘门面：Windows 通知区域 / macOS 菜单栏，统一走 pystray。"""

    def __init__(self, window, on_quit: Callable[[], None]) -> None:
        self._window = window
        self._on_quit = on_quit
        self._icon = None            # pystray.Icon
        self._exiting = False
        self._mac_delegate = None    # TrayAppDelegate 实例（防释放）
        self._mac_observer = None    # (notification center, token)
        self._win_hook = None        # (form, FormClosing handler)

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        # macOS 上 pystray 会直接操作 NSStatusBar，必须在主线程建图标：
        # main() 里是在 webview.start() 之前调用，正好是主线程。
        try:
            from PIL import Image
            import pystray

            # 优先从 EXE_DIR/assets/（Inno 安装器维护的稳定路径，升级时最可靠）
            # 读图标；fallback 到 APP_DIR（PyInstaller onefile 临时解压目录）。
            # 背景：升级安装时 exe 可能被杀软/文件锁阻塞覆盖，但 assets 目录
            # 下的普通 png/ico 总能成功覆盖；若只从 APP_DIR 读就会拿到旧 exe
            # 内嵌的旧图标，托盘显示旧图而任务栏（PE 资源 + 快捷方式）显示新图。
            icon_path = (EXE_DIR / "assets" / "icon.png")
            if not icon_path.exists():
                icon_path = APP_DIR / "assets" / "icon.png"
                log.debug("tray icon: fallback to APP_DIR (%s)", icon_path)
            with Image.open(icon_path) as image:
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

    def on_window_ready(self) -> None:
        """pywebview 建完原生窗口后安装平台钩子（见模块 docstring）。"""
        self._window.events.shown.wait(10)
        if sys.platform == "darwin":
            self._install_mac_hooks()
        elif sys.platform == "win32":
            self._install_windows_hooks()

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
        observer, self._mac_observer = self._mac_observer, None
        self._mac_delegate = None
        win_hook, self._win_hook = self._win_hook, None
        if observer is not None:
            try:
                observer[0].removeObserver_(observer[1])
            except Exception:  # noqa: BLE001
                log.exception("could not remove macOS activation observer")
        if win_hook is not None:
            try:
                win_hook[0].FormClosing -= win_hook[1]
            except Exception:  # noqa: BLE001
                log.exception("could not remove windows session-close hook")
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
        """替换 app delegate（Cmd+Q / Dock 唤回）+ 注册第二实例唤回通知。

        AppKit 不是线程安全的，整段派发到主线程执行；等过 ``events.shown``
        才调到这里，pywebview 自己的 delegate 已装完，不会被再覆盖。
        """
        try:
            from PyObjCTools import AppHelper

            def _install() -> None:
                delegate = TrayAppDelegate.alloc().init()
                delegate._tray = self
                AppKit.NSApplication.sharedApplication().setDelegate_(delegate)
                self._mac_delegate = delegate

                # 应用已在前台、窗口收着时，激活不会触发 DidBecomeActive，
                # 第二个实例改用显式通知（发送端见 single_instance）。
                center = Foundation.NSDistributedNotificationCenter.defaultCenter()
                token = center.addObserverForName_object_queue_usingBlock_(
                    SHOW_NOTIFICATION,
                    None,
                    Foundation.NSOperationQueue.mainQueue(),
                    lambda _note: self.show(),
                )
                self._mac_observer = (center, token)
                log.info("macOS app hooks installed")

            AppHelper.callAfter(_install)
        except Exception:  # noqa: BLE001
            log.exception("macOS app hooks unavailable; Cmd+Q will only hide the window")

    # -- Windows only ---------------------------------------------------- #
    def _install_windows_hooks(self) -> None:
        """关机/注销/Restart Manager 关窗（非 UserClosing）不收托盘，放行退出。

        pywebview 的 FormClosing 处理器先跑（一律 hide + Cancel）；本 handler
        追加在其后，把系统级关窗改回放行，走正常退出链（FormClosed ->
        events.closed -> 停后端进程树）。
        """
        try:
            from System.Windows.Forms import CloseReason

            def _pass_through(_sender, args) -> None:
                if args.CloseReason != CloseReason.UserClosing:
                    self.allow_exit()
                    args.Cancel = False

            form = self._window.native
            form.FormClosing += _pass_through
            self._win_hook = (form, _pass_through)
            log.info("windows session-close hook installed")
        except Exception:  # noqa: BLE001
            log.exception("windows session-close hook unavailable")


__all__ = ["SHOW_NOTIFICATION", "SystemTray"]
