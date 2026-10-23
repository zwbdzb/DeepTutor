"""Telemetry event reporter — direct HTTPS report, no local queue.

设计（2026-10-21 定，详见会话规划）：
- 只承载**低频**事件（app_start / login / error / app_exit），直接 POST 到
  服务端 ``POST /v1/events``（批量数组协议，当前单次一条）——将来高频
  turn 事件上本地队列时，服务端协议层零改动。
- fire-and-forget：每事件一个 daemon 线程，**绝不阻塞调用方**；任何失败
  静默丢弃，不在本地计数或补传。
- 三道护栏：
  1. 连续 ``fail_threshold`` 次失败 → 熔断 ``mute_seconds``：断网时每条
     error 陪葬一次 4s 超时不可接受（refused 端口表现为 timeout 而非立即
     失败，2026-10-12 实证）；
  2. ``error`` 前缀事件会话级限速（默认 50 条）：防启动失败循环把遥测
     打成事件风暴；
  3. 退出路径用 :meth:`flush_exit` 同步短超时发送——daemon 线程在进程
     退出时被硬杀，退出原因事件必须同步发。
- 隐私红线：props 白名单由调用方保证，本模块不上报任何用户内容；日志只
  打 DEBUG（上报失败静默）。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
import logging
import platform
import threading
import time
from typing import Any
import urllib.request

log = logging.getLogger("dt.telemetry")

_MESSAGE_MAX_CHARS = 200


def os_description() -> str:
    """系统名称和版本合并上报，不采集主机名或单独的架构字段。"""
    system = platform.system()
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0] or platform.release()}"
    if system == "Windows":
        return f"Windows {platform.release()} ({platform.version()})"
    return f"{system} {platform.release()}".strip() or "unknown"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward OAuth credentials or report contents to a redirect target.
        return None


def truncate_message(value: Any, limit: int = _MESSAGE_MAX_CHARS) -> str:
    """props 里的自由文本统一截断（message/异常类型名等），防巨型 payload。"""
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TelemetryReporter:
    """Direct-report telemetry client with circuit breaker + rate limit."""

    def __init__(
        self,
        endpoint: str,
        base_props: dict[str, Any],
        *,
        access_token: Callable[[], str] | None = None,
        timeout: float = 4.0,
        flush_timeout: float = 1.0,
        error_rate_limit: int = 50,
        fail_threshold: int = 3,
        mute_seconds: float = 600.0,
    ) -> None:
        self._endpoint = (endpoint or "").rstrip("/")
        self._base = dict(base_props)
        self._access_token = access_token or (lambda: "")
        self._timeout = timeout
        self._flush_timeout = flush_timeout
        self._error_rate_limit = error_rate_limit
        self._fail_threshold = fail_threshold
        self._mute_seconds = mute_seconds
        self._lock = threading.Lock()
        self._fails = 0           # 连续失败计数（成功清零）
        self._muted_until = 0.0   # 熔断窗口（time.monotonic 域）
        self._error_count = 0     # error 前缀事件限速计数（会话级）
        self._closed = False

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return bool(self._endpoint)

    # ------------------------------------------------------------------ #
    def report(
        self, name: str, props: dict[str, Any] | None = None, *,
        access_token: str | None = None, occurred_at: str | None = None,
    ) -> None:
        """Fire-and-forget 上报；任何路径都不抛异常、不阻塞调用方。"""
        if not self.enabled:
            return
        try:
            # Capture before scheduling; log events keep their original timestamp.
            ts = int(time.time())
            if occurred_at:
                try:
                    ts = int(datetime.fromisoformat(occurred_at).timestamp())
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
            # Snapshot identity before scheduling: switching accounts must not
            # attribute an older event to the new account.
            token = self._access_token() if access_token is None else access_token
            thread = threading.Thread(
                target=self._post,
                args=(name, dict(props or {}), token, ts),
                daemon=True,
                name="dt-telemetry",
            )
            thread.start()
        except Exception:  # noqa: BLE001
            log.debug("telemetry scheduling failed")

    # ------------------------------------------------------------------ #
    def flush_exit(self, name: str, props: dict[str, Any] | None = None) -> None:
        """退出钩子专用：同步短超时发一条（app_exit 每会话仅一次，值得等）。"""
        if not self.enabled:
            return
        try:
            ts = int(time.time())
            self._post(name, dict(props or {}), self._access_token(), ts, timeout=self._flush_timeout)
        except Exception:  # noqa: BLE001
            log.debug("telemetry exit report failed")

    # ------------------------------------------------------------------ #
    def _post(
        self, name: str, props: dict[str, Any], token: str, ts: int, *, timeout: float | None = None
    ) -> None:
        try:
            self._post_sync(name, props, token, ts, timeout=timeout)
        except Exception:  # noqa: BLE001 — fire-and-forget 铁律：绝不外泄异常
            log.debug("telemetry post raised unexpectedly")

    # ------------------------------------------------------------------ #
    def _post_sync(
        self, name: str, props: dict[str, Any], token: str, ts: int, *, timeout: float | None = None
    ) -> None:
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return
            if now < self._muted_until:
                return
            if name.startswith("error") and self._error_count >= self._error_rate_limit:
                return
            if name.startswith("error"):
                self._error_count += 1
        event: dict[str, Any] = {
            "name": name,
            "ts": ts,
            "props": props,
            **self._base,
        }
        payload = json.dumps({"events": [event]}).encode("utf-8")
        try:
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            request = urllib.request.Request(
                self._endpoint,
                data=payload,
                headers=headers,
                method="POST",
            )
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=self._timeout if timeout is None else timeout) as response:
                response.read(1024)
            with self._lock:
                self._fails = 0
            log.debug("telemetry sent: %s", name)
        except OSError:
            with self._lock:
                self._fails += 1
                if self._fails >= self._fail_threshold:
                    self._muted_until = time.monotonic() + self._mute_seconds
                    log.debug(
                        "telemetry muted for %ss after %d consecutive failures",
                        self._mute_seconds, self._fails,
                    )

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """优雅停机：拒绝新事件（进行中的上报线程自然结束）。"""
        with self._lock:
            self._closed = True
