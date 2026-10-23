"""Tray checks; add --headed for a real WebView + tray smoke test
(Windows notification area, or the macOS menu bar)."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.tray import SystemTray  # noqa: E402


class TrayTests(unittest.TestCase):
    def test_close_without_a_visible_tray_exits(self):
        window = Mock()
        tray = SystemTray(window, Mock())
        self.assertTrue(tray.on_closing())
        tray._icon = Mock(visible=False)
        self.assertTrue(tray.on_closing())
        window.hide.assert_not_called()

    def test_close_hides_but_explicit_quit_allows_destroy(self):
        window = Mock()
        tray = SystemTray(window, Mock())
        tray._icon = Mock(visible=True)
        self.assertFalse(tray.on_closing())
        window.hide.assert_called_once()
        tray.quit()
        self.assertTrue(tray.on_closing())
        tray._on_quit.assert_called_once()

    def test_hide_failure_allows_exit(self):
        tray = SystemTray(Mock(), Mock())
        tray._icon = Mock(visible=True)
        tray._window.hide.side_effect = RuntimeError("window unavailable")
        with self.assertLogs("dt.tray", level="ERROR"):
            self.assertTrue(tray.on_closing())

    @unittest.skipUnless(sys.platform == "win32", "Windows tray")
    def test_tray_start_failure_keeps_window_closable(self):
        tray = SystemTray(Mock(), Mock())
        with patch("pystray.Icon", side_effect=RuntimeError("tray unavailable")):
            with self.assertLogs("dt.tray", level="ERROR"):
                tray.start()
        self.assertTrue(tray.on_closing())

    def test_api_quit_bypasses_close_to_tray(self):
        from desktop import main

        window = Mock()
        tray = SystemTray(window, main.Api.quit)
        tray._icon = Mock(visible=True)
        with patch.object(main, "webview_windows", [window]):
            with patch.dict(main._shared, {"tray": tray}):
                main.Api.quit()
        window.destroy.assert_called_once()
        self.assertTrue(tray.on_closing())

    @unittest.skipUnless(sys.platform == "win32", "Windows session-close hook")
    def test_session_close_passes_through_but_user_close_hides(self):
        import clr  # pythonnet：真实运行时由 pywebview 的 winforms 后端加载

        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import CloseReason

        window = Mock()
        window.native = MagicMock()
        tray = SystemTray(window, Mock())
        tray._install_windows_hooks()
        self.assertIsNotNone(tray._win_hook)
        handler = tray._win_hook[1]

        # 关机/注销/Restart Manager：放行关窗并置退出标志（走正常退出链）
        shutdown = Mock(CloseReason=CloseReason.WindowsShutDown, Cancel=True)
        handler(None, shutdown)
        self.assertFalse(shutdown.Cancel)
        self.assertTrue(tray._exiting)

        # 用户点 X：handler 不干预（收不收托盘由 on_closing 链路决定）
        tray2 = SystemTray(window, Mock())
        tray2._install_windows_hooks()
        user_close = Mock(CloseReason=CloseReason.UserClosing, Cancel=True)
        tray2._win_hook[1](None, user_close)
        self.assertTrue(user_close.Cancel)
        self.assertFalse(tray2._exiting)

        tray2.stop()  # 退订 FormClosing 并清理状态（不抛异常即可）
        self.assertIsNone(tray2._win_hook)


class MacTrayTests(unittest.TestCase):
    """macOS 分支：只验证「多出来的那两件小事」与退出语义（不加载 AppKit）。"""

    def test_start_reuses_pystray_without_touching_platform_hooks(self):
        tray = SystemTray(Mock(), Mock())
        icon = Mock()
        fake = Mock(Icon=Mock(return_value=icon))
        with patch("desktop.tray.sys.platform", "darwin"), patch.dict(
            sys.modules, {"pystray": fake}
        ), patch.object(tray, "_install_mac_hooks") as hooks:
            tray.start()
        # P1 时序修复：平台钩子改由 on_window_ready 装（等 events.shown），
        # start() 时 pywebview 还没建窗口，装了也会被覆盖。
        hooks.assert_not_called()
        icon.run_detached.assert_called_once_with(tray._setup)

    def test_window_ready_installs_platform_hooks(self):
        for platform, hook in (("darwin", "_install_mac_hooks"),
                               ("win32", "_install_windows_hooks")):
            with self.subTest(platform=platform):
                tray = SystemTray(Mock(), Mock())
                with patch("desktop.tray.sys.platform", platform), patch.object(
                    tray, hook
                ) as install:
                    tray.on_window_ready()
                install.assert_called_once()

    def test_menu_quit_allows_destroy_and_disposes_the_item(self):
        tray = SystemTray(Mock(), Mock())
        tray._icon = Mock(visible=True)
        tray.quit()
        tray._on_quit.assert_called_once()
        self.assertTrue(tray.on_closing())          # 退出后关窗不再拦截
        tray.stop()
        self.assertIsNone(tray._icon)
        tray.stop()                                  # 幂等

    def test_show_restores_the_window(self):
        tray = SystemTray(Mock(), Mock())
        tray.show()
        tray._window.show.assert_called_once()
        tray._window.restore.assert_called_once()

    def test_stop_hides_item_but_leaves_pywebviews_loop_alone(self):
        tray = SystemTray(Mock(), Mock())
        icon, center = Mock(visible=True), Mock()
        tray._icon, tray._mac_observer = icon, (center, object())
        with patch("desktop.tray.sys.platform", "darwin"):
            tray.stop()
        icon.stop.assert_not_called()               # 不向 NSApp 发退出信号
        center.removeObserver_.assert_called_once()
        self.assertIsNone(tray._mac_observer)


def headed() -> None:
    from desktop import main, single_instance
    import webview

    if sys.platform == "win32":
        main.install_windows_shell_menu()
    window = webview.create_window(
        "",
        html="<h1>Tray smoke test</h1>",
        width=520,
        height=320,
        menu=main.build_native_menu(main.Api),
    )
    tray = SystemTray(window, main.Api.quit)
    proc = Mock()
    main.webview_windows.append(window)
    main._shared.update(tray=tray, proc=proc)
    window.events.closing += tray.on_closing
    window.events.closed += main._on_closed
    errors = []

    def wait_for(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError("timed out waiting for GUI state")

    def check_window():
        from System.Windows.Forms import CloseReason, FormClosingEventArgs

        try:
            tray.on_window_ready()  # 平台钩子（等 events.shown）
            wait_for(lambda: tray._icon is not None and tray._icon.visible)
            wait_for(window.events.shown.is_set)
            hwnd = int(window.native.Handle.ToInt64())
            u32 = single_instance._u32()
            window.destroy()
            wait_for(lambda: not u32.IsWindowVisible(hwnd))
            assert not window.events.closed.is_set(), "close must keep the window alive"
            proc.stop.assert_not_called()
            print("PASS: close hides window and preserves backend", flush=True)

            # A second process must discover the hidden, untitled main window.
            probe = (
                "from desktop import single_instance as si; "
                "h = si._find_window_by_image(si._own_image_name()); "
                f"assert h == {hwnd}, hex(h); "
                "assert si._bring_to_front(h)"
            )
            subprocess.run(
                [sys.executable, "-c", probe],
                check=True,
                timeout=15,
                cwd=Path(__file__).resolve().parents[1],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            wait_for(lambda: bool(u32.IsWindowVisible(hwnd)))
            print("PASS: second instance restores hidden window", flush=True)

            window.destroy()
            wait_for(lambda: not u32.IsWindowVisible(hwnd))
            tray._icon()  # Real pystray default menu action.
            wait_for(lambda: bool(u32.IsWindowVisible(hwnd)))
            print("PASS: tray default action restores window", flush=True)

            # 关机/注销/Restart Manager（P2 回归）：系统级关窗不收托盘，
            # 放行后走正常退出链（destroy -> closed -> 停后端进程树）。
            assert tray._win_hook is not None, "session-close hook not installed"
            args = FormClosingEventArgs(CloseReason.WindowsShutDown, True)
            tray._win_hook[1](None, args)
            assert args.Cancel is False, "shutdown close must pass through"
            print("PASS: shutdown/logoff close passes through", flush=True)
            window.destroy()  # _exiting 已置位：真关，走完整退出链
        except Exception as exc:
            errors.append(exc)
            tray.allow_exit()
            window.destroy()

    def check_window_mac():
        import AppKit

        def visible():
            try:
                return bool(window.native.isVisible())
            except Exception:  # noqa: BLE001
                return False

        try:
            tray.on_window_ready()  # 平台钩子（等 events.shown）
            wait_for(lambda: tray._icon is not None and tray._icon.visible)
            wait_for(visible)
            print("PASS: menu bar item is installed", flush=True)

            window.destroy()
            wait_for(lambda: not visible())
            assert not window.events.closed.is_set(), "close must keep the window alive"
            proc.stop.assert_not_called()
            print("PASS: close hides window and preserves backend", flush=True)

            tray.show()
            wait_for(visible)
            print("PASS: tray open restores window", flush=True)

            # P1 回归：app delegate 必须是托盘的（pywebview 建窗口时会覆盖
            # 抢先安装的 delegate，所以钩子改在 on_window_ready 里装）。
            app = AppKit.NSApplication.sharedApplication()
            wait_for(lambda: getattr(app.delegate(), "_tray", None) is tray)
            print("PASS: tray app delegate survives pywebview startup", flush=True)

            # Dock 唤回：窗口收着时点 Dock 图标（pywebview 未实现该回调）。
            window.destroy()
            wait_for(lambda: not visible())
            app.delegate().applicationShouldHandleReopen_hasVisibleWindows_(app, False)
            wait_for(visible)
            print("PASS: dock reopen restores window", flush=True)

            # Cmd+Q / 应用菜单「退出」：delegate 取消硬退，改走托盘退出链
            # （callAfter(tray.quit) -> destroy -> closed -> 停后端进程树）。
            assert not app.delegate().applicationShouldTerminate_(app)
            print("PASS: Cmd+Q routes through the tray quit chain", flush=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            tray.allow_exit()
            window.destroy()

    try:
        tray.start()
        webview.start(check_window_mac if sys.platform == "darwin" else check_window)
        wait_for(lambda: proc.stop.call_count == 1)
        assert tray._icon is None and tray._mac_observer is None, (
            "quit must dispose the tray"
        )
        if errors:
            raise errors[0]
        print("PASS: tray exit destroys window and stops backend", flush=True)
    finally:
        tray.stop()
        main.webview_windows.clear()
        main._shared.clear()


if __name__ == "__main__":
    if "--headed" in sys.argv:
        headed()
    else:
        unittest.main()
