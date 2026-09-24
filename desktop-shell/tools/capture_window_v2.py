"""Capture the running EduBuddy window (DPI-aware, PID-tree matched).

铁律（skills/windows-gui-evidence）：
  1. 截图前声明 PER_MONITOR_AWARE_V2，GetWindowRect 才给物理坐标；
  2. 窗口按进程树匹配，不按标题瞎抓（z 序第一个同名窗口可能是别人的）；
  3. WebView2 是 GPU 合成窗口，PrintWindow 需带 PW_RENDERFULLCONTENT。
"""
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    user32.SetProcessDPIAware()

PW_RENDERFULLCONTENT = 0x00000002


def descendants(pid: int) -> set[int]:
    """CreateToolhelp32Snapshot 遍历整棵进程树。"""
    TH32CS_SNAPPROCESS = 0x2
    kids = {pid: set()}

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    allp: dict[int, int] = {}
    if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
        while True:
            allp[entry.th32ProcessID] = entry.th32ParentProcessID
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    kernel32.CloseHandle(snap)
    tree = {pid}
    changed = True
    while changed:
        changed = False
        for c, p in allp.items():
            if p in tree and c not in tree:
                tree.add(c)
                changed = True
    return tree


def find_windows(title: str) -> list[tuple[int, int]]:
    """枚举可见顶层窗口，返回 (hwnd, pid) 中标题匹配者。"""
    result: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value == title:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            result.append((hwnd, pid.value))
        return True

    user32.EnumWindows(cb, 0)
    return result


def main() -> int:
    root_pid = int(sys.argv[1])
    out = Path(sys.argv[2])
    tree = descendants(root_pid)
    wins = [(h, p) for h, p in find_windows("EduBuddy") if p in tree]
    if len(wins) != 1:
        print(f"EVIDENCE-FAIL: expected 1 window in pid-tree {sorted(tree)}, got {wins}")
        return 2
    hwnd, _ = wins[0]
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    L, T, R, B = rect.left, rect.top, rect.right, rect.bottom
    W, H = R - L, B - T
    print("rect:", L, T, R, B, f"({W}x{H})")

    gdi32 = ctypes.windll.gdi32
    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, W, H)
    gdi32.SelectObject(hdc_mem, bmp)
    ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
    time.sleep(0.2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight = W, -H
    bmi.biPlanes, bmi.biBitCount = 1, 32
    bmi.biCompression = 0  # BI_RGB
    buf = ctypes.create_string_buffer(W * H * 4)
    gdi32.GetDIBits(hdc_mem, bmp, 0, H, buf, ctypes.byref(bmi), 0)

    from PIL import Image
    img = Image.frombuffer("RGBA", (W, H), buf.raw, "raw", "BGRA", 0, 1)
    img = img.convert("RGB")
    img.save(out)
    print("SAVED" if ok else "PRINTWINDOW-FALSE", out, img.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
