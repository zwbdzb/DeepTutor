"""ThinkBuddy Desktop — application entry point.

Brings up a native WebView2 window. While `deeptutor start` boots, a splash
page shows live status; once 127.0.0.1:3782 answers, the window enters a
**login-gated session loop**:

    未登录  → 停留在登录门控页（WorkBuddy 风格：吉祥物 + 黑色登录按钮）
            → 点击按钮用系统浏览器打开 Tokengine 平台注册/登录（PKCE + 回环回调）
            → 登录成功：令牌 + 可用模型（按 model_type 分流）写入 DeepTutor
            → 导航进入应用
    已登录  → 直接进入应用
    应用内退出登录 → 吊销令牌、摘除模型配置 → 导航回登录门控页（功能不可用）
            → 再次登录后重新进入应用

Closing the window hides to the tray (Windows notification area / macOS menu
bar); Quit stops the whole deeptutor process tree.

开发旁路：环境变量 ``DEEPTUTOR_DESKTOP_SKIP_LOGIN=1`` 跳过登录门控（离线开发用）。
"""
from __future__ import annotations

from collections import deque
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import uuid
import webbrowser

from desktop import (
    APP_NAME,
    __version__,
    clipboard,
    dialogs,
    native_menu_backend,
    single_instance,
)
from desktop.auth import AuthManager
from desktop.auth import config as auth_config
from desktop.checkin import CheckinCoordinator, checkin_message
from desktop.default_search import ensure_default_search
from desktop.engine_log_tail import EngineLogTail
from desktop.inject import AccountChipInjector, ToastInjector
from desktop.menubar import (
    _auto_check_updates_on_startup,
    build_native_menu,
    dismiss_ready_update,
    install_ready_update,
)
from desktop.native_menu_backend import (
    configure_account_chip,
    install_windows_shell_menu,
    set_account_chip_hidden,
)
from desktop.process import DEFAULT_FRONTEND_PORT, DeepTutorProcess
import desktop.runtime as rt
from desktop.splash import splash_html
from desktop.telemetry_reporter import (
    TelemetryReporter,
    os_description,
    truncate_message,
)
from desktop.titlebar_account import AccountStatusSync, _fmt_balance
from desktop.tray import SystemTray

log = logging.getLogger("dt.main")

# End users should never see the technical boot panel (ports / paths / logs).
# Set DEEPTUTOR_DESKTOP_DEBUG=1 to render it again for development.
DEBUG = os.environ.get("DEEPTUTOR_DESKTOP_DEBUG", "") == "1"

# 邀请活动页（走系统浏览器打开）；域名走 endpoints 覆盖链
# （环境变量 THINKBUDDY_API_BASE > endpoints.json#api_base > 内置生产默认），
# 解析为空串时回退内置生产默认，避免拼出 "/invite?..." 相对路径。
INVITE_URL = (
    (auth_config.thinkbuddy_api_base(rt.ROOT) or "https://thinkbuddy.hanyoai.com")
    + "/invite?utm_source=agent"
)

# --------------------------------------------------------------------------- #
# logging -------------------------------------------------------------------- #
LOG_DIR = rt.ROOT / "logs"
LOG_FILE = LOG_DIR / "app.log"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    log.info("=== %s v%s starting ===", APP_NAME, __version__)


