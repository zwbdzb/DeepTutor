"""Incremental tail of the engine's structured JSONL log.

为什么存在：引擎（DeepTutor）自身的日志落盘是**正确的**——
``configure_logging`` 用 ``RotatingFileHandler`` + ``JsonlFormatter`` 写
``<home>/data/user/logs/deeptutor.jsonl``，每行一个 JSON 对象，``level``
是结构化字段、traceback 在 ``exception`` 字段里天然单行。壳的 app.log 转发
副本反而把级别混淆成了统一 INFO（``process.py._handle_line`` 曾无条件
``log.info``）。因此 error 遥测的数据源**不是**壳日志，而是这个 JSONL。

"增量"语义：
- 记录上次读到的字节偏移 ``_offset``，每轮只读 ``(offset, size]`` 新增段，
  每条日志恰好处理一次；
- 启动时 ``_offset`` 定位到**当前文件末尾**——只关心本会话期间的新错误，
  不翻历史旧账（否则壳每次重启都会把历史 ERROR 重报一遍）。

三个必处理的边界：
1. **轮转**：``RotatingFileHandler`` 写满会把当前文件 rename 成 ``.1`` 并
   另开新文件——表现为 ``size < offset``，此时 offset 重置 0 从头读新文件；
2. **半行**：读的时刻引擎可能正写到一半（末尾无 ``\\n``）——残段留在
   缓冲区，下轮拼上后半截再解析；
3. **Windows 句柄**：引擎轮转时要 rename 正在被写的文件，Windows 上
   任何持有读句柄的外部进程都会让 rename 失败（``PermissionError``），
   引擎那条日志就丢了。所以本模块**每轮 poll 都重新 open/close**，
   不长持句柄，把冲突窗口压到毫秒级。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("dt.telemetry")

DEFAULT_POLL_INTERVAL = 5.0
_ERROR_LEVELS = frozenset({"ERROR", "CRITICAL"})
# JSONL 轮转后的滚动文件名（RotatingFileHandler 默认后缀），轮转探测时用。
_ROLL_SUFFIXES = (".1", ".2", ".3")


class EngineLogTail:
    """Watch a JSONL log incrementally; invoke ``on_error`` per ERROR entry."""

    def __init__(
        self,
        path: Path,
        on_error: Callable[[dict], None],
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._path = Path(path)
        self._on_error = on_error
        self._poll_interval = poll_interval
        self._offset = 0
        self._file_id: tuple[int, int] | None = None  # (st_dev, st_ino) 轮转检测
        self._pending = b""      # 半行残段（末尾无 \n 的不完整行）
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._started = False

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._offset = self._initial_offset()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="dt-jsonl-tail"
        )
        self._thread.start()
        log.info("engine log tail: watching %s (from offset %d)", self._path, self._offset)

    # ------------------------------------------------------------------ #
    def stop(self, timeout: float = 2.0) -> None:
        self._stop_evt.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # ------------------------------------------------------------------ #
    def _initial_offset(self) -> int:
        """启动即定位到文件末尾：不追历史（防壳重启后重报旧 ERROR）。"""
        try:
            stat = self._path.stat()
            self._file_id = (stat.st_dev, stat.st_ino)
            return stat.st_size
        except OSError:
            self._file_id = None
            return 0

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 — tail 绝不能因单轮异常而死
                log.debug("engine log tail poll failed", exc_info=True)
            self._stop_evt.wait(self._poll_interval)

    # ------------------------------------------------------------------ #
    def _poll_once(self) -> None:
        try:
            stat = self._path.stat()
            size = stat.st_size
            file_id = (stat.st_dev, stat.st_ino)
        except OSError:
            # 文件尚未出现（引擎可能关闭了 file_output）：原地等待即可。
            self._offset = 0
            self._pending = b""
            self._file_id = None
            return

        # 轮转检测：RotatingFileHandler rename 旧文件后另开新文件，新文件
        # inode 必然不同。用文件身份（而非 size 比较）判定——轮转后新文件
        # 可能已在两次 poll 之间写入内容使 size > 旧 offset，此时按 size
        # 判定会漏判并从旧 offset 读到错位数据（单测 rotation 场景实证）。
        if file_id != self._file_id:
            log.info("engine log rotated (file identity changed); restarting from 0")
            self._file_id = file_id
            self._offset = 0
            self._pending = b""
        elif size < self._offset:
            # 防御兜底：同 inode 被截断（罕见），从头读。
            log.info("engine log truncated (size %d < offset %d); restarting from 0", size, self._offset)
            self._offset = 0
            self._pending = b""
        if size == self._offset:
            return

        # 每轮重新 open/close：不长持句柄，避免顶撞引擎侧 RotatingFileHandler
        # 在 Windows 上的 rename（持句柄会让引擎轮转失败丢日志）。
        chunk = b""
        try:
            with self._path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError as exc:
            log.debug("engine log read failed: %s", exc)
            return
        self._offset += len(chunk)

        data = self._pending + chunk
        *lines, self._pending = data.split(b"\n")
        self._pending = self._pending[-65536:]  # 防御：残段异常膨胀时截断
        for raw in lines:
            self._handle_raw_line(raw)

    # ------------------------------------------------------------------ #
    def _handle_raw_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            entry = json.loads(text)
        except ValueError:
            log.debug("engine log: non-JSON line skipped (%d chars)", len(text))
            return
        if not isinstance(entry, dict):
            return
        if str(entry.get("level", "")).upper() in _ERROR_LEVELS:
            try:
                self._on_error(entry)
            except Exception:  # noqa: BLE001 — 回调方异常不能杀死 tail
                log.debug("engine log on_error callback raised", exc_info=True)
