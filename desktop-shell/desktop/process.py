"""Manage the `deeptutor start` subprocess.

端口策略（2026-10-20 定）：壳托管模式（DEEPTUTOR_SHELL_MANAGED=1）下后端口由
launcher 让内核 bind(:0) 分配（ephemeral，零冲突），前端口由壳在 spawn 前
bind 探测选定（3782 固定优先、被外部占用时向上回退）并经 FRONTEND_PORT env
注入；实际后端口从子进程 banner 行回读，供 stop() 的端口清扫兜底。
"""
from __future__ import annotations

import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
import ctypes
import struct
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib import request as urlrequest

from desktop import __version__

log = logging.getLogger("dt.process")

# Invocation for a deeptutor that has no console entry-point .exe (e.g. an
# embedded-runtime python). `deeptutor` may be a bare path or this list.
EMBED_INVOKE = (
    "from deeptutor_cli.main import main; raise SystemExit(main())"
)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEFAULT_BACKEND_PORT = 8001  # legacy sweep target; the live port is OS-assigned in shell-managed mode
DEFAULT_FRONTEND_PORT = 3782
FRONTEND_PATH = "/"  # health-probe path on the frontend server
SHELL_MANAGED_ENV = "DEEPTUTOR_SHELL_MANAGED"
# 后端 banner 行回读：zh "后端       http://127.0.0.1:PORT" / en "Backend  ..."
BACKEND_URL_PATTERN = re.compile(r"http://127\.0\.0\.1:(\d+)")

# 探活专用无代理 opener（教训同 deeptutor/runtime/launcher.py）：urlopen 默认
# 读系统代理环境变量，Clash/公司代理开启且 ProxyOverride 不含 127.0.0.1 时，
# loopback 探活会被代理劫持、必然失败，健康后端被误判死、180s 超时后弹
# "启动失败"横幅——探活绝不能漏进系统代理。
_LOOPBACK_OPENER = urlrequest.build_opener(urlrequest.ProxyHandler({}))

# 已知异常退出码的机制解读（取证用）。键一律按无符号 32 位归一。
# 依据：2026-10-20 用户机 app.log 实证 + Windows NTSTATUS 文档。
_EXIT_CODE_HINTS = {
    0x00000001: "进程被外部 TerminateProcess 结束（taskkill /F、任务管理器、"
                "安全软件拦截）或自行 process.exit(1)；进程自身零输出 = 外杀特征",
    0x40010004: "DBG_TERMINATE_PROCESS：系统关机/控制台控制事件强制终止"
                "（0xC000013A CTRL_C 同族），常伴随整机重启",
    0xC000013A: "STATUS_CONTROL_C_EXIT：收到 CTRL+C / CTRL_BREAK 控制台事件",
    0xC0000005: "STATUS_ACCESS_VIOLATION：进程自身崩溃（内存访问越界）",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN：进程自身崩溃（栈溢出保护触发）",
}


def describe_exit_code(code: int) -> str:
    """Human-readable mechanism hint for an abnormal Windows exit code."""
    try:
        normalized = int(code) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return ""
    return _EXIT_CODE_HINTS.get(normalized, "")


def _as_cmd(deeptutor) -> list[str]:
    """Normalize a deeptutor executable into an argv prefix."""
    if isinstance(deeptutor, (str, Path)):
        return [str(deeptutor)]
    return [str(arg) for arg in deeptutor]


def _decode_line(raw: bytes) -> str:
    """Decode a stdout chunk; deeptutor emits GBK on zh-CN Windows consoles."""
    try:
        return raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk", errors="replace").rstrip("\r\n")
        except Exception:  # noqa: BLE001
            return raw.decode("latin-1", errors="replace").rstrip("\r\n")


