"""Process identity for local recovery when leases live in another process."""

from __future__ import annotations

import os
import socket
import time
import uuid

import psutil


def new_worker_id() -> str:
    # Creation time distinguishes a restarted worker from an unrelated reused PID.
    try:
        born = psutil.Process().create_time()
    except (psutil.Error, OSError, NotImplementedError):
        # Android/PRoot 等受限环境下 psutil 可能读不到 /proc/<pid>/stat；
        # born 只用于区分 PID 复用，退化成当前时间不影响恢复语义。
        born = time.time()
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}:{born}"


def owner_process_may_be_alive(turn: dict) -> bool:
    """Protect a foreign memory-mode owner unless its death can be established.

    Old rows without process identity retain restart recovery. Legacy application
    IDs have host:pid:nonce; their row creation time provides a PID reuse bound.
    Memory mode cannot prove a remote host dead and must not reap its turns.
    """
    parts = str(turn.get("owner_id") or "").split(":")
    if len(parts) not in {3, 4}:
        return False
    host, raw_pid, _nonce = parts[:3]
    try:
        pid = int(raw_pid)
        if pid <= 0:
            return False
        born = float(parts[3]) if len(parts) == 4 else None
    except ValueError:
        return False
    if host != socket.gethostname():
        return True
    try:
        process = psutil.Process(pid)
        actual = process.create_time()
        if born is not None and actual != born:
            return False
        if born is None and actual > float(turn.get("created_at") or 0):
            return False
        return process.is_running() and process.status() not in {
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        }
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True