# --------------------------------------------------------------------------- #
# JS bridge / state ---------------------------------------------------------- #
class Api:
    """Methods callable from the splash/gate page via `pywebview.api.*`."""

    def __init__(
        self,
        frontend_url: str,
        auth: AuthManager,
        debug: bool = False,
        website_url: str | None = None,
    ) -> None:
        self._url = frontend_url
        self._auth = auth
        configured_website_url = (
            website_url
            if website_url is not None
            else auth_config.thinkbuddy_api_base(rt.ROOT)
        )
        self._website_url = configured_website_url.strip().rstrip("/")
        self._checkin = CheckinCoordinator(auth, self._website_url)
        self._debug = debug
        self._lock = threading.Lock()
        self._phase = "boot"
        self._text = "正在初始化…"
        self._detail = ""
        self._loglines: deque[str] = deque(maxlen=5)

    def set_status(self, phase: str, text: str, detail: str = "") -> None:
        with self._lock:
            self._phase = phase
            self._text = text
            # technical detail is debug-only; end users just see the status line
            if detail and self._debug:
                self._detail = detail

    def push_line(self, line: str) -> None:
        """Feed a live boot log line. Ignored in production (debug-only)."""
        if not self._debug:
            return
        with self._lock:
            self._loglines.append(line)
            self._detail = "\n".join(self._loglines)

    # -- bridge methods called from the splash page -------------------- #
    def status(self) -> dict:
        with self._lock:
            return {
                "phase": self._phase,
                "text": self._text,
                "detail": self._detail,
            }

    def open_browser(self) -> None:
        webbrowser.open(self._url)

    # -- Tokengine 登录桥（登录门控页 / 应用内按钮共用）------------------ #
    def auth_status(self) -> dict:
        return self._auth.status()

    def login(self) -> dict:
        """发起 OAuth 登录；成功后令牌与模型自动写入 DeepTutor 并进入应用。"""
        result = self._auth.start_login()
        if result.get("ok"):
            webbrowser.open(result["url"])
            self.set_status("login", "已在浏览器打开 Tokengine 登录页…",
                            "请完成登录并在授权页点击「确认授权」")
        else:
            self.set_status("login", "无法发起登录",
                            result.get("detail") or result.get("error") or "")
        return result

    def logout(self) -> dict:
        return self._auth.logout()

    # -- 右键菜单动作（登录后）------------------------------------------ #
    def open_platform(self) -> dict:
        """系统浏览器打开 Tokengine 平台首页（注册/登录入口）。"""
        return self._auth.open_platform()

    def refresh_models(self) -> dict:
        """重拉中继 /v1/models → 重写 model_catalog → 回写账号信息。"""
        return self._auth.refresh_models()

    def refresh_user_info(self) -> dict:
        """重拉平台 /v1/userinfo → 只更新用户名/余额（不动模型、不刷页面）。"""
        return self._auth.refresh_user_info()

    def checkin_points(self) -> dict:
        """Use the OAuth subject to check in directly with thinkbuddy-website."""
        return self._checkin.checkin()

    def copy_relay(self) -> dict:
        """复制 API 中继地址到剪贴板（刻意不复制业务 token，避免泄露）。"""
        st = self._auth.status()
        url = str(st.get("relay_base") or "").strip()
        if not url:
            return {"ok": False, "message": "暂无 API 地址可复制"}
        clipboard.set_text(url)
        return {"ok": True, "url": url}

    def about(self) -> dict:
        """『关于 ThinkBuddy』信息：壳版本 + deeptutor 版本 + 中继域名。"""
        return {
            "app": f"{APP_NAME} 桌面端",
            "app_version": __version__,
            "deeptutor_version": str(_shared.get("deeptutor_version") or "未知"),
            "relay": str(self._auth.status().get("relay_base") or ""),
        }

    def toast(self, msg: str, kind: str = "info",
              duration_ms: int = 2000) -> None:
        """在页面底部弹一条轻提示（kind: 'info' | 'success' | 'error'）。"""
        js = "window.__thinkbuddyToast(%s, %d, %s)" % (
            json.dumps(str(msg), ensure_ascii=False),
            duration_ms,
            json.dumps(kind, ensure_ascii=False),
        )
        for w in webview_windows:
            try:
                w.evaluate_js(js)
                return
            except Exception:  # noqa: BLE001  窗口还没就绪 / 已关闭
                continue

    @staticmethod
    def quit() -> None:
        tray = _shared.get("tray")
        if tray is not None:
            tray.allow_exit()
        for w in webview_windows:
            try:
                w.destroy()
            except Exception:  # noqa: BLE001  (window already closed)
                pass


# shared handles ----------------------------------------------------
webview_windows: list = []
_shared: dict = {"proc": None}  # the live DeepTutorProcess


def _reload_page() -> None:
    """登录成功后刷新应用页面，让 DeepTutor 重新读取刚写入的模型目录。"""
    for w in webview_windows:
        try:
            w.evaluate_js("location.reload()")
            log.info("已刷新应用页面以载入新写入的模型")
            return
        except Exception:  # noqa: BLE001
            continue