def _listener_pid(port: int) -> int | None:
    """Find the PID listening on a TCP port using the Windows TCP table."""
    if os.name != "nt":
        return None
    AF_INET = 2
    TCP_TABLE_OWNER_PID_LISTENER = 3
    MIB_TCP_STATE_LISTEN = 2
    size = ctypes.c_ulong(0)
    try:
        api = ctypes.windll.iphlpapi
        api.GetExtendedTcpTable(
            None, ctypes.byref(size), False, AF_INET,
            TCP_TABLE_OWNER_PID_LISTENER, 0,
        )
        table = ctypes.create_string_buffer(size.value)
        if api.GetExtendedTcpTable(
            table, ctypes.byref(size), False, AF_INET,
            TCP_TABLE_OWNER_PID_LISTENER, 0,
        ) != 0:
            return None
        count = struct.unpack_from("I", table, 0)[0]
        row_size = 24
        for index in range(count):
            row = struct.unpack_from("IIIIII", table, 4 + index * row_size)
            state, _, local_port, _, _, pid = row
            if state == MIB_TCP_STATE_LISTEN and socket.ntohs(local_port) == port:
                return pid
    except Exception:  # noqa: BLE001
        return None
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            timeout=2,
            creationflags=CREATE_NO_WINDOW, check=False,
        ).stdout
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                return int(line.split()[-1])
    except Exception:  # noqa: BLE001
        pass
    return None


