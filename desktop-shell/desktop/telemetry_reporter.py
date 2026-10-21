"""Telemetry event reporter — direct HTTPS report, no local queue.

设计（2026-10-21 定，详见会话规划）：
- 只承载**低频**事件（当前：error / app_exit；login 预留），直接 POST 到
  服务端 ``POST /v1/events``（批量数组协议，当前单次一条）——将来高频
  turn 事件上本地队列时，服务端协议层零改动。
- fire-and-forget：每事件一个 daemon 线程，**绝不阻塞调用方**；任何失败
  静默丢弃并计入 ``_lost``，下次成功时以 ``lost_before`` 捎带——断网用户
  的 error 内容会丢（幸存者偏差），但"丢了多少"必须对服务端可见。
- 三道护栏：
  1. 连续 ``fail_threshold`` 次失败 → 熔断 ``mute_seconds``：断网时每条
     error 陪葬一次 4s 超时不可接受（refused 端口表现为 timeout 而非立即
     失败，2026-10-12 实证）；
  2. ``error`` 前缀事件会话级限速（默认 50 条）：防启动失败循环把遥测
     打成事件风暴；
  3. 退出路径用 :meth:`flush_exit` 同步短超时发送——daemon 线程在进程
     退出时被硬杀，退出原因事件必须同步发。
- 隐私红线：props 白名单由调用方保证，本模块不上报任何用户内容；日志只
  打 DEBUG（上报失败静默，靠 ``lost_before`` 对账）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("dt.telemetry")

_DEVICE_ID_FILE = "telemetry_device_id"
_MESSAGE_MAX_CHARS = 200


def load_or_create_device_id(home: Path) -> str:
    """安装级匿名 ID：``<home>/telemetry_device_id`` 存随机 UUID。

    与账户完全无关（不含硬件指纹/MAC/用户名），首次生成后永久复用；
    文件损坏/被删则重新生成（只影响事件连续性，无安全影响）。
    编码铁律：显式 ``encoding="utf-8"``（zh-CN Windows 默认 cp936）。
    """
    path = Path(home) / _DEVICE_ID_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except (OSError, ValueError):
        pass
    device_id = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(device_id + "\n", encoding="utf-8")
    except OSError:
        log.debug("device id persist failed; using ephemeral id")
    return device_id


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
        timeout: float = 4.0,
        flush_timeout: float = 1.0,
        error_rate_limit: int = 50,
        fail_threshold: int = 3,
        mute_seconds: float = 600.0,
    ) -> None:
        self._endpoint = (endpoint or "").rstrip("/")
        self._base = dict(base_props)
        self._timeout = timeout
        self._flush_timeout = flush_timeout
        self._error_rate_limit = error_rate_limit
        self._fail_threshold = fail_threshold
        self._mute_seconds = mute_seconds
        self._lock = threading.Lock()
        self._lost = 0            # 本会话累计丢弃（成功时捎带后清零）
        self._fails = 0           # 连续失败计数（成功清零）
        self._muted_until = 0.0   # 熔断窗口（time.monotonic 域）
        self._error_count = 0     # error 前缀事件限速计数（会话级）
        self._closed = False

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return bool(self._endpoint)

    # ------------------------------------------------------------------ #
    def report(self, name: str, props: dict[str, Any] | None = None) -> None:
        """Fire-and-forget 上报；任何路径都不抛异常、不阻塞调用方。"""
        if not self.enabled:
            return
        thread = threading.Thread(
            target=self._post,
            args=(name, dict(props or {})),
            daemon=True,
            name="dt-telemetry",
        )
        thread.start()

    # ------------------------------------------------------------------ #
    def flush_exit(self, name: str, props: dict[str, Any] | None = None) -> None:
        """退出钩子专用：同步短超时发一条（app_exit 每会话仅一次，值得等）。"""
        if not self.enabled:
            return
        self._post_sync(name, dict(props or {}))

    # ------------------------------------------------------------------ #
    def _post(self, name: str, props: dict[str, Any]) -> None:
        try:
            self._post_sync(name, props)
        except Exception:  # noqa: BLE001 — fire-and-forget 铁律：绝不外泄异常
            log.debug("telemetry post raised unexpectedly", exc_info=True)

    # ------------------------------------------------------------------ #
    def _post_sync(self, name: str, props: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return
            if now < self._muted_until:
                self._lost += 1
                return
            if name.startswith("error") and self._error_count >= self._error_rate_limit:
                self._lost += 1
                return
            if name.startswith("error"):
                self._error_count += 1
            lost_before, self._lost = self._lost, 0
        event: dict[str, Any] = {
            "name": name,
            "ts": int(time.time()),
            "props": props,
            "lost_before": lost_before,
            **self._base,
        }
        payload = json.dumps({"events": [event]}).encode("utf-8")
        try:
            request = urllib.request.Request(
                self._endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                response.read(1024)
            with self._lock:
                self._fails = 0
            log.debug("telemetry sent: %s", name)
        except OSError as exc:
            with self._lock:
                self._fails += 1
                self._lost += 1
                if self._fails >= self._fail_threshold:
                    self._muted_until = time.monotonic() + self._mute_seconds
                    log.debug(
                        "telemetry muted for %ss after %d consecutive failures (%s)",
                        self._mute_seconds, self._fails, exc,
                    )

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """优雅停机：拒绝新事件（进行中的上报线程自然结束）。"""
        with self._lock:
            self._closed = True