def _on_app_page(expect_url: str | None) -> bool:
    """当前是否停在应用页面（而非启动页/登录门控页）。"""
    if not expect_url:
        return True
    for w in webview_windows:
        try:
            cur = w.get_current_url() or ""
            return cur.startswith(expect_url)
        except Exception:  # noqa: BLE001
            continue
    return False


def _chip_actions(api: Api) -> dict:
    """标题栏账号区下拉菜单的动作分发表（动作名见 titlebar_account.MENU_ACTIONS）。

    与旧页面菜单（ADR-002）行为对齐，两处差异：
      * 退出登录的「再次点击确认」是自绘 HTML 才有的交互，原生菜单改用
        系统 MessageBox（确定/取消）二次确认；
      * 「刷新可用模型」成功后照旧刷新页面，让应用重新读取模型目录。
    """
    def invite() -> None:
        webbrowser.open(INVITE_URL)
        api.toast("已在浏览器打开邀请页")

    def refresh() -> None:
        res = api.refresh_models()
        if res.get("ok"):
            api.toast("模型已刷新", kind="success")
            _reload_page()
        else:
            api.toast(res.get("message") or res.get("error") or "刷新失败",
                      kind="error", duration_ms=3500)

    def refresh_user() -> None:
        """菜单头 ⟳ 图标：轻量刷新用户名/余额（/v1/userinfo）。

        不动模型目录、不刷新页面；store 更新后 AccountStatusSync（0.4s
        轮询）会自动把新余额推到菜单头行。toast 带上新余额给即时反馈。
        """
        res = api.refresh_user_info()
        if res.get("ok"):
            amount = _fmt_balance(res.get("balance"))
            api.toast("用户信息已刷新" + (f"，余额 ¥{amount}" if amount else ""),
                      kind="success")
        else:
            api.toast(res.get("message") or res.get("error") or "刷新失败",
                      kind="error", duration_ms=3500)

    def checkin() -> None:
        api.toast("正在签到…")
        result = api.checkin_points()
        # 结果 toast 按成功/失败区分语义圆点与停留时长（成功 2s / 失败 3.5s）。
        ok = bool(result.get("ok"))
        api.toast(
            checkin_message(result),
            kind="success" if ok else "error",
            duration_ms=2000 if ok else 3500,
        )

    def logout() -> None:
        # MB_OKCANCEL | MB_ICONWARNING；IDOK == 1
        if dialogs.message_box("退出登录", "确定要退出当前账号吗？", 0x31) != 1:
            return
        res = api.logout()
        if res.get("ok"):
            api.toast("已退出登录", kind="success")
            evt = _shared.get("logout_requested")
            if isinstance(evt, threading.Event):
                evt.set()  # 会话主循环将导航回登录门控页
        else:
            api.toast("退出登录失败，请查看日志", kind="error", duration_ms=3500)

    def about() -> None:
        info = api.about()
        lines = [
            f"{APP_NAME} 桌面端：v{info['app_version']}",
            f"ThinkBuddy 引擎：v{info['deeptutor_version']}",
        ]
        if info.get("relay"):
            lines.append(f"中继：{info['relay']}")
        dialogs.message_box(f"关于 {APP_NAME}", "\n".join(lines))

    return {
        "login": api.login,
        "switch": api.login,
        "platform": api.open_platform,
        "invite": invite,
        "checkin": checkin,
        "refresh": refresh,
        "refresh_user": refresh_user,
        "copy": api.copy_relay,
        "logout": logout,
        "about": about,
        # 更新角标（ADR-007）：角标主体 = 安装并重启；✕ = 收起继续用旧版
        "update_restart": lambda: install_ready_update(api),
        "update_dismiss": dismiss_ready_update,
    }


# --------------------------------------------------------------------------- #
# 登录门控会话循环 ------------------------------------------------------------- #
def _gate_needed(auth: AuthManager) -> bool:
    """是否需要登录门控：显式旁路 > 本地可用令牌（登录写入的或手配的）。"""
    if os.environ.get("DEEPTUTOR_DESKTOP_SKIP_LOGIN", "") == "1":
        return False
    return not auth.has_usable_token()


