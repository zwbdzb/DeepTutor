"""单实例守卫回归测试（tools/test_single_instance.py）。

覆盖三件事——前两件都真实出过事（见 desktop/single_instance.py 模块 docstring）：

  A. 锁语义（**跨平台**：Windows=命名互斥体 / macOS=PID 文件）
     A1 第二个进程 acquire() 必须 False（挡住第二个实例）；
     A2 持有者退出后必须能**立刻**接手 —— 断言没有陈旧锁 / 陈旧 PID 状态。
        旧实现死在这里：Windows 上 os.kill(pid, 0) 实为 GenerateConsoleCtrlEvent，
        对无控制台的 GUI 壳抛 OSError(87)，被当成「进程已死」→ 删锁 → 放行；
        macOS 上该语义正确，本测试同时守护 POSIX 分支。

  B. 端到端（Windows）：已有实例 → 点快捷方式应「激活既有窗口」而不是再开一个
     B0 起一个「实例 1」进程：持有互斥体 + 开一个标题唯一的真实窗口；
     B1 把它的窗口最小化；
     B2 起「实例 2」进程调 **真实入口** acquire_or_activate()，断言返回
        ACTIVATED（而非 PRIMARY）并自行退出；
     B3 断言窗口被还原（非最小化）且窗口数仍为 1。

  C. 端到端（Windows）：无标题窗口——复刻真实壳形态
     壳窗口文本恒为空串（pywebview 不订阅 DocumentTitleChanged），激活必须
     靠**进程镜像名**匹配；故意传入必不匹配的标题，断言仍 ACTIVATED。

  D. 端到端（macOS）：PID 文件持有者活着 → NSRunningApplication 激活
     依赖 AppKit（pywebview Cocoa 后端必然随包），只能真机跑。

跑法（仓库 dev venv；B/C 需 Windows + WebView2，D 需 macOS）：
    .\\.venv\\Scripts\\python tools\\test_single_instance.py      # Windows
    python3 tools/test_single_instance.py                          # macOS
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop import APP_NAME, single_instance as si   # noqa: E402

CREATE_NO_WINDOW = 0x08000000
LOCK = str(Path(os.environ.get("TEMP", ".")) / "thinkbuddy_test_instance.lock")
TITLE = f"{APP_NAME} #singletest{os.getpid()}"        # 唯一标题，避免匹配到真实窗口

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ")
          + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        _FAILED.append(name)


def info(name: str, detail: str) -> None:
    print(f"  INFO  {name}  [{detail}]")


def _spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-u", "-c", code], cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _wait_line(proc: subprocess.Popen, want: str, timeout: float = 60.0) -> str:
    """读到以 want 开头的行（返回该行），超时/进程结束返回 ''。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                return ""
            continue
        print(f"        [child] {line.rstrip()}")
        if line.startswith(want):
            return line.strip()
    return ""


# --------------------------------------------------------------------------- #
# A. 互斥体语义                                                                #
# --------------------------------------------------------------------------- #
HOLDER = (
    "import time\n"
    "from desktop import single_instance as si\n"
    "print('ACQUIRE', si.acquire(r'%s'), flush=True)\n"
    "time.sleep(120)\n" % LOCK
)
PROBER = (
    "from desktop import single_instance as si\n"
    "print('ACQUIRE', si.acquire(r'%s'), flush=True)\n" % LOCK
)


def test_mutex() -> None:
    print("A. 互斥体语义（第二个进程必须被挡住）")
    holder = _spawn(HOLDER)
    try:
        check("A1 首个进程 acquire() == True",
              _wait_line(holder, "ACQUIRE True") != "",
              "持有者未能拿到互斥体")
        prober = _spawn(PROBER)
        out = prober.communicate(timeout=30)[0].strip()
        print(f"        [prober] {out}")
        check("A2 第二个进程 acquire() == False", out.endswith("ACQUIRE False"), out)
    finally:
        holder.kill()
        holder.wait(timeout=15)
    prober2 = _spawn(PROBER)
    out = prober2.communicate(timeout=30)[0].strip()
    print(f"        [prober2] {out}")
    check("A3 持有者退出后无陈旧状态，下一个进程 acquire() == True",
          out.endswith("ACQUIRE True"), out)


