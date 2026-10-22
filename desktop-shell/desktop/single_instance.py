"""单实例守卫：同一登录会话里只允许一个 ThinkBuddy 桌面壳在跑。

第二次启动不再弹「已在运行」提示，而是把**已经打开的那个窗口**带到前台
（还原最小化 + 置顶），然后安静退出——即用户点桌面快捷方式时的预期行为。

机制选型
--------
* Windows：命名互斥体 ``Local\\ThinkBuddyDesktop.SingleInstance``。内核对象
  随进程消亡自动释放，天然没有「陈旧锁文件 / 陈旧 PID」问题；``Local\\``
  命名空间按登录会话隔离——同一台机器不同用户各跑一个实例互不干扰。
  已运行实例的窗口按**进程镜像名**查找后用 Win32 API 带到前台。
* POSIX（macOS）：PID 文件（``O_EXCL`` 创建 + ``os.kill(pid, 0)`` 探活），那里
  的 ``os.kill(pid, 0)`` 语义（只探活、不发信号）是正确的。已运行实例用
  AppKit ``NSRunningApplication`` 按 PID 激活（unhide + 带到前台）——AppKit
  随 pywebview 的 Cocoa 后端必然可用，无需新增依赖。
  注意：正常打包成 .app 后，Finder/Dock 双击在已运行时由 LaunchServices
  **原生激活**既有实例（第二个进程根本不会启动）；本守卫覆盖的是
  ``open -n``、终端直启二进制等绕过 LaunchServices 的路径，以及陈旧锁自愈。

**跨平台导入纪律**：本模块在 Windows 上被冻结进 exe、在 macOS 上打进 .app，
**顶层不得 import ``ctypes.wintypes``**——它在 POSIX 上直接抛
``ValueError``（VARIANT_BOOL 的 ``'v'`` 类型码只在 Windows 的 ctypes 注册）；
所有 Win32/AppKit 访问都延迟到各自平台的分支函数内。

历史 Bug ①（2026-10-19 修复，实测 3 个窗口 6 个进程共存）
----------------------------------------------------------
旧实现 Windows 上也用 PID 文件探活，但 **Windows 的 ``os.kill(pid, 0)`` 不是
探活**：sig 0 恰好等于 ``signal.CTRL_C_EVENT``，CPython 会调
``GenerateConsoleCtrlEvent(0, pid)``——对共享控制台的进程会真的广播一次
Ctrl+C，对无控制台的 GUI 壳（本应用冻结版、``creationflags`` 与窗口化进程）
直接失败 ``OSError(87, ERROR_INVALID_PARAMETER)``（实测 6 个 live pid 全部
如此）。旧代码把该异常笼统算进「进程已死」分支 → 删锁 → 放行新实例。
后果不止多开窗口：每个新实例在 ``DeepTutorProcess.start()`` 里都会
``_kill_stale_listeners`` 掉先前实例的 :8001/:3782 后端（同运行时目录，
判定为「本应用孤儿」），于是多个窗口共享同一份后端，最后一个窗口关闭时
又把这份共享后端一起停掉（装机 app.log 11:33:10 实锤：两条
``window closed; stopping deeptutor`` 相隔 1.2s，共享后端 pid 一致）。

历史 Bug ②（2026-10-19 二次修正，同日审查发现）
------------------------------------------------
激活逻辑初版按 ``FindWindowW(None, APP_NAME)`` 按标题找窗口，但壳的
WinForms 窗口文本是 ``create_window("")`` 传入的**空串**——本项目的
pywebview 版本不订阅 WebView2 的 ``DocumentTitleChanged``，页面
``<title>``（splash 的 ThinkBuddy / 应用页的 DeepTutor）怎么变，窗口文本
都永远不变。按标题找在真实应用里**永远落空**，激活只会降级成弹提示框；
回归测试 B 之所以通过，是因为测试自己造了一个同标题窗口（假阳性）。
修正：主判定改为**按进程镜像名**枚举顶层窗口（EnumWindows +
GetWindowThreadProcessId + QueryFullProcessImageNameW，过滤可见性）——
已运行实例的窗口必然属于同名 exe，与标题、页面阶段完全无关；标题匹配
降级为兜底（供测试进程等镜像名不同构的场景使用）。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("dt.single_instance")

# acquire_or_activate() 的三种结果
PRIMARY = "primary"          # 本进程成为唯一实例，继续启动
ACTIVATED = "activated"      # 已有实例，已把它的窗口带到前台 → 静默退出
UNAVAILABLE = "unavailable"  # 已有实例但窗口拉不起来（尚未创建 / 已卡死）→ 提示兜底

# --------------------------------------------------------------------------- #
# Windows：命名互斥体                                                          #
# --------------------------------------------------------------------------- #
_MUTEX_NAME = "Local\\ThinkBuddyDesktop.SingleInstance"
_ERROR_ALREADY_EXISTS = 183
_mutex_handle: int | None = None


def _acquire_mutex() -> bool:
    """创建命名互斥体；返回 False 表示已有实例持有它。"""
    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    err = ctypes.get_last_error()
    if not handle:
        # 极端情况（句柄耗尽/权限异常）：宁可放行启动，也不要让用户打不开软件
        log.error("CreateMutexW failed (err=%s); continuing without guard", err)
        return True
    if err == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _mutex_handle = handle       # 持有到进程退出；内核对象随之释放
    return True


# --------------------------------------------------------------------------- #
# POSIX：PID 文件                                                              #
# --------------------------------------------------------------------------- #
def _acquire_pid_file(lock_path: Path) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            pid = int(lock_path.read_text().strip())
            os.kill(pid, 0)          # POSIX：0 号信号只探活，不发信号
        except ProcessLookupError:
            pass                     # 持有者已死 → 陈旧锁，清掉重来
        except (ValueError, PermissionError):
            return False             # 内容不可解析 / 无权探测 → 视为仍在运行
        except OSError:
            pass                     # 其他探活失败 → 当作陈旧锁处理
        else:
            return False             # 探活成功 → 已有实例
        lock_path.unlink(missing_ok=True)
        return _acquire_pid_file(lock_path)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def acquire(lock_path: Path | str) -> bool:
    """返回 True 表示本进程成为唯一实例。"""
    if os.name == "nt":
        return _acquire_mutex()
    return _acquire_pid_file(Path(lock_path))


def release(lock_path: Path | str) -> None:
    """进程正常退出时的显式收尾（互斥体也可由内核在进程消亡时释放）。"""
    global _mutex_handle
    if os.name == "nt":
        if _mutex_handle:
            try:
                ctypes.WinDLL("kernel32").CloseHandle(
                    ctypes.c_void_p(_mutex_handle))
            except Exception:  # noqa: BLE001
                pass
            _mutex_handle = None
        return
    try:
        Path(lock_path).unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Windows：把已运行实例的窗口带到前台                                           #
# --------------------------------------------------------------------------- #
_ASFW_ANY = -1              # AllowSetForegroundWindow：授权任意进程置顶
_SW_SHOW = 5
_SW_RESTORE = 9
_FLASHW_ALL = 3             # FLASHW_CAPTION | FLASHW_TRAY
_user32 = None


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hwnd", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("uCount", ctypes.c_uint),
        ("dwTimeout", ctypes.c_uint),
    ]


def _u32():
    """延迟绑定 user32 原型（非 Windows 平台不会走到这里）。"""
    global _user32
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.FindWindowW.restype = ctypes.c_void_p
        u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        u.IsWindow.restype = ctypes.c_bool
        u.IsWindow.argtypes = [ctypes.c_void_p]
        u.IsIconic.restype = ctypes.c_bool
        u.IsIconic.argtypes = [ctypes.c_void_p]
        u.ShowWindow.restype = ctypes.c_bool
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.SetForegroundWindow.restype = ctypes.c_bool
        u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u.AttachThreadInput.restype = ctypes.c_bool
        u.AttachThreadInput.argtypes = [
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool,
        ]
        u.GetWindowThreadProcessId.restype = ctypes.c_ulong
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.AllowSetForegroundWindow.restype = ctypes.c_bool
        u.AllowSetForegroundWindow.argtypes = [ctypes.c_ulong]
        u.FlashWindowEx.argtypes = [ctypes.POINTER(_FLASHWINFO)]
        _user32 = u
    return _user32


def _own_image_name() -> str:
    """当前进程可执行文件名（小写、纯文件名），用于按进程归属找窗口。"""
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetModuleFileNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong,
        ]
        buf = ctypes.create_unicode_buffer(1024)
        if k.GetModuleFileNameW(None, buf, 1024):
            return buf.value.rsplit("\\", 1)[-1].lower()
    except Exception:  # noqa: BLE001
        log.exception("GetModuleFileNameW failed")
    return ""


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("showCmd", ctypes.c_uint),
        ("ptMinPosition", ctypes.c_long * 2),
        ("ptMaxPosition", ctypes.c_long * 2),
        ("rcNormalPosition", ctypes.c_long * 4),
    ]


def _restored_area(hwnd: int) -> int:
    """窗口还原态面积：最小化时 GetWindowRect 给的是图标尺寸（~158x26），
    必须读 GetWindowPlacement.rcNormalPosition 才是真实还原大小。"""
    u = _u32()
    try:
        u.GetWindowPlacement.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_WINDOWPLACEMENT),
        ]
        wp = _WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
        if u.GetWindowPlacement(ctypes.c_void_p(hwnd), ctypes.byref(wp)):
            r = wp.rcNormalPosition
            return abs((r[2] - r[0]) * (r[3] - r[1]))
    except Exception:  # noqa: BLE001
        log.exception("GetWindowPlacement failed (hwnd=0x%X)", hwnd)
    try:
        rect = _RECT()
        u.GetWindowRect.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_RECT),
        ]
        if u.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return abs((rect.right - rect.left) * (rect.bottom - rect.top))
    except Exception:  # noqa: BLE001
        log.exception("GetWindowRect failed (hwnd=0x%X)", hwnd)
    return 0


def _find_window_by_image(image: str) -> int:
    """枚举顶层窗口，找「属于镜像名为 ``image`` 的其他进程」的主窗口。

    壳窗口文本恒为空串（见模块 docstring 历史 Bug ②），标题不可依赖；
    进程归属才是稳定判据。同一进程除主窗体外还可能有**微型辅助窗口**
    （实测 pywebview 激活瞬间会冒出 65x50 的可见小窗），且 Z 序不定——
    因此不能命中即停，而是收集全部命中窗口（包括隐藏到托盘的窗口），按**还原态面积**取最大
    （主窗体 1280x860 恒大于任何辅助窗口）。把本进程排除在外（守卫阶段
    本进程尚无窗口，纯属保险）。
    """
    u = _u32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcessId.restype = ctypes.c_ulong
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [
        ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong,
    ]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    u.EnumWindows.restype = ctypes.c_bool
    u.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p),
        ctypes.c_void_p,
    ]

    me = kernel32.GetCurrentProcessId()
    owner_pid = ctypes.c_ulong()
    candidates: list[int] = []

    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lp):
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        pid = owner_pid.value
        if not pid or pid == me:
            return True
        proc = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not proc:
            return True        # 跨提权场景读不到镜像名，放弃该窗口
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(
                proc, 0, buf, ctypes.byref(size)
            ) and buf.value.rsplit("\\", 1)[-1].lower() == image:
                candidates.append(int(hwnd))
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(proc))
        return True

    u.EnumWindows(proto(cb), None)
    if not candidates:
        return 0
    if len(candidates) == 1:
        return candidates[0]
    best = max(candidates, key=_restored_area)
    log.info(
        "image match: %d candidate windows %s; picked 0x%X (largest restored area)",
        len(candidates), [hex(h) for h in candidates], best,
    )
    return best


def find_window(title: str) -> int:
    """找已运行实例的顶层窗口，找不到返回 0。

    主判定：按进程镜像名（本 exe 名）——窗口必然属于同名 exe，与标题、
    页面阶段（splash/门控/应用）无关。兜底：按标题 ``FindWindowW``，供
    镜像名不同构的场景（如回归测试里 python.exe 起的假想实例）使用。
    """
    if os.name != "nt":
        return 0
    image = _own_image_name()
    if image:
        try:
            hwnd = _find_window_by_image(image)
            if hwnd:
                log.info("found instance window by image name %r hwnd=0x%X", image, hwnd)
                return hwnd
        except Exception:  # noqa: BLE001
            log.exception("find_window_by_image failed")
    try:
        return int(_u32().FindWindowW(None, title) or 0)
    except Exception:  # noqa: BLE001
        log.exception("FindWindowW failed")
        return 0


def _bring_to_front(hwnd: int) -> bool:
    u = _u32()
    kernel32 = ctypes.windll.kernel32
    try:
        # 本进程由用户点击快捷方式启动（前台进程的子进程），有资格把置顶权
        # 授予任意进程；SetForegroundWindow 的前台锁因此不会拦住这次调用。
        u.AllowSetForegroundWindow(_ASFW_ANY)
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, _SW_RESTORE)
        else:
            u.ShowWindow(hwnd, _SW_SHOW)
        if u.GetForegroundWindow() != hwnd:
            # 前台锁兜底：临时把本线程挂到当前前台线程上再置顶，最后摘开
            fg_tid = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
            cur_tid = kernel32.GetCurrentThreadId()
            attached = bool(
                fg_tid and fg_tid != cur_tid
                and u.AttachThreadInput(cur_tid, fg_tid, True)
            )
            u.SetForegroundWindow(hwnd)
            if attached:
                u.AttachThreadInput(cur_tid, fg_tid, False)
        if u.GetForegroundWindow() != hwnd:
            info = _FLASHWINFO(
                ctypes.sizeof(_FLASHWINFO), hwnd, _FLASHW_ALL, 3, 0,
            )
            u.FlashWindowEx(ctypes.byref(info))   # 仍未置顶 → 任务栏闪一下
        return True
    except Exception:  # noqa: BLE001
        log.exception("activate window failed (hwnd=0x%X)", hwnd)
        return False


def activate_existing_window(title: str, timeout: float = 3.0,
                             poll: float = 0.2) -> bool:
    """把已运行实例的窗口带到前台；超时仍未出现窗口则返回 False。

    ``timeout`` 内轮询是必要的：实例可能刚被拉起、窗口尚未创建（引导阶段），
    或窗口句柄还在 WinForms 初始化中。
    """
    if os.name != "nt":
        return False
    deadline = time.monotonic() + timeout
    while True:
        hwnd = find_window(title)
        if hwnd:
            log.info("activating existing window hwnd=0x%X", hwnd)
            return _bring_to_front(hwnd)
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


# --------------------------------------------------------------------------- #
# POSIX：把已运行实例带到前台（macOS AppKit）                                    #
# --------------------------------------------------------------------------- #
def _read_pid(lock_path: Path | str) -> int | None:
    """读取 PID 文件内容；不存在/不可解析返回 None。"""
    try:
        return int(Path(lock_path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """POSIX 探活：``os.kill(pid, 0)`` 只探活不发信号（语义仅 POSIX 正确）。"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # EPERM = 进程存在但属其他用户 → 视为活着
    except OSError:
        return False