def _wait_gate_login(api: Api, auth: AuthManager,
                     stop: threading.Event | None = None) -> bool:
    """停在登录门控页，阻塞直到一次登录尝试成功。

    页面文本经 ``api.set_status`` 推送（门控页每 300ms 轮询）：
    默认「登录后开始使用」；浏览器已打开显示等待文案；失败显示
    「登录未完成：<原因>」（门控页按前缀渲染成红字）。

    ``stop`` 置位时立即返回 False（窗口已关闭，调用方应退出会话循环），
    避免门控等待线程在窗口销毁后仍空转。
    """
    api.set_status("login", "登录后开始使用", "")
    while True:
        if stop is not None and stop.is_set():
            return False
        result = auth.wait_done(timeout=2.0)
        if result.get("ok"):
            return True
        err = result.get("error")
        if err == "timeout":
            continue          # 尚无尝试结束，继续等（按钮态由页面自轮询）
        if err == "skipped":
            # skip 通道已从 UI 移除；万一触发（旧页面残留）视为未登录继续等
            api.set_status("login", "登录后开始使用", "")
            continue
        msg = result.get("message") or "登录未完成"
        api.set_status("login", f"登录未完成：{msg}",
                       result.get("detail") or "")


def run_session(window, api: Api, url: str, auth: AuthManager,
                stop: threading.Event) -> None:
    """门控 ↔ 应用 的会话主循环（后台线程；窗口关闭时随进程退出）。

    每一轮：需要登录则停在门控页等成功 → 进入应用 → 等待退出登录信号
    → 导航回门控页（load_html 重载登录页，应用界面随之不可用）。

    退出登录后 ``force_gate`` 置位：下一轮**无条件**停在门控页等一次全新
    登录，不再复评 ``has_usable_token()``——注销后回门控是确定性动作，
    不能被 catalog 里残留的手配 api_key 等状态跳过（否则刚 load_html 的
    登录页会在几毫秒后被 load_url 覆盖，应用带着已吊销令牌闪回）。
    ``DEEPTUTOR_DESKTOP_SKIP_LOGIN=1`` 仅旁路启动门控，注销后仍回门控页。
    """
    logout_evt = threading.Event()
    _shared["logout_requested"] = logout_evt
    gate_page = splash_html(debug=DEBUG, version=__version__, gate=True)
    force_gate = _gate_needed(auth)

    while not stop.is_set():
        try:
            # ---- 登录门控：未登录则停留在此，直到登录成功 ---- #
            if force_gate or _gate_needed(auth):
                force_gate = False
                if not _wait_gate_login(api, auth, stop):
                    return      # 窗口已关闭，会话循环随之结束

            # ---- 进入应用 ---- #
            # 丢弃门控等待期间残留的退出信号。注销回门控后，账号菜单的
            # 「退出登录」仍可能被再次触发（菜单 + 确认框），把 evt 置位；
            # 此刻会话循环阻塞在 _wait_gate_login，无人消费——若不清理，
            # 下一次登录成功进入应用时会立即误判为退出，闪回登录页
            # （2026-09-28 事故：登录完成 0.5s 后 logout detected）。
            # 与 AuthManager.logout() 清理陈旧 _done/_result（09-18 事故）
            # 互为镜像：陈旧成功信号导致「退出后闪回应用」，陈旧退出信号
            # 导致「登录后闪回登录页」。门控页本就处于未登录态，丢弃安全。
            logout_evt.clear()
            api.set_status("ready", "服务已就绪 ✓", f"正在载入本地应用 {url}")
            log.info("navigating to %s", url)
            time.sleep(0.5)  # let the splash repaint the "ready" state
            # 启动/门控阶段账号区一直隐藏；进应用页才亮出（显示已登录用户名）。
            # 登录入口归门控页页面按钮，标题栏只做「身份指示 + 账号操作」。
            set_account_chip_hidden(False)
            window.load_url(url)

            # endpoints.json 显式改动后的重绑定提示（改写动作在 main() 早期已完成）
            sync = getattr(api, "endpoint_sync", None) or {}
            if sync.get("changed"):
                api.toast(f"API 地址已按 endpoints.json 对齐：{sync.get('relay_base', '')}")

            # ---- 会话期：等待退出登录 ---- #
            while not logout_evt.wait(timeout=2.0):
                if stop.is_set():
                    return
            logout_evt.clear()

            # ---- 回到登录门控页：应用界面随页面卸载而不可用 ---- #
            log.info("logout detected; returning to login gate")
            api.set_status("login", "登录后开始使用", "")
            set_account_chip_hidden(True)   # 门控页隐藏账号区（登录入口在页面中间）
            window.load_html(gate_page)
            force_gate = True  # 注销后必须重新登录才能再进应用
        except Exception:  # noqa: BLE001
            if stop.is_set():
                return          # 窗口已关闭，静默退出
            log.exception("session loop iteration failed")
            api.set_status("error", "会话异常，请关闭窗口后重新打开。",
                           "若反复失败，请把日志文件发给技术支持：\n" + str(LOG_FILE))
            return


