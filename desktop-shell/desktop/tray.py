"""System tray integrated with pywebview's event loop."""

from __future__ import annotations

from collections.abc import Callable
import logging
import sys

from desktop import APP_NAME
from desktop.runtime import APP_DIR

log = logging.getLogger("dt.tray")


class SystemTray:
    def __init__(self, window, on_quit: Callable[[], None]) -> None:
        self._window = window
        self._on_quit = on_quit
        self._icon = None
        self._exiting = False

    def start(self) -> None:
        # shortcut: Windows only; integrate Cocoa's shared loop before enabling macOS.
        if sys.platform != "win32":
            return
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

    def _setup(self, icon) -> None:
        try:
            if not self._exiting:
                icon.visible = True
        except Exception:  # noqa: BLE001
            log.exception("could not show system tray icon")

    def on_closing(self) -> bool:
        # Never hide the only window unless the tray can bring it back.
        if self._exiting or self._icon is None or not self._icon.visible:
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
        if icon is not None:
            try:
                icon.visible = False
                icon.stop()
            except Exception:  # noqa: BLE001
                log.exception("could not stop system tray")