# --------------------------------------------------------------------------- #
# B. 端到端激活                                                                #
# --------------------------------------------------------------------------- #
DUMMY = (
    "from desktop import single_instance as si\n"
    "print('ACQUIRE', si.acquire(r'%s'), flush=True)\n"
    "import webview\n"
    "webview.create_window('%s', html='<h1>single-instance test window</h1>',"
    " width=520, height=320)\n"
    "print('WINDOW', flush=True)\n"
    "webview.start()\n" % (LOCK, TITLE)
)
SECOND = (
    "import sys\n"
    "from desktop import single_instance as si\n"
    "outcome = si.acquire_or_activate(r'%s', '%s', timeout=20.0)\n"
    "print('OUTCOME', outcome, flush=True)\n"
    "sys.exit(0 if outcome == si.ACTIVATED else 9)\n" % (LOCK, TITLE)
)

_u32_cache = None


def _u32():
    global _u32_cache
    if _u32_cache is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.IsIconic.restype = ctypes.c_bool
        u.IsIconic.argtypes = [ctypes.c_void_p]
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.EnumWindows.restype = ctypes.c_bool
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        _u32_cache = u
    return _u32_cache


def _window_count(title: str) -> int:
    found: list[int] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lp):
        buf = ctypes.create_unicode_buffer(256)
        _u32().GetWindowTextW(hwnd, buf, 256)
        if buf.value == title:
            found.append(int(hwnd))
        return True

    _u32().EnumWindows(proto(cb), None)
    return len(found)


def test_activate_e2e() -> None:
    print("B. 端到端：已有实例 + 第二次启动 → 激活既有窗口")
    dummy = _spawn(DUMMY)
    try:
        check("B0 实例 1 成为主实例（acquire() == True）",
              _wait_line(dummy, "ACQUIRE True", timeout=30) != "",
              "未能取得互斥体")
        deadline = time.monotonic() + 90
        hwnd = 0
        while time.monotonic() < deadline:
            hwnd = si.find_window(TITLE)
            if hwnd:
                break
            time.sleep(0.5)
        if not hwnd:
            check("B0 实例 1 窗口创建成功", False, "90s 内未出现标题为 %r 的窗口" % TITLE)
            return
        u32 = _u32()
        u32.ShowWindow(ctypes.c_void_p(hwnd), 6)      # SW_MINIMIZE
        time.sleep(1.0)
        check("B1 前置：实例 1 窗口已最小化",
              bool(u32.IsIconic(ctypes.c_void_p(hwnd))))

        second = _spawn(SECOND)
        rc = second.wait(timeout=60)
        line = _wait_line(second, "OUTCOME", timeout=1) or "(无输出)"
        print(f"        [child2] {line} rc={rc}")
        check("B2 实例 2 判定为 ACTIVATED（不开第二个窗口）",
              line.endswith("activated"), line)
        check("B2 实例 2 以 0 退出（静默退出，不弹提示框）", rc == 0, f"rc={rc}")

        time.sleep(0.5)
        check("B3 既有窗口被还原（非最小化）",
              not bool(u32.IsIconic(ctypes.c_void_p(hwnd))))
        check("B3 窗口数 == 1（没有多开）", _window_count(TITLE) == 1,
              f"count={_window_count(TITLE)}")
        fg = int(u32.GetForegroundWindow() or 0)
        if fg == hwnd:
            check("B4 既有窗口被置为前台", True)
        else:
            # 前台锁未授予时（测试进程不是前台进程的子进程）属预期；
            # 真实路径由 Explorer 拉起，具备前台权限。
            info("B4 前台未抢占",
                 f"fg=0x{fg:X} hwnd=0x{hwnd:X}（测试进程无前台锁权限时属预期）")
    finally:
        dummy.kill()
        dummy.wait(timeout=15)


# --------------------------------------------------------------------------- #
# C. 端到端激活（无标题窗口——复刻真实壳）                                        #
# --------------------------------------------------------------------------- #
# 真实壳的 WinForms 窗口文本恒为 ""（create_window("") + pywebview 不订阅
# DocumentTitleChanged），激活必须靠**进程镜像名**匹配而非标题。本场景故意
# 给 acquire_or_activate 传一个不可能匹配的标题，断言仍能激活。
DUMMY_UNTITLED = (
    "from desktop import single_instance as si\n"
    "print('ACQUIRE', si.acquire(r'%s'), flush=True)\n"
    "import webview\n"
    "webview.create_window('', html='<h1>untitled shell window</h1>',"
    " width=520, height=320)\n"
    "print('WINDOW', flush=True)\n"
    "webview.start()\n" % LOCK
)
SECOND_ANYTITLE = (
    "import sys\n"
    "from desktop import single_instance as si\n"
    "outcome = si.acquire_or_activate(r'%s', 'NoWindowWouldBeNamedThis', timeout=20.0)\n"
    "print('OUTCOME', outcome, flush=True)\n"
    "sys.exit(0 if outcome == si.ACTIVATED else 9)\n" % LOCK
)