def _http_probe(url: str, timeout: float = 2.0) -> tuple[bool, int | None]:
    """Return (reachable, http_status) for the frontend TCP listener."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, 200
    except Exception:  # noqa: BLE001 - any failure means "not ready yet"
        return False, None


def _port_free(port: int) -> bool:
    """bind 探测：此刻能否在该端口监听（内核原子裁决，无 TOCTOU 时点快照问题）。

    connect 探测回答「有没有人在听」，这里回答「能不能 bind」——才是选口真正
    要的判定。nginx [::]:8001 事件（2026-10-20）实证了缺口：Windows 默认
    V6ONLY=1 下，外部进程只监听 [::]:P 不妨碍 0.0.0.0:P bind 成功，但浏览器
    解析 localhost 优先走 ::1，会撞上别人的服务——因此 IPv4/IPv6 双栈都必须
    空闲。socket 选项按平台对齐 asyncio/uvicorn 的真实 bind 行为。
    """
    probes: list[tuple[int, str]] = [(socket.AF_INET, "0.0.0.0")]
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM):
            pass
        probes.append((socket.AF_INET6, "::"))
    except OSError:
        pass  # 无 IPv6 栈：IPv4 判定即权威
    for family, host in probes:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        except OSError:
            return False
        finally:
            sock.close()
    return True


# --------------------------------------------------------------------------- #
# 进程归属判定（孤儿清理用）                                                     #
# --------------------------------------------------------------------------- #
def _exe_path_of(pid: int) -> str | None:
    """Return the executable path of a process, or None if unavailable."""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not handle:
            return None
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _parent_pid(pid: int) -> int | None:
    """Return the parent process id via NtQueryInformationProcess, or None."""
    if os.name != "nt":
        return None

    class _PBI(ctypes.Structure):
        _fields_ = [
            ("ExitStatus", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("AffinityMask", ctypes.c_void_p),
            ("BasePriority", ctypes.c_void_p),
            ("UniqueProcessId", ctypes.c_void_p),
            ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ]

    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            pbi = _PBI()
            ret_len = ctypes.c_ulong()
            status = ctypes.windll.ntdll.NtQueryInformationProcess(
                handle, 0, ctypes.byref(pbi),
                ctypes.sizeof(pbi), ctypes.byref(ret_len),
            )
            if status != 0:
                return None
            return int(pbi.InheritedFromUniqueProcessId or 0) or None
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _taskkill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, creationflags=CREATE_NO_WINDOW, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("taskkill failed: %s", exc)


def _listener_pids_posix(port: int) -> list[int]:
    """Find PIDs listening on a TCP port via lsof (macOS/Linux）。

    与 Windows 的 _listener_pid 对等：后端/前端孙进程被 detach 成独立
    进程组，SIGTERM 编排进程够不到它们，只能按端口定位后逐个发信号。
    """
    if os.name == "nt":
        return []
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-s", "tcp:listen"],
            capture_output=True, text=True, timeout=3, check=False,
        ).stdout
        pids: list[int] = []
        for line in out.splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                continue
        return pids
    except Exception as exc:  # noqa: BLE001
        log.warning("lsof port %s failed: %s", port, exc)
        return []


def _kill_port_listeners_posix(port: int, sig: int) -> None:
    for pid in _listener_pids_posix(port):
        try:
            os.kill(pid, sig)
            log.info("posix kill(%s) pid=%s on port %s", sig, pid, port)
        except ProcessLookupError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("posix kill pid=%s failed: %s", pid, exc)


class DeepTutorProcess:
    """Owns the lifetime of the `deeptutor start` child process.

    Frozen, windowed executables cannot reliably read child stdout from a
    pipe. The child therefore writes to a log file and this class tails that
    file with an independently opened handle.
    """

    def __init__(self, deeptutor, home: Path, node_dir: Path | None = None,
                 on_line=None):
        # `deeptutor` is either a path/str (a console entry-point exe) or an
        # argv list (e.g. ["python.exe", "run_deeptutor.py"] for the embedded
        # runtime). Normalize both to a leading argv prefix.
        self.deeptutor_cmd = _as_cmd(deeptutor)
        self.home = Path(home)
        self.node_dir = Path(node_dir) if node_dir else None
        self.on_line = on_line
        self.frontend_port = DEFAULT_FRONTEND_PORT
        self.backend_port = DEFAULT_BACKEND_PORT
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._child_out = None
        self._tail = None
        self.child_log_path: Path | None = None
        self.frontend_ready = threading.Event()
        self.frontend_launching = threading.Event()
        self._stopping = False  # 主动关停标记：监督线程据此放弃自动拉起

    # ------------------------------------------------------------------ #
    @property
    def frontend_url(self) -> str:
        return f"http://127.0.0.1:{self.frontend_port}"

    # ------------------------------------------------------------------ #
    def _kill_stale_listeners(self) -> None:
        """清掉占用本应用端口的「本运行时」孤儿进程（越界进程一律不动）。

        背景：壳被强杀/崩溃时，deeptutor 的后端/前端孙进程会脱离进程树
        存活，占住 8001/3782；下次启动 deeptutor 检测到冲突会弹交互提示
        （stdin 非交互时直接退出）→ 表现为「正在启动本地服务」卡死。
        这里在 spawn 之前清场，只杀属于本应用运行时目录的 python/node。
        """
        if os.name != "nt":
            return
        roots: list[str] = []
        try:
            py = Path(self.deeptutor_cmd[0])
            if py.exists():
                py = py.resolve()
                # .../runtime/python/python.exe → .../runtime；venv/Scripts/x → venv
                roots.append(str(py.parent.parent))
                roots.append(str(py.parent))
        except Exception:  # noqa: BLE001
            pass
        if self.node_dir is not None:
            roots.append(str(self.node_dir))
        roots = [r.lower().rstrip("\\/") for r in roots if r]
        if not roots:
            return

        def owned(exe: str | None) -> bool:
            if not exe:
                return False
            exe_l = exe.lower()
            if exe_l.rsplit("\\", 1)[-1] not in ("python.exe", "node.exe", "deeptutor.exe"):
                return False
            return any(exe_l.startswith(r + "\\") for r in roots)

        for port, name in ((self.backend_port, "backend"), (self.frontend_port, "frontend")):
            pid = _listener_pid(port)
            if not pid:
                continue
            exe = _exe_path_of(pid)
            if not owned(exe):
                log.warning(
                    "port %s held by non-runtime process (pid=%s exe=%s); leaving it alone",
                    port, pid, exe,
                )
                continue
            # 若父进程也是本运行时的进程（残留编排器），连树一起杀
            ppid = _parent_pid(pid)
            if ppid and owned(_exe_path_of(ppid)):
                log.info("killing stale %s orchestrator tree (parent pid=%s)", name, ppid)
                _taskkill_tree(ppid)  # /T 连同端口持有者一起终止
            else:
                log.info(
                    "killing stale %s on :%s (pid=%s exe=%s)", name, port, pid, exe
                )
                _taskkill_tree(pid)

    # ------------------------------------------------------------------ #
    def _select_frontend_port(self) -> int:
        """前端口选口：3782 固定优先，被外部进程占用时向上回退（bind 探测）。

        清场（_kill_stale_listeners）只处理本运行时孤儿，所以走到这里仍被
        占用的端口必然属于外部进程，直接换口而不是失败。后端口不在这里定：
        壳托管模式下 launcher 让内核 bind(:0) 分配，前端代理经 spawn env
        握手跟随后端口，壳只需盯住自己要加载的前端 URL。
        """
        preferred = DEFAULT_FRONTEND_PORT
        for candidate in range(preferred, min(preferred + 200, 65536)):
            if _port_free(candidate):
                if candidate != preferred:
                    log.warning(
                        "frontend port %s held by a foreign process; "
                        "falling back to %s",
                        preferred, candidate,
                    )
                return candidate
        log.warning(
            "no free port at or above %d; keeping %d and letting the child report",
            preferred, preferred,
        )
        return preferred

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Spawn `deeptutor start --home <ws> --no-browser` as a hidden child."""
        self.home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        if self.node_dir is not None:
            env["PATH"] = str(self.node_dir) + os.pathsep + env.get("PATH", "")
        # DeepTutor respects DEEPTUTOR_HOME too; pass --home explicitly.
        env["DEEPTUTOR_HOME"] = str(self.home)
        # 子进程 stdio 强制 UTF-8（2026-10-22）：zh-CN Windows 上 Python 子进程
        # stdout 默认 cp936/GBK，thinkbuddy.log 会落成 GBK 字节——VS Code /
        # Win11 记事本等 UTF-8 工具打开全是乱码（用户机实证：banner 十余次
        # 会话全部 mojibake）。PYTHONIOENCODING 显式指定 stdio，PYTHONUTF8=1
        # 兜底文件系统等其余编码面；_decode_line 本就 UTF-8 优先、GBK 回退，
        # 新旧引擎输出都能正确转发。launcher.py common_env 的同名变量只覆盖
        # deeptutor 再往下拉起的孙进程，这里才是壳直拉子进程的源头。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # 后端就绪超时放宽（2026-10-24）：launcher 默认 60s 是服务端部署的
        # 保守值（#1435 已被撞过），桌面端首启要把整个 site-packages 导入
        # 一遍（更新后首启还要叠加杀软逐文件扫描），用户机 1.6.15 更新事故
        # 实证 60s 撞线 → launcher 退出 → 壳弹「启动失败」横幅，用户手动再
        # 点一次才能进。桌面后端只服务本机前端：等待的代价只是慢，撞线的
        # 代价是错误横幅——取 180s。打包期预编译 .pyc（build_runtime.py
        # precompile_site_packages）才是首启提速的主力，这里是兜底。
        env["DEEPTUTOR_BACKEND_READY_TIMEOUT"] = "180"
        # 注入壳自身版本：后端据此与 thinkbuddy 发布源比对，驱动 Web 侧边栏
        # 左下角「设置」旁的更新提示点（橙点亮/灰）。与「关于」页同源，
        # 优先取正在运行的壳版本而非安装器写入注册表的版本（见 menubar.py）。
        env["DEEPTUTOR_DESKTOP_VERSION"] = __version__

        # 启动前清场：别让上次异常退出留下的孤儿把端口冲突顶到子进程面前。
        self._kill_stale_listeners()
        # 前端口选口（bind 探测，外部占用向上回退）；清场之后执行，刚杀掉的
        # 本应用孤儿端口会回到空闲态、优先复用。
        self.frontend_port = self._select_frontend_port()
        # 壳托管标记：launcher 据此让内核为后端分配 ephemeral 端口（bind :0），
        # 彻底消除后端口冲突；前端经 FRONTEND_PORT 显式注入，壳与子进程天然一致。
        env[SHELL_MANAGED_ENV] = "1"
        env["FRONTEND_PORT"] = str(self.frontend_port)

        # 监督线程用 _stopping 区分「主动关停」与「意外死亡」；每次 start
        # （含监督线程的自动拉起）都回到运行态，并复位就绪事件的陈旧状态。
        self._stopping = False
        self.frontend_ready.clear()
        self.frontend_launching.clear()

        cmd = self.deeptutor_cmd + [
            "start",
            "--home", str(self.home),
            "--no-browser",
        ]
        log.info("spawn: %s", " ".join(cmd))
        # 文件名用品牌名 thinkbuddy.log：本目录（<工作区>/logs）是「帮助 →
        # 打开日志目录」菜单的落点，面向最终用户，不能暴露内部项目标识
        # deeptutor（曾用名 deeptutor-child.log，2026-10-22 品牌化改名）。
        self.child_log_path = self.home / "logs" / "thinkbuddy.log"
        self.child_log_path.parent.mkdir(parents=True, exist_ok=True)
        # 证据保全：追加而非截断（"wb" 曾把上一会话的临终输出连同崩溃取证
        # 一起抹掉）。超过 8MB 轮转成 .old，防止无限膨胀。
        try:
            if (
                self.child_log_path.exists()
                and self.child_log_path.stat().st_size > 8 * 1024 * 1024
            ):
                rotated = self.child_log_path.with_name(
                    self.child_log_path.name + ".old"
                )
                rotated.unlink(missing_ok=True)
                self.child_log_path.replace(rotated)
                log.info("rotated %s -> %s", self.child_log_path.name, rotated.name)
        except OSError:
            log.exception("child log rotation failed; appending anyway")
        self._child_out = self.child_log_path.open("ab")
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.home),
            env=env,
            stdout=self._child_out,
            stderr=subprocess.STDOUT,
            # stdin 用真实管道而非 DEVNULL：Windows 上 NUL 设备的
            # sys.stdin.isatty() 返回 True（FILE_TYPE_CHAR 怪癖），会让
            # deeptutor 的「非 TTY 自动退出」保护失效、走进交互 input()。
            stdin=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        self._reader = threading.Thread(
            target=self._read_output, daemon=True, name="dt-output"
        )
        self._reader.start()
        log.info("deeptutor pid=%s", self.proc.pid)

    def wait_ready(self, timeout: float = 180.0, *,
                   on_progress=None) -> tuple[str, int]:
        """Block until the frontend answers, or raise TimeoutError/RuntimeError.

        就绪判定只依赖前端端口探测（无条件、每轮一次），**不依赖子进程
        stdout 的任何文本**——文本通道（子进程缓冲/尾随线程/语言差异）任何
        一环出问题都不能拖累启动。

        on_progress(elapsed_seconds)：长等待期间的进度回调（每 5s 心跳触发
        一次），供壳把「已等待 Xs」刷进 splash，避免界面静止被当成卡死
        （2026-10-24：更新后首启可达数分钟，生产模式 push_line 是
        debug-only，等待期原本没有任何可见反馈）。回调异常只记日志，
        绝不影响就绪等待本身。
        """
        start = time.monotonic()
        last_beat = start
        while True:
            now = time.monotonic()
            if now - start >= timeout:
                raise TimeoutError(
                    f"frontend {self.frontend_url} did not become ready within "
                    f"{timeout:.0f}s. See the app log for deeptutor output."
                )
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(
                    f"deeptutor exited early (rc={self.proc.returncode}). "
                    "See the app log for deeptutor output."
                )
            if self.frontend_ready.wait(timeout=0.5):
                log.info("frontend ready (child signal) at %s", self.frontend_url)
                return self.frontend_url, 200
            reachable, _status = _http_probe(self.frontend_url, timeout=1.0)
            if reachable:
                log.info("frontend ready (port probe) at %s", self.frontend_url)
                return self.frontend_url, 200
            if now - last_beat >= 5:
                last_beat = now
                elapsed = now - start
                log.info(
                    "wait_ready: %.0fs elapsed; child alive, frontend %s not "
                    "answering yet", elapsed, self.frontend_url,
                )
                if on_progress is not None:
                    try:
                        on_progress(elapsed)
                    except Exception:  # noqa: BLE001
                        log.exception("wait_ready on_progress callback failed")

    def wait_backend_ready(self, timeout: float = 180.0, *,
                           on_progress=None) -> tuple[str, int]:
        """Block until the backend answers HTTP, or raise TimeoutError/RuntimeError.

        前端（Next standalone）不依赖后端即可应答：「前端已就绪」不蕴含
        「后端已就绪」。后端 uvicorn 监听后的首个请求会触发应用栈惰性初始化
        （2026-10-24 用户机实测 ~13s），壳若在前端就绪时立刻切主界面，首屏
        API 全部 500。这里在切换前 HTTP 探活后端：**任何** HTTP 应答（含
        4xx/5xx 路由级错误）都证明应用栈已活、可以进主界面；只有连接拒绝/
        超时才继续等。on_progress 语义与 wait_ready 相同（5s 心跳）。子进程
        早退抛 RuntimeError，交给调用方的重试/横幅链路。
        """
        url = f"http://127.0.0.1:{self.backend_port}/"
        start = time.monotonic()
        last_beat = start
        while True:
            now = time.monotonic()
            if now - start >= timeout:
                raise TimeoutError(
                    f"backend {url} did not become ready within {timeout:.0f}s. "
                    "See the app log for deeptutor output."
                )
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(
                    f"deeptutor exited early (rc={self.proc.returncode}). "
                    "See the app log for deeptutor output."
                )
            try:
                # 无代理 opener（见 _LOOPBACK_OPENER 注释）：探活绝不能走系统代理
                with _LOOPBACK_OPENER.open(url, timeout=1.0):  # noqa: S310 - loopback probe of our own child
                    log.info("backend ready (http 200) at %s", url)
                    return url, 200
            except HTTPError as exc:
                # 应用栈已应答：路由级 404/500 也算就绪，不让业务路由语义拖累启动
                log.info("backend ready (http %s) at %s", exc.code, url)
                return url, exc.code
            except (URLError, TimeoutError, OSError):
                pass
            if now - last_beat >= 5:
                last_beat = now
                elapsed = now - start
                log.info(
                    "wait_backend_ready: %.0fs elapsed; backend %s not answering yet",
                    elapsed, url,
                )
                if on_progress is not None:
                    try:
                        on_progress(elapsed)
                    except Exception:  # noqa: BLE001
                        log.exception("wait_backend_ready on_progress callback failed")
            time.sleep(0.5)

    # ------------------------------------------------------------------ #
    def supervise(
        self,
        *,
        on_event=None,
        max_restarts: int = 3,
        backoff: float = 5.0,
        stability_window: float = 600.0,
    ) -> threading.Thread:
        """稳态监督：子进程意外退出时自动拉起，恢复后通知壳重载页面。

        背景（2026-10-20 用户机实证）：前端 node / 后端 uvicorn 可被外部
        TerminateProcess（安全软件、任务管理器、taskkill）或系统关机事件
        静默杀死；launcher 检测到后停掉整个子树退出，而壳若无所知，
        WebView 停在 ERR_CONNECTION_REFUSED 死页上——用户只能干等或重启
        应用。此线程把「死页挂一小时」变成「自愈几秒」。

        策略：意外退出 → stop() 清树 → start() 重新 spawn → wait_ready；
        最多 max_restarts 次，指数退避 backoff×次数；服务稳定运行超过
        stability_window 秒后重置计数（偶发外杀不会耗尽预算，崩溃循环
        仍有界）。

        on_event(kind, message)：kind ∈ child_exit / restarting /
        restart_failed / recovered / gave_up，供壳做 UI 提示与页面重载。
        """
        def _emit(kind: str, message: str) -> None:
            if on_event is None:
                return
            try:
                on_event(kind, message)
            except Exception:  # noqa: BLE001
                log.exception("supervise on_event callback failed")

        def _loop() -> None:
            restarts = 0
            stable_since: float | None = None
            while not self._stopping:
                proc = self.proc
                if proc is None:
                    return
                rc = proc.poll()
                if rc is None:
                    time.sleep(2.0)
                    continue
                if self._stopping:
                    return
                # 服务已稳定跑过一整个窗口期才重置重试预算：崩溃循环仍有界
                now = time.monotonic()
                if stable_since is not None and now - stable_since >= stability_window:
                    restarts = 0
                log.error(
                    "deeptutor exited unexpectedly (rc=%s)%s",
                    rc,
                    f"; {describe_exit_code(rc)}" if describe_exit_code(rc) else "",
                )
                _emit("child_exit", f"本地服务意外退出（退出码 {rc}）")
                gave_up = False
                while restarts < max_restarts and not self._stopping:
                    restarts += 1
                    _emit(
                        "restarting",
                        f"正在自动恢复本地服务（第 {restarts}/{max_restarts} 次）…",
                    )
                    log.info(
                        "supervise: restarting deeptutor (attempt %d/%d)",
                        restarts, max_restarts,
                    )
                    try:
                        self.stop()
                        self.start()
                        self.wait_ready(timeout=120.0)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("supervise: restart attempt %d failed", restarts)
                        _emit("restart_failed", f"自动恢复失败：{exc}")
                        deadline = time.monotonic() + backoff * restarts
                        while time.monotonic() < deadline and not self._stopping:
                            time.sleep(0.5)
                        continue
                    stable_since = time.monotonic()
                    log.info("supervise: recovered after %d restart(s)", restarts)
                    _emit("recovered", "本地服务已恢复")
                    break
                else:
                    if not self._stopping:
                        gave_up = True
                if gave_up:
                    log.error("supervise: giving up after %d restarts", max_restarts)
                    _emit(
                        "gave_up",
                        "本地服务多次自动恢复失败，请关闭窗口后重新打开 ThinkBuddy",
                    )
                    return

        thread = threading.Thread(target=_loop, daemon=True, name="dt-supervise")
        thread.start()
        return thread

    # ------------------------------------------------------------------ #
    def stop(self, timeout: float = 10.0) -> None:
        """Terminate the whole process tree (backend + next.js server).

        DeepTutor detaches its backend/frontend children into their own process
        groups, so ``taskkill /T`` on the orchestrator PID can occasionally leave
        orphans behind. We therefore also re-scan the known ports and kill any
        listener there.
        """
        # 先立关停标记：监督线程看到后放弃自动拉起，避免关窗途中又 spawn。
        self._stopping = True
        start_pid = None
        if self.proc is not None:
            start_pid = self.proc.pid
            log.info("stopping deeptutor pid=%s", start_pid)
        if os.name == "nt":
            if start_pid:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(start_pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=CREATE_NO_WINDOW,
                        check=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("taskkill failed: %s", exc)
            # port-based sweep (covers detached grandchildren)
            for port, name in ((self.backend_port, "backend"), (self.frontend_port, "frontend")):
                pid = _listener_pid(port)
                if pid:
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True,
                            creationflags=CREATE_NO_WINDOW,
                            check=False,
                        )
                        log.info("killed stale %s on :%s (pid=%s)", name, port, pid)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("port kill failed: %s", exc)
        else:
            # SIGTERM 编排器，让它尽快收尾
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            # 后端/前端孙进程被 detach 成独立进程组，SIGTERM 编排器够不到
            # 它们（Windows 靠 taskkill /T；mac 无对等物）。按端口先礼后兵，
            # 否则要死等编排器优雅收尾，关窗体感拖沓且留孤儿占 8001/3782。
            if self.proc is not None:
                _kill_port_listeners_posix(self.backend_port, signal.SIGTERM)
                _kill_port_listeners_posix(self.frontend_port, signal.SIGTERM)

        deadline = time.monotonic() + timeout
        while self.proc and self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        if os.name != "nt" and self.proc is not None:
            # 兜底：优雅退出超时的孙进程直接 SIGKILL，不留孤儿
            _kill_port_listeners_posix(self.backend_port, signal.SIGKILL)
            _kill_port_listeners_posix(self.frontend_port, signal.SIGKILL)
        self._close_handles()

    # ------------------------------------------------------------------ #
    def _read_output(self) -> None:
        """Tail the child log without touching the frozen parent's PIPE.

        用 ``os.read`` 原始 fd 轮询（此前 ``BufferedReader.read(65536)`` 在
        冻结版里曾静默卡死，就绪文本永远进不了壳），并全程留痕方便排查。
        """
        log.info("tail thread: watching %s", self.child_log_path)
        try:
            fd = os.open(
                str(self.child_log_path),
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except OSError:
            log.exception("tail thread: open failed")
            return
        # 子日志已改为追加模式：跳过历史会话内容，只尾随本次 spawn 之后的新输出。
        os.lseek(fd, 0, os.SEEK_END)
        total = 0
        try:
            pending = bytearray()
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    total += len(chunk)
                    pending.extend(chunk)
                    *lines, pending_bytes = pending.split(b"\n")
                    pending.clear()
                    pending.extend(pending_bytes)
                    for raw in lines:
                        self._handle_line(raw)
                    continue
                if self.proc is not None and self.proc.poll() is not None:
                    if pending:
                        self._handle_line(bytes(pending) + b"\n")
                    break
                # 心跳已删除（2026-10-21）：wait_ready 只依赖端口探测、
                # 不依赖任何文本；10s 一条的心跳曾占 app.log 大头。
                time.sleep(0.2)
        except Exception:  # noqa: BLE001
            log.exception("tail thread: failed to read deeptutor output")
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            log.info("tail thread: exit (%d bytes total)", total)

    def _handle_line(self, raw: bytes) -> None:
        line = _decode_line(raw)
        # app.log 只记壳事件（2026-10-21）：子进程转发行降为 DEBUG。此前无条件
        # INFO 把"读到一行"这个动作标成了级别，导致 app.log 里 ERROR/WARNING
        # 全部伪装成 INFO（级别语义在转发链路上从未被传递）。子进程输出的
        # 完整原样副本仍在 <home>/logs/thinkbuddy.log；结构化事实源是
        # 引擎自己的 <home>/data/user/logs/deeptutor.jsonl（error 遥测走
        # EngineLogTail 增量读它，不依赖本行）。
        log.debug("deeptutor: %s", line)
        lowered = line.lower()
        if "前端 已就绪" in line or "frontend ready" in lowered:
            self.frontend_ready.set()
        if "正在启动前端" in line or "starting frontend" in lowered:
            self.frontend_launching.set()
        # 壳托管模式下后端口由 OS 分配，壳无法预知；从子进程 banner 行
        # （后端/Backend + http://127.0.0.1:PORT）回读实际端口，stop() 的
        # 端口清扫才能兜住 taskkill /T 偶尔漏掉的孙进程。前端 API 行
        # （前端 API / Frontend API）是浏览器侧外部地址，显式排除。
        if "后端" in line or "backend" in lowered:
            if "前端" not in line and "frontend" not in lowered:
                match = BACKEND_URL_PATTERN.search(line)
                if match:
                    port = int(match.group(1))
                    if port != self.backend_port:
                        log.info("learned OS-assigned backend port %s", port)
                        self.backend_port = port
        if self.on_line:
            try:
                self.on_line(line)
            except Exception:  # noqa: BLE001
                pass

    def _close_handles(self) -> None:
        proc = self.proc
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        if self._child_out is not None:
            try:
                self._child_out.close()
            except Exception:  # noqa: BLE001
                pass
            self._child_out = None
        if self._tail is not None:
            try:
                self._tail.close()
            except Exception:  # noqa: BLE001
                pass
            self._tail = None
