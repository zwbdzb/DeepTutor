"""Read-only loaders for the rows a turn's latency is derived from.

Every connection uses ``mode=ro`` plus ``PRAGMA query_only`` (the same posture
as ``services/memory/snapshot/adapters.py``): an analysis query must never
checkpoint a WAL, migrate a schema, or hold a write lock on a database a live
turn is using.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Content deltas dominate ``turn_events`` (one row per streamed chunk — 507 of
# 515 rows in one real turn), so the analyzer reads only event types that carry
# timing structure. Anything added here must stay bounded per turn.
TIMING_EVENT_TYPES: tuple[str, ...] = (
    "session",
    "stage_start",
    "stage_end",
    "progress",
    "tool_call",
    "tool_result",
    "wait_for_input",
    "result",
    "done",
    "error",
)

_TURN_COLUMNS = (
    "id, session_id, capability, status, error, created_at, updated_at, "
    "finished_at, failure_code, retryable"
)


def chat_db_path() -> Path:
    """The chat database for the active owner scope."""
    from deeptutor.services.path_service import get_path_service

    return get_path_service().get_chat_history_db()


def usage_db_path() -> Path:
    """The per-owner LLM call ledger."""
    from deeptutor.multi_user.paths import get_owner_path_service

    return get_owner_path_service().get_user_root() / "usage.sqlite3"


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = 1")
        yield conn
    finally:
        conn.close()


@contextmanager
def _connect_optional(path: Path) -> Iterator[sqlite3.Connection | None]:
    """同 ``_connect``，但数据库不存在时产出 ``None``。

    干净安装还没有数据库；这种情况下分析接口应当回答「没有 turn」，
    而不是让整个请求 500。
    """
    if not path.exists():
        yield None
        return
    with _connect(path) as conn:
        yield conn


def _json_object(raw: Any) -> dict[str, Any]:
    """Parse a JSON column that may be empty or malformed (schema default '')."""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _turn_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "turn_id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "capability": str(row["capability"] or ""),
        "status": str(row["status"] or ""),
        "error": str(row["error"] or ""),
        "created_at": float(row["created_at"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
        "finished_at": float(row["finished_at"]) if row["finished_at"] else None,
        "failure_code": str(row["failure_code"] or ""),
        "retryable": bool(row["retryable"]),
    }


def load_turn(turn_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    with _connect_optional(db_path or chat_db_path()) as conn:
        if conn is None:
            return None
        row = conn.execute(
            f"SELECT {_TURN_COLUMNS} FROM turns WHERE id = ?", (turn_id,)
        ).fetchone()
    return _turn_row(row) if row else None


def list_turns(
    session_id: str, *, limit: int = 20, db_path: Path | None = None
) -> list[dict[str, Any]]:
    with _connect_optional(db_path or chat_db_path()) as conn:
        if conn is None:
            return []
        rows = conn.execute(
            f"SELECT {_TURN_COLUMNS} FROM turns WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, int(limit)),
        ).fetchall()
    return [_turn_row(row) for row in rows]


def list_recent_turns(
    *, limit: int = 20, session_id: str = "", db_path: Path | None = None
) -> list[dict[str, Any]]:
    """Newest turns across every conversation, or just one of them."""
    sql = f"SELECT {_TURN_COLUMNS} FROM turns"
    params: list[Any] = []
    if session_id:
        sql += " WHERE session_id = ?"
        params.append(session_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    with _connect_optional(db_path or chat_db_path()) as conn:
        if conn is None:
            return []
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_turn_row(row) for row in rows]


def latest_session_id(*, db_path: Path | None = None) -> str | None:
    """Most recently updated conversation that actually has turns."""
    with _connect_optional(db_path or chat_db_path()) as conn:
        if conn is None:
            return None
        row = conn.execute(
            "SELECT session_id FROM turns ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return str(row["session_id"]) if row else None


def load_events(turn_id: str, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in TIMING_EVENT_TYPES)
    sql = (
        "SELECT seq, type, source, stage, metadata_json, timestamp "
        "FROM turn_events "
        f"WHERE turn_id = ? AND type IN ({placeholders}) "
        "ORDER BY seq"
    )
    with _connect_optional(db_path or chat_db_path()) as conn:
        if conn is None:
            return []
        rows = conn.execute(sql, (turn_id, *TIMING_EVENT_TYPES)).fetchall()
    return [
        {
            "seq": int(row["seq"]),
            "type": str(row["type"]),
            "source": str(row["source"] or ""),
            "stage": str(row["stage"] or ""),
            "timestamp": float(row["timestamp"]),
            "metadata": _json_object(row["metadata_json"]),
        }
        for row in rows
    ]


def load_prompts(
    turn_ids: list[str], *, db_path: Path | None = None
) -> dict[str, dict[str, Any]]:
    """turn_id -> 触发它的那条用户消息。

    存着两条独立的关联，两条都用：turn 指向自己的 assistant 消息，那条消息的
    ``parent_message_id`` 就是用户消息；同时 DONE 信封直接带 ``user_message_id``。
    重新生成或中途取消的 turn 可能只剩其中一条。
    """
    ids = [str(item) for item in turn_ids if item]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    prompts: dict[str, dict[str, Any]] = {}
    with _connect_optional(db_path or chat_db_path()) as conn:
        if conn is None:
            return {}
        assistant_by_turn: dict[str, Any] = {}
        for row in conn.execute(
            f"SELECT id, assistant_message_id FROM turns WHERE id IN ({placeholders})",
            tuple(ids),
        ):
            assistant_by_turn[str(row["id"])] = row["assistant_message_id"]

        parent_by_assistant: dict[Any, Any] = {}
        assistant_ids = [value for value in assistant_by_turn.values() if value]
        if assistant_ids:
            marks = ",".join("?" for _ in assistant_ids)
            for row in conn.execute(
                f"SELECT id, parent_message_id FROM messages WHERE id IN ({marks})",
                tuple(assistant_ids),
            ):
                parent_by_assistant[row["id"]] = row["parent_message_id"]

        done_user_ids: dict[str, Any] = {}
        for row in conn.execute(
            "SELECT turn_id, metadata_json FROM turn_events "
            f"WHERE turn_id IN ({placeholders}) AND type = 'done'",
            tuple(ids),
        ):
            meta = _json_object(row["metadata_json"])
            if meta.get("user_message_id"):
                done_user_ids[str(row["turn_id"])] = meta["user_message_id"]

        user_ids: set[Any] = set(done_user_ids.values())
        for assistant_id in assistant_by_turn.values():
            parent = parent_by_assistant.get(assistant_id)
            if parent:
                user_ids.add(parent)
        text_by_id: dict[Any, str] = {}
        if user_ids:
            marks = ",".join("?" for _ in user_ids)
            for row in conn.execute(
                f"SELECT id, role, content FROM messages WHERE id IN ({marks})",
                tuple(user_ids),
            ):
                if str(row["role"] or "") == "user":
                    text_by_id[row["id"]] = str(row["content"] or "")

    for turn_id in ids:
        message_id = parent_by_assistant.get(assistant_by_turn.get(turn_id))
        if message_id is None or message_id not in text_by_id:
            message_id = done_user_ids.get(turn_id)
        text = text_by_id.get(message_id, "")
        if message_id is not None or text:
            prompts[turn_id] = {"user_message_id": message_id, "user_message": text}
    return prompts


def load_llm_calls(
    turn_id: str, *, usage_path: Path | None = None
) -> list[dict[str, Any]]:
    path = usage_path or usage_db_path()
    if not path.exists():
        return []
    try:
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT call_id, started_at, source, usage_json "
                "FROM llm_calls WHERE turn_id = ? ORDER BY started_at",
                (turn_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        # A store from before the ledger existed simply has no model timings.
        logger.debug("llm_calls unavailable: %s", exc)
        return []
    return [
        {
            "call_id": str(row["call_id"]),
            "started_at": float(row["started_at"] or 0.0),
            "source": str(row["source"] or ""),
            "usage": _json_object(row["usage_json"]),
        }
        for row in rows
    ]