def test_activate_untitled() -> None:
    print("C. 端到端：无标题窗口（真实壳形态）→ 靠进程镜像名激活")
    image = si._own_image_name()
    check("C0 本进程镜像名可解析", bool(image), f"image={image!r}")
    dummy = _spawn(DUMMY_UNTITLED)
    try:
        check("C1 实例 1 成为主实例（acquire() == True）",
              _wait_line(dummy, "ACQUIRE True", timeout=30) != "",
              "未能取得互斥体")
        deadline = time.monotonic() + 90
        hwnd = 0
        while time.monotonic() < deadline:
            hwnd = si._find_window_by_image(image)
            if hwnd:
                break
            time.sleep(0.5)
        if not hwnd:
            check("C1 实例 1 无标题窗口可按镜像名找到", False, "90s 内未命中")
            return
        check("C1 无标题窗口按镜像名找到（标题路径失效也能命中）", True,
              f"hwnd=0x{hwnd:X}")
        u32 = _u32()
        u32.ShowWindow(ctypes.c_void_p(hwnd), 6)      # SW_MINIMIZE
        time.sleep(1.0)

        second = _spawn(SECOND_ANYTITLE)
        rc = second.wait(timeout=60)
        line = _wait_line(second, "OUTCOME", timeout=1) or "(无输出)"
        print(f"        [child2] {line} rc={rc}")
        check("C2 实例 2 判定为 ACTIVATED（标题不匹配也靠镜像名激活）",
              line.endswith("activated"), line)
        check("C2 实例 2 以 0 退出", rc == 0, f"rc={rc}")
        time.sleep(0.5)
        check("C3 无标题窗口被还原（非最小化）",
              not bool(u32.IsIconic(ctypes.c_void_p(hwnd))))
    finally:
        dummy.kill()
        dummy.wait(timeout=15)


# --------------------------------------------------------------------------- #
# D. 端到端激活（macOS：NSRunningApplication 按 PID 激活，仅真机可跑）           #
# --------------------------------------------------------------------------- #
def test_activate_mac() -> None:
    if sys.platform != "darwin":
        print("D. macOS 激活测试 —— 跳过（仅 darwin 上运行）")
        return
    print("D. 端到端：macOS PID 文件持有者活着 → AppKit 激活既有实例")
    dummy = _spawn(DUMMY)
    try:
        check("D0 实例 1 成为主实例（acquire() == True）",
              _wait_line(dummy, "ACQUIRE True", timeout=30) != "",
              "未能取得 PID 文件锁")
        # AppKit 激活的是应用整体，不依赖窗口标题/句柄；窗口出现后激活必成
        deadline = time.monotonic() + 90
        activated = False
        while time.monotonic() < deadline:
            second = _spawn(SECOND)
            rc = second.wait(timeout=60)
            line = _wait_line(second, "OUTCOME", timeout=1) or "(无输出)"
            print(f"        [child] {line} rc={rc}")
            second = None
            if line.endswith("activated") and rc == 0:
                activated = True
                break
            time.sleep(1.0)      # 实例 1 可能还在启动，稍后重试
        check("D1 实例 2 判定为 ACTIVATED（NSRunningApplication 激活成功）",
              activated, "90s 内多次尝试均未激活")
    finally:
        dummy.kill()
        dummy.wait(timeout=15)


if __name__ == "__main__":
    print(f"python={sys.executable}\nlock={LOCK}\ntitle={TITLE}")
    test_mutex()                 # A：跨平台（Windows=互斥体 / POSIX=PID 文件）
    if os.name == "nt":
        test_activate_e2e()      # B：Windows 端到端
        test_activate_untitled() # C：Windows 无标题窗口（真实壳形态）
    else:
        test_activate_mac()      # D：macOS AppKit 激活
    print("\n=== " + ("ALL PASS" if not _FAILED else "FAILED: " + ", ".join(_FAILED)) + " ===")
    raise SystemExit(1 if _FAILED else 0)