# --------------------------------------------------------------------------- #
# bootstrap ------------------------------------------------------------------ #
def bootstrap(window, api: Api, auth: AuthManager) -> None:
    """runtime -> deeptutor start -> health check -> 登录门控会话循环。"""
    proc: DeepTutorProcess | None = None
    try:
        # 1. runtime (venv/portable-node/deeptutor; dev = system PATH)
        api.set_status("boot", "正在准备运行环境…", "检查 Python / Node / ThinkBuddy")
        deeptutor, node_dir = rt.ensure_runtime(on_line=api.push_line)
        if deeptutor is None:
            raise RuntimeError(
                "未找到 ThinkBuddy / Node.js 运行时。\n"
                "请先安装：pip install -U deeptutor 和 Node.js 20+\n"
                "（打包版安装器内置运行时，无需手动处理）"
            )
        if node_dir is None:
            raise RuntimeError(
                "未找到 Node.js（ThinkBuddy 需要 Node 20+ 才能启动前端）。请安装 Node.js。"
            )

        # 2. workspace
        home = rt.default_workspace()
        home.mkdir(parents=True, exist_ok=True)
        try:
            ensure_default_search(home)
        except (OSError, ValueError, TypeError, AttributeError):
            log.exception("默认搜索配置初始化失败，保留现有设置")
        api.set_status("boot", "正在启动 ThinkBuddy 本地服务…", f"工作区：{home}")

        # 3.5 直报不走本地队列。错误数据来自引擎 JSONL 增量 tail。
        #     （不依赖子进程，置于 spawn 重试循环之外，只建一次。）
        reporter = TelemetryReporter(
            auth_config.telemetry_url(rt.ROOT),
            {
                "machine_id": auth.machine_id(),
                "session_id": str(uuid.uuid4()),
                "app_ver": __version__,
                "os": os_description(),
            },
            access_token=auth.telemetry_access_token,
        )
        _shared["reporter"] = reporter
        _shared["boot_t0"] = time.monotonic()

        def _on_engine_error(entry: dict) -> None:
            reporter.report(
                "error_backend",
                {
                    "logger": truncate_message(entry.get("logger", ""), 120),
                    "message": truncate_message(entry.get("message", "")),
                    # exception 全文含本机路径（用户名属 PII 边缘），只报
                    # 首行异常类型做聚合指纹。
                    "error_kind": truncate_message(
                        str(entry.get("exception", "")).splitlines()[-1] if entry.get("exception") else "",
                        120,
                    ),
                },
                occurred_at=entry.get("timestamp"),
            )

        tail = EngineLogTail(
            home / "data" / "user" / "logs" / "deeptutor.jsonl",
            on_error=_on_engine_error,
        )
        _shared["engine_log_tail"] = tail
        tail.start()

        # 3+4. spawn + 就绪等待，带一次有界自动重试（2026-10-24）。
        # 旧路径首启失败直接弹「启动失败」横幅：supervise 在 wait_ready 成功
        # 之后才启动，管不到首启；而冷机/杀软扫描/更新后首启确实可能超时，
        # 用户机实证「再点一次就能进」——把这个自愈变成自动行为（最多 1 次）。
        proc = None
        url = ""
        last_exc: Exception | None = None
        for attempt in (1, 2):
            if attempt == 2:
                api.set_status(
                    "boot",
                    "首次启动较慢，正在自动重试…",
                    "已重置本地服务再试一次，无需手动操作",
                )
                log.warning(
                    "bootstrap: first spawn/wait attempt failed (%s); retrying once",
                    last_exc,
                )
                time.sleep(2.0)
            proc = DeepTutorProcess(deeptutor, home, node_dir, on_line=api.push_line)
            _shared["proc"] = proc
            try:
                proc.start()
                api.set_status(
                    "boot",
                    "正在启动本地服务…",
                    f"{proc.frontend_url}（后端 :{proc.backend_port} + 前端 :{proc.frontend_port}）",
                )
                log.info(
                    "waiting for frontend readiness (attempt %d, timeout=%ss)...",
                    attempt, 150,
                )
                url, _status = proc.wait_ready(timeout=150)
                # 前端就绪 ≠ 后端就绪：Next standalone 不依赖后端即可应答，
                # 而后端监听后的首个请求会触发应用栈惰性初始化（实测 ~13s），
                # 直接切主界面会让首屏 API 全 500。这里 HTTP 探活后端，任何
                # 应答即过；失败走与上面相同的 spawn 重试链路。
                proc.wait_backend_ready(timeout=180)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.exception("bootstrap: spawn/wait attempt %d failed", attempt)
                try:
                    proc.stop()
                except Exception:  # noqa: BLE001
                    log.exception(
                        "bootstrap: proc.stop cleanup failed after attempt %d", attempt
                    )
                _shared["proc"] = None
                proc = None
        if proc is None:
            raise RuntimeError(f"本地服务未能启动：{last_exc}") from last_exc
        _shared["deeptutor_version"] = rt.resolve_deeptutor_version() or "未知"

        # 4.5 Sync Tokengine user info / balance / models in the background so
        # an unreachable platform can never block the login gate.
        def _boot_refresh() -> None:
            try:
                if auth.refresh_models().get("ok"):
                    log.info("Tokengine account/models refreshed on boot")
                else:
                    log.warning("Tokengine refresh skipped or failed")
            except Exception:
                log.exception("Tokengine boot refresh failed")
            finally:
                # One event per launch; wait for the existing background token
                # refresh so saved sessions can be identified after a restart.
                reporter.report("app_start")

        threading.Thread(target=_boot_refresh, daemon=True, name="dt-boot-refresh").start()

        # 5. 账号区 + toast 注入。
        #    Windows：账号入口走原生标题栏 AccountChip（configure_account_chip）。
        #    macOS：用壳原生 titlebarAccessoryViewController 在红绿灯左边
        #    挂 NSView + NSTextField（见 AccountChipInjector），位置与
        #    Windows 账号区几乎一致（标题栏右侧）。pywebview 的 Cocoa 后端
        #    已 import AppKit（打包必然含 pyobjc），无需额外 hiddenimports。
        #    run() 通过 AppHelper.callAfter 把 UI 创建派发给主线程，当前线
        #    程等 5s 超时，不会卡住 bootstrap。
        _account_chip_injector = None
        if sys.platform == "darwin":
            _account_chip_injector = AccountChipInjector(
                window=window,
                on_login=api.login,
                menu_actions=_chip_actions(api),
            )
            _shared["account_chip_injector"] = _account_chip_injector
            _account_chip_injector.run()  # 主线程内初始化（callAfter 派发）
            # 应用菜单（屏幕顶部第一项）中文化：等 pywebview 建好菜单后派发
            from desktop import menubar as _menubar

            threading.Thread(
                target=_menubar._localize_app_menu_later,
                daemon=True, name="dt-appmenu-localize",
            ).start()

        sync = AccountStatusSync(
            chip_fn=lambda: native_menu_backend.account_chip() or _account_chip_injector,
            status_of=api.auth_status,
            on_authenticated=_reload_page,
            on_app_page=lambda: _on_app_page(url),
        )
        _shared["account_sync"] = sync
        threading.Thread(target=sync.run, daemon=True, name="dt-account-sync").start()

        toaster = ToastInjector(window, expect_url=url)
        _shared["toast_injector"] = toaster
        threading.Thread(target=toaster.run, daemon=True, name="dt-toast").start()

        # 5.5 稳态监督：子进程（后端/前端）被外杀或崩溃时自动拉起，恢复后
        #     重载页面——否则 WebView 会停在 ERR_CONNECTION_REFUSED 死页上
        #     直到用户手动重启（2026-10-20 用户机 app.log 实证：前端 14:28
        #     被外部 TerminateProcess 杀死，死页挂了近 1 小时无人知晓）。
        def _on_service_event(kind: str, message: str) -> None:
            log.info("service event: %s — %s", kind, message)
            if kind == "recovered":
                try:
                    window.load_url(url)
                except Exception:  # noqa: BLE001
                    log.exception("reload after service recovery failed")
            api.set_status("error" if kind == "gave_up" else "boot", message, "")

        proc.supervise(on_event=_on_service_event)

        # 6. 登录门控会话主循环（门控 ↔ 应用，退出登录即回门控页）
        stop_evt = threading.Event()
        _shared["session_stop"] = stop_evt
        run_session(window, api, url, auth, stop_evt)
    except Exception as exc:  # noqa: BLE001
        log.exception("bootstrap failed")
        if proc:
            proc.stop()
            _shared["proc"] = None
        # keep it friendly for end users; full traceback lives in the log file
        if DEBUG:
            api.set_status("error", f"启动失败：{exc}", "")
        else:
            api.set_status(
                "error",
                "启动失败，请关闭窗口后重新打开。",
                "若反复失败，请把日志文件发给技术支持：\n" + str(LOG_FILE),
            )


