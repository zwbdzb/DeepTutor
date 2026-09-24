"""临时调试：真实 SendInput 点击是否到达测试窗口。"""
import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:  # noqa: BLE001
    user32.SetProcessDPIAware()

from desktop import native_menu_backend as nmb
assert nmb.install_windows_shell_menu()
from System.Drawing import Point, Size
from System.Windows.Forms import Application, Cursor

from webview.menu import Menu, MenuAction

hits = {"down": 0, "click": 0, "paint": 0}

def noop() -> None:
    pass

form = nmb._WF.Form()
form.Text = "click-debug"
form.StartPosition = nmb._WF.FormStartPosition.Manual
form.Location = Point(80, 80)
form.Size = Size(900, 640)
form.TopMost = True
strip = nmb._build_menu_strip(
    form,
    [Menu("文件", items=[MenuAction("新建", noop)]),
     Menu("编辑", items=[MenuAction("复制", noop)])],
)

def on_down(s, e):
    hits["down"] += 1
    print(f"    strip.MouseDown btn={e.Button} at=({e.X},{e.Y})")

def on_click(s, e):
    hits["click"] += 1
    print(f"    strip.MouseClick btn={e.Button} at=({e.X},{e.Y})")

strip.MouseDown += on_down
strip.MouseClick += on_click

def pump(sec: float) -> None:
    end = time.monotonic() + sec
    while time.monotonic() < end:
        Application.DoEvents()
        time.sleep(0.03)

form.Show()
pump(0.5)
form.Activate()
pump(0.3)

hwnd = int(form.Handle.ToInt64())
fg = int(user32.GetForegroundWindow() or 0)
rect = nmb._RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
print(f"form hwnd=0x{hwnd:X} foreground=0x{fg:X} same={fg == hwnd}")
print(f"form rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")

item = strip.Items[0]
b = item.Bounds
scr = strip.RectangleToScreen(b)
cx, cy = int((scr.Left + scr.Right) / 2), int((scr.Top + scr.Bottom) / 2)
print(f"item0 bounds=({b.X},{b.Y},{b.Width},{b.Height}) click target=({cx},{cy})")

pt = nmb._POINT()
user32.SetCursorPos(cx, cy)
time.sleep(0.1)
user32.GetCursorPos(ctypes.byref(pt))
print(f"cursor after SetCursorPos=({pt.x},{pt.y})")
wnd_at = user32.WindowFromPoint(pt)
print(f"window at cursor=0x{wnd_at:X} (form=0x{hwnd:X})")

user32.mouse_event(0x0002, 0, 0, 0, 0)
time.sleep(0.08)
user32.mouse_event(0x0004, 0, 0, 0, 0)
pump(0.6)

dd = item.DropDown
print(f"hits={hits} dd.Visible={dd.Visible} Tag={dd.Tag}")
fg2 = int(user32.GetForegroundWindow() or 0)
print(f"foreground after=0x{fg2:X}")

form.Close()
pump(0.2)
