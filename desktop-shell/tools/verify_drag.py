# -*- coding: utf-8 -*-
"""验证菜单栏空白区拖动窗口是否生效。

原理：用 SendInput 在窗口顶部菜单栏空白区（客户区中部、避开左侧菜单项
与右侧窗口按钮/账号区）按下左键 → 分步移动 → 抬起，对比 GetWindowRect
前后差值。若拖拽链路（MouseDown → ReleaseCapture → WM_NCLBUTTONDOWN
HTCAPTION）工作正常，窗口矩形必然发生位移。

用法：python verify_drag.py [--pid <pid>]
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SM_XV = 76
SM_YV = 77
SM_CXV = 78
SM_CYV = 79


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


MOVE = 0x0001
ABS = 0x8000
LEFTDOWN = 0x0002
LEFTUP = 0x0004

INPUT_MOUSE = 0


def send_mouse(flags, dx=0, dy=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx, dy, 0, flags, 0, None)
    n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if n != 1:
        raise OSError("SendInput failed")


def px_to_abs(x, y):
    vx = user32.GetSystemMetrics(SM_XV)
    vy = user32.GetSystemMetrics(SM_YV)
    vw = user32.GetSystemMetrics(SM_CXV)
    vh = user32.GetSystemMetrics(SM_CYV)
    ax = int((x - vx) * 65535 / max(1, vw - 1))
    ay = int((y - vy) * 65535 / max(1, vh - 1))
    return ax, ay


def abs_move(x, y):
    ax, ay = px_to_abs(x, y)
    send_mouse(MOVE | ABS, ax, ay)


def get_rect(hwnd):
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise OSError("GetWindowRect failed")
    return r


def find_windows(pid=None, title_contains=None):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if pid is not None:
            wpid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(h, ctypes.byref(wpid))
            if wpid.value != pid:
                return True
        if title_contains:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, buf, 256)
            if title_contains not in buf.value:
                return True
        if user32.IsWindowVisible(h):
            found.append(h)
        return True

    user32.EnumWindows(cb, 0)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--title", default="EduBuddy")
    args = ap.parse_args()

    wins = find_windows(pid=args.pid, title_contains=args.title)
    if not wins:
        print("FAIL: no matching visible window")
        sys.exit(2)
    hwnd = wins[0]

    # 确保非最大化（最大化状态下拖动语义是"还原+移动"，干扰判定）
    if user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, 6)  # SW_RESTORE
        time.sleep(0.6)

    r0 = get_rect(hwnd)
    w = r0.right - r0.left
    h = r0.bottom - r0.top
    print(f"window hwnd=0x{hwnd:X} rect=({r0.left},{r0.top})-({r0.right},{r0.bottom}) {w}x{h}")

    # 拖拽起点：客户区中部偏左（45% 宽），纵向 20px 处 —— 菜单栏空白区。
    sx = r0.left + int(w * 0.45)
    sy = r0.top + 20
    tx, ty = sx + 320, sy + 80

    abs_move(sx, sy)
    time.sleep(0.25)
    send_mouse(LEFTDOWN)
    time.sleep(0.25)

    steps = 24
    for i in range(1, steps + 1):
        x = sx + (tx - sx) * i // steps
        y = sy + (ty - sy) * i // steps
        abs_move(x, y)
        time.sleep(0.02)

    time.sleep(0.15)
    send_mouse(LEFTUP)
    time.sleep(0.5)

    r1 = get_rect(hwnd)
    dx = r1.left - r0.left
    dy = r1.top - r0.top
    print(f"after drag  rect=({r1.left},{r1.top})-({r1.right},{r1.bottom})")
    print(f"delta dx={dx} dy={dy}")

    if abs(dx) > 50 and abs(dy) > 20:
        print("PASS: window moved with the drag")
    else:
        print("FAIL: window did not move — drag chain broken")
        sys.exit(1)


if __name__ == "__main__":
    main()