# --------------------------------------------------------------------------- #
# entry ---------------------------------------------------------------------- #
def main() -> int:
    _setup_logging()

    outcome = single_instance.acquire_or_activate(rt.ROOT / "app.lock", APP_NAME)
    if outcome == single_instance.ACTIVATED:
        # 已有实例：已把它的窗口带到前台，本进程静默退出（点快捷方式的预期行为）
        log.info("another instance is running; its window was brought to front")
        return 0
    if outcome != single_instance.PRIMARY:
        log.warning("another instance is already running; window not activatable")
        # 用跨平台 dialogs.message_box（mac 走 tkinter/osascript）。
        # 之前直接调 ctypes.windll.user32.MessageBoxW，在 mac 上抛
        # AttributeError 被吞掉后静默 return 1 —— 表现就是“图标闪一下没窗口”。
        dialogs.message_box(
            APP_NAME,
            "ThinkBuddy 已在运行，请直接切换到已打开的窗口。",
            0x40,  # MB_ICONINFORMATION
        )
        return 1

    try:
        import webview  # lazy so a missing dep still yields a readable error
        install_windows_shell_menu()
    except Exception as exc:  # noqa: BLE001
        log.error("pywebview missing: %s", exc)
        return 2

    # Keep downloads in the authenticated WebView and use its native save dialog.
    webview.settings["ALLOW_DOWNLOADS"] = True
    # macOS：不追加 pywebview 默认的英文 Edit / View 菜单，顶部菜单栏只保留
    # build_native_menu 的 文件 / 编辑 / 视图 / 帮助（避免中英文菜单重复）。
    webview.settings["SHOW_DEFAULT_MENUS"] = False

    frontend_url = f"http://127.0.0.1:{DEFAULT_FRONTEND_PORT}"
    def _on_login(access_token: str) -> None:
        reporter = _shared.get("reporter")
        if reporter is not None:
            reporter.report("login", access_token=access_token)

    auth = AuthManager(on_login=_on_login)
    api = Api(
        frontend_url,
        auth,
        debug=DEBUG,
        website_url=auth_config.thinkbuddy_api_base(rt.ROOT),
    )

    # 端点对齐要赶在 DeepTutor 后端起来之前做：catalog 改写完成后，
    # 后端首次读取拿到的就是 endpoints.json 指向的地址。
    try:
        api.endpoint_sync = auth.apply_endpoint_overrides()
    except Exception:  # noqa: BLE001  对齐失败不拦启动，日志里可查
        log.exception("端点对齐失败（忽略，继续启动）")
        api.endpoint_sync = {}

    # 标题栏账号区回调登记（须在窗口创建前；见 native_menu_backend.AccountChip）
    configure_account_chip(on_login=api.login, actions=_chip_actions(api))

    window = webview.create_window(
        "",
        html=splash_html(debug=DEBUG, version=__version__),
        width=1280,
        height=860,
        min_size=(450, 450),
        js_api=api,
        background_color="#ffffff",
        text_select=True,
        menu=build_native_menu(api),
    )
    webview_windows.append(window)

    tray = SystemTray(window, api.quit)
    _shared["tray"] = tray
    window.events.closing += tray.on_closing
    window.events.closed += _on_closed
    # 标题栏图标与菜单位置由 native_menu_backend 的自定义标题栏方案在窗口
    # 创建期统一处理（ShowIcon=False + WM_NCCALCSIZE 抹掉标题栏，菜单条
    # 顶到第一排）。不要再在 shown/loaded 事件里做 ctypes 调用——冻结版中
    # 曾引发 Python 工作线程集体冻结（2026-09-23），那套代码已删除。

    try:
        # webview.start(func) 的 func 在独立线程跑，与原生窗口创建并发；
        # bootstrap 内部有健康检查兜底，托盘平台钩子则等 events.shown。
        def _on_window_ready() -> None:
            threading.Thread(
                target=bootstrap, args=(window, api, auth), daemon=True
            ).start()
            # 启动后延迟静默检查更新：有新版本才弹询问框，无更新不打扰。
            threading.Thread(
                target=_auto_check_updates_on_startup,
                args=(api,),
                daemon=True,
                name="dt-auto-update",
            ).start()
            # 平台钩子（mac delegate / 关机放行）须等 pywebview 建完窗口再装。
            tray.on_window_ready()

        tray.start()
        webview.start(_on_window_ready, debug=False)
    except Exception as exc:  # noqa: BLE001
        # mac 打包 console=False，任何窗口创建/事件循环阶段的崩溃都会被
        # 吞掉 → 表现就是“图标闪一下就没”。加兜底：写死日志 + 弹可见错误框，
        # 不再静默退出。
        log.exception("窗口启动失败（webview.start 抛异常）：%s", exc)
        dialogs.message_box(
            APP_NAME,
            "ThinkBuddy 窗口启动失败。\n"
            f"错误：{exc}\n\n"
            "若反复失败，请把日志文件发给技术支持：\n" + str(LOG_FILE),
            0x10,  # MB_ICONERROR
        )
        return 3
    finally:
        _shutdown()
    return 0