def activate_existing_pid(pid: int) -> bool:
    """macOS：用 AppKit 把 PID 对应的已运行实例带到前台（unhide + 激活）。

    AppKit 随 pywebview 的 Cocoa 后端必然随包分发（打包必然含 pyobjc），
    这里仍延迟导入 + 失败兜底：万一不可用，轮询会走到 UNAVAILABLE 提示框。
    ``activateWithOptions_(NSApplicationActivateIgnoringOtherApps)`` 在
    macOS 14+ 标记为 deprecated 但仍生效（替代 API ``activate()`` 要 14+ 才有），
    全版本可用性优先。激活对象是**应用**而非窗口——与 Dock 点击语义一致。
    """
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSRunningApplication
    except Exception:  # noqa: BLE001
        log.exception("AppKit import failed; cannot activate running instance")
        return False
    try:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            # 非GUI 应用（理论不可达：壳必有 NSApplication）→ 交给轮询兜底
            log.info("pid %s is not a running GUI application", pid)
            return False
        app.unhide()           # Cmd+H 隐藏态先恢复；可见时 no-op
        # NSApplicationActivateIgnoringOtherApps = 1 << 1：
        # 即便当前焦点在别的应用也强制激活成功
        ok = bool(app.activateWithOptions_(1 << 1))
        log.info("activate existing instance pid=%s via NSRunningApplication -> %s",
                 pid, ok)
        return ok
    except Exception:  # noqa: BLE001
        log.exception("activate existing pid=%s failed", pid)
        return False


