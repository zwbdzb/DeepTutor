"""Tests for telemetry direct-report + engine JSONL incremental tail.

覆盖（2026-10-21 方案三道护栏 + 增量三边界）：
- EngineLogTail：启动不追旧账 / 追加触发回调 / 轮转重置 / 半行拼接 /
  非 JSON 行跳过 / 回调异常不杀死线程
- TelemetryReporter：成功清零失败计数 / 连败熔断 / error 限速 /
  flush_exit 传递 props（不再上报事件丢失计数）
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import urllib.request

import pytest

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop.engine_log_tail import EngineLogTail
from desktop.telemetry_reporter import TelemetryReporter


# --------------------------------------------------------------------------- #
# EngineLogTail
# --------------------------------------------------------------------------- #
class ErrorCollector:
    def __init__(self) -> None:
        self.errors: list[dict] = []

    def __call__(self, entry: dict) -> None:
        self.errors.append(entry)


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for entry in entries:
            handle.write((json.dumps(entry) + "\n").encode("utf-8"))


def run_polls(tail: EngineLogTail, cycles: int = 2) -> None:
    for _ in range(cycles):
        tail._poll_once()


def test_tail_skips_preexisting_content_and_reports_new_errors(tmp_path: Path) -> None:
    log_path = tmp_path / "deeptutor.jsonl"
    write_jsonl(
        log_path,
        [
            {"level": "ERROR", "logger": "old", "message": "历史错误，启动时不应被上报"},
            {"level": "INFO", "logger": "x", "message": "hi"},
        ],
    )
    collector = ErrorCollector()
    tail = EngineLogTail(log_path, collector, poll_interval=0.05)
    tail.start()
    try:
        assert collector.errors == []  # 启动 offset=末尾，不追旧账
        write_jsonl(
            log_path,
            [
                {"level": "WARNING", "logger": "w", "message": "warn"},
                {"level": "ERROR", "logger": "mod.a", "message": "boom"},
            ],
        )
        run_polls(tail)
        assert [e["logger"] for e in collector.errors] == ["mod.a"]
        assert collector.errors[0]["message"] == "boom"
    finally:
        tail.stop()


def test_tail_handles_rotation(tmp_path: Path) -> None:
    log_path = tmp_path / "deeptutor.jsonl"
    write_jsonl(log_path, [{"level": "ERROR", "logger": "first", "message": "f"}])
    collector = ErrorCollector()
    tail = EngineLogTail(log_path, collector, poll_interval=0.05)
    tail.start()
    try:
        run_polls(tail)
        # 模拟 RotatingFileHandler：rename 旧文件 → 新空文件 → 追加新错误
        log_path.rename(log_path.parent / "deeptutor.jsonl.1")
        log_path.write_bytes(b"")
        write_jsonl(log_path, [{"level": "ERROR", "logger": "second", "message": "s"}])
        run_polls(tail)
        assert [e["logger"] for e in collector.errors] == ["second"]
    finally:
        tail.stop()


def test_tail_buffers_partial_line(tmp_path: Path) -> None:
    log_path = tmp_path / "deeptutor.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    collector = ErrorCollector()
    tail = EngineLogTail(log_path, collector, poll_interval=0.05)
    tail.start()
    try:
        # 半行：完整行从第 15 字节截断（无换行符），不能解析、不能误报
        full = json.dumps({"level": "ERROR", "logger": "half", "message": "done"})
        raw = full.encode("utf-8")
        with log_path.open("ab") as handle:
            handle.write(raw[:15])
        run_polls(tail)
        assert collector.errors == []
        # 补上后半截 + 换行 → 拼接成完整行后触发
        with log_path.open("ab") as handle:
            handle.write(raw[15:] + b"\n")
        run_polls(tail)
        assert [e["logger"] for e in collector.errors] == ["half"]
    finally:
        tail.stop()


def test_tail_skips_non_json_and_survives_callback_errors(tmp_path: Path) -> None:
    log_path = tmp_path / "deeptutor.jsonl"
    calls = {"n": 0}

    def flaky(entry: dict) -> None:
        calls["n"] += 1
        raise RuntimeError("callback must not kill the tail")

    write_jsonl(
        log_path,
        [
            {"level": "ERROR", "logger": "a", "message": "x"},
            {"level": "ERROR", "logger": "b", "message": "y"},
        ],
    )
    tail = EngineLogTail(log_path, flaky, poll_interval=0.05)
    tail.start()
    try:
        # 先消费掉启动前的内容？不——启动 offset=末尾。追加新行（含非 JSON）
        with log_path.open("ab") as handle:
            handle.write(b"this is not json\n")
            handle.write((json.dumps({"level": "CRITICAL", "logger": "c"}).encode("utf-8")) + b"\n")
        run_polls(tail)
        assert calls["n"] == 1  # 非 JSON 跳过；CRITICAL 计入；回调异常未杀线程
    finally:
        tail.stop()


# --------------------------------------------------------------------------- #
# TelemetryReporter
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        return b""


def make_reporter(monkeypatch, endpoint="https://t.example/events", **kwargs):
    sent: list[dict] = []

    def fake_urlopen(request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        sent.append(body)
        return FakeResponse()

    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *args: SimpleNamespace(open=fake_urlopen)
    )
    reporter = TelemetryReporter(endpoint, {"machine_id": "d", "os": "test"}, **kwargs)
    return reporter, sent


def test_report_sends_batch_envelope_with_base_props(monkeypatch) -> None:
    reporter, sent = make_reporter(monkeypatch)
    reporter.flush_exit("error_backend", {"logger": "m"})
    reporter.flush_exit("app_exit", {"reason": "window_closed"})
    assert len(sent) == 2
    event = sent[0]["events"][0]
    assert event["name"] == "error_backend"
    assert event["machine_id"] == "d"
    assert event["props"] == {"logger": "m"}
    assert "lost_before" not in event
    assert sent[1]["events"][0]["name"] == "app_exit"


def test_consecutive_failures_trip_circuit_breaker(monkeypatch) -> None:
    calls = {"n": 0}

    def failing_urlopen(request, timeout=None):
        calls["n"] += 1
        raise OSError("connection refused")

    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *args: SimpleNamespace(open=failing_urlopen)
    )
    reporter = TelemetryReporter(
        "https://t.example/events", {}, fail_threshold=3, mute_seconds=600.0
    )
    for _ in range(3):
        reporter._post_sync("error_backend", {}, "", 1792576514)
    assert calls["n"] == 3
    assert reporter._muted_until > 0
    # 熔断窗口内：同步与异步路径都静默丢弃，不再打网络
    reporter._post_sync("app_exit", {}, "", 1792576514)
    reporter.flush_exit("error_backend", {})
    assert calls["n"] == 3


def test_error_rate_limit_drops_excess(monkeypatch) -> None:
    calls = {"n": 0}

    def failing_urlopen(request, timeout=None):
        calls["n"] += 1
        raise OSError("down")

    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *args: SimpleNamespace(open=failing_urlopen)
    )
    reporter = TelemetryReporter(
        "https://t.example/events", {}, error_rate_limit=2, fail_threshold=100
    )
    for i in range(4):
        reporter._post_sync("error_backend", {"i": i}, "", 1792576514)
    assert calls["n"] == 2  # 前两条真实发出（失败），后两条被限速丢弃
    # 非 error 前缀不受 error 限速影响
    reporter._post_sync("app_exit", {}, "", 1792576514)
    assert calls["n"] == 3


def test_disabled_reporter_is_noop() -> None:
    reporter = TelemetryReporter("", {"machine_id": "d"})
    reporter.report("error_backend", {})  # 不应抛异常、不应发请求
    reporter.flush_exit("app_exit", {})
    assert reporter.enabled is False