def _on_closed() -> None:
    log.info("window closed; stopping deeptutor")
    tray = _shared.pop("tray", None)
    if tray is not None:
        tray.stop()
    stop_evt = _shared.get("session_stop")
    if isinstance(stop_evt, threading.Event):
        stop_evt.set()
    for key in ("account_sync", "toast_injector"):
        worker = _shared.get(key)
        if worker is not None:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001
                pass
            _shared[key] = None
    proc = _shared["proc"]
    if proc:
        try:
            proc.stop()
        except Exception:  # noqa: BLE001
            pass
        _shared["proc"] = None
    tail = _shared.get("engine_log_tail")
    if tail is not None:
        try:
            tail.stop()
        except Exception:  # noqa: BLE001
            pass
        _shared["engine_log_tail"] = None
    reporter = _shared.get("reporter")
    if reporter is not None:
        try:
            # app_exit 走同步短超时（护栏3）：daemon 线程在进程退出时被硬杀，
            # 退出原因事件必须同步发。clean=True 仅覆盖用户主动关窗路径；
            # 壳自身崩溃时无人发送（已知接受的极限场景）。
            duration = time.monotonic() - _shared.get("boot_t0", time.monotonic())
            reporter.flush_exit(
                "app_exit",
                {
                    "reason": "window_closed",
                    "clean": True,
                    "session_duration_s": round(duration, 1),
                },
            )
        except Exception:  # noqa: BLE001
            pass
        reporter.close()
        _shared["reporter"] = None


def _shutdown() -> None:
    _on_closed()
    single_instance.release(rt.ROOT / "app.lock")
    log.info("exited cleanly")


if __name__ == "__main__":
    raise SystemExit(main())