def acquire_or_activate(lock_path: Path | str, window_title: str,
                        timeout: float = 12.0) -> str:
    """启动入口，返回三种结果之一（见下面的常量）。

    轮询里同时做两件事，覆盖两类真实时序：

      * ``acquire()`` —— 上一个实例正在退出（更新重启 / 刚关窗），互斥体
        （Windows）/ PID 文件陈旧锁（macOS）马上会被释放/自愈，这里能立刻
        接手，不必让用户等；
      * 激活既有实例 —— 实例活着但可能还在启动，Windows 等窗口句柄出现
        （按镜像名找），macOS 直接按 PID 激活应用。
    """
    if acquire(lock_path):
        return PRIMARY

    deadline = time.monotonic() + timeout

    if os.name == "nt":
        while True:
            remaining = deadline - time.monotonic()
            if activate_existing_window(
                window_title, timeout=min(2.0, max(0.2, remaining))
            ):
                return ACTIVATED
            if acquire(lock_path):
                log.info("previous instance exited; continuing as primary")
                return PRIMARY
            if time.monotonic() >= deadline:
                return UNAVAILABLE
            time.sleep(0.4)

    # POSIX（macOS/Linux）：持有者活着 → 激活它的应用；死了 → 锁自愈后接手
    while True:
        pid = _read_pid(lock_path)
        if pid and _pid_alive(pid) and activate_existing_pid(pid):
            return ACTIVATED
        if acquire(lock_path):
            log.info("previous instance exited; continuing as primary")
            return PRIMARY
        if time.monotonic() >= deadline:
            return UNAVAILABLE
        time.sleep(0.4)
