"""WorkBuddy session-history importer for the local desktop backend.

WorkBuddy keeps its terminal-agent conversations under ``~/.workbuddy`` in
Claude-Code-shaped JSONL files::

    ~/.workbuddy/projects/<encoded-cwd>/<session-uuid>.jsonl

plus a ``workbuddy.db`` SQLite database whose ``sessions`` table carries the
display title, the user-renamed title, the model, the expert, and a soft-delete
marker (``deleted_at``).

This module implements the P0 path from the feasibility doc: the local backend
scans and parses these files directly (no browser File System Access needed),
skips soft-deleted sessions, and hands normalized transcripts to the shared
``import_session`` persistence, so imported sessions carry the
``imported_workbuddy_*`` id prefix and can be re-opened / continued in the chat
loop. Re-importing is idempotent by external id.

Only human dialogue is imported: tool calls, tool results, reasoning, file
snapshots and session metadata are dropped, and the harness-injected
``<system-reminder>`` context that wraps the actual user question is stripped.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

__all__ = [
    "workbuddy_root",
    "discover_workbuddy_root",
    "list_workbuddy_sessions",
    "parse_workbuddy_session",
    "parse_workbuddy_sessions",
]

_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder[^>]*>[\s\S]*?</system-reminder>", re.IGNORECASE
)
# The real user question is wrapped in <user_query>; take it preferentially so
# the imported transcript never starts with the working-directory context.
_USER_QUERY_RE = re.compile(r"<user_query>([\s\S]*?)</user_query>", re.IGNORECASE)

_HEAD_BYTES = 64 * 1024

# Record types that are tool/thinking/meta noise rather than human dialogue.
_DROP_TYPES = frozenset(
    {
        "session-meta",
        "file-history-snapshot",
        "reasoning",
        "function_call",
        "function_call_result",
        "credit-usage",
        "resend-fork-notice",
    }
)

# Careful: the session table is the app's live SQLite DB; always open read-only.
_DB_URI_TEMPLATE = "file:{path}?mode=ro"


def workbuddy_root() -> Path:
    """The conventional install-time home of WorkBuddy data."""
    return Path.home() / ".workbuddy"


def discover_workbuddy_root(root: Path | None = None) -> Path | None:
    """Return ``root`` when it looks like a WorkBuddy home, else ``None``."""
    candidate = root or workbuddy_root()
    if candidate.is_dir() and (candidate / "projects").is_dir():
        return candidate
    return None


_META_TIME_FIELDS = {"created_at", "updated_at", "last_activity_at"}


_SELECT_ALL = (
    "SELECT id, title, custom_title, deleted_at, model, expert_id, "
    "created_at, updated_at, last_activity_at FROM sessions"
)
# Older / migrated WorkBuddy dbs may lack some columns; keep the metadata join
# best-effort rather than abandoning it (and the soft-delete filter) entirely.
_SELECT_MINIMAL = (
    "SELECT id, title, custom_title, deleted_at FROM sessions"
)


def _load_db_rows(root: Path) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Read session metadata from ``workbuddy.db``.

    Returns ``(rows, deleted_ids)`` where ``rows`` maps each **non-deleted**
    session uuid (the JSONL basename) to a small dict of useful columns, and
    ``deleted_ids`` is the set of uuids that were soft-deleted. Callers use the
    latter to skip the matching transcripts entirely. Any DB failure degrades to
    empty values so a corrupt or migrated DB never blocks the import of the
    JSONL transcripts themselves.
    """
    db_path = root / "workbuddy.db"
    if not db_path.is_file():
        return {}, set()
    rows: dict[str, dict[str, Any]] = {}
    deleted_ids: set[str] = set()
    try:
        con = sqlite3.connect(_DB_URI_TEMPLATE.format(path=db_path.as_posix()), uri=True)
        try:
            con.row_factory = sqlite3.Row
            try:
                cur = con.execute(_SELECT_ALL)
            except sqlite3.OperationalError:
                cur = con.execute(_SELECT_MINIMAL)
            columns = {col[0] for col in (cur.description or [])}
            for row in cur:
                deleted = row["deleted_at"] not in (None, "", 0)
                if deleted:
                    deleted_ids.add(str(row["id"]))
                    continue  # soft-deleted sessions are not imported
                meta: dict[str, Any] = {}
                if "title" in row.keys():
                    meta["title"] = row["title"] or ""
                if "custom_title" in row.keys():
                    meta["custom_title"] = row["custom_title"] or ""
                if "model" in columns:
                    meta["model"] = row["model"] or ""
                if "expert_id" in columns:
                    meta["expert_id"] = row["expert_id"] or ""
                for field_ in _META_TIME_FIELDS:
                    if field_ not in columns:
                        continue
                    value = row[field_]
                    if value not in (None, ""):
                        try:
                            meta[field_] = int(value) / 1000.0
                        except (TypeError, ValueError):
                            pass
                rows[str(row["id"])] = meta
        finally:
            con.close()
    except sqlite3.Error:
        return {}, set()
    return rows, deleted_ids


def clean_text(text: str) -> str:
    """Strip harness-injected system-reminder blocks (with or without attrs)."""
    return _SYSTEM_REMINDER_RE.sub("", text).strip()


def _extract_user_text(content: Any) -> tuple[str, bool]:
    """Reduce a user message's content blocks to text + image flag.

    WorkBuddy wraps the actual question in ``<user_query>``; extract it first,
    falling back to whatever remains after the system-reminder context is
    stripped.
    """
    text, had_images = _flatten_content_text(content)
    text = clean_text(text)
    match = _USER_QUERY_RE.search(text)
    if match:
        inner = match.group(1).strip()
        if inner:
            text = inner
    return text.strip(), had_images


def _flatten_content_text(content: Any) -> tuple[str, bool]:
    """Join the text-bearing blocks (input_text / output_text) of a content
    array. Tool/thinking/image payloads are intentionally ignored."""
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    parts: list[str] = []
    had_images = False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "image_blob_ref" or (block_type or "").startswith("image"):
            had_images = True
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts).strip(), had_images


def _message_to_import(rec: dict[str, Any]) -> dict[str, Any] | None:
    role = rec.get("role")
    if rec.get("type") != "message" or role not in ("user", "assistant"):
        return None
    if role == "user":
        content, had_images = _extract_user_text(rec.get("content"))
    else:
        content, had_images = _flatten_content_text(rec.get("content"))
        content = clean_text(content)
    if not content:
        return None
    created_at: float | None = None
    ts = rec.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        created_at = ts / 1000.0
    message: dict[str, Any] = {
        "role": role,
        "content": content,
        "created_at": created_at,
    }
    if had_images:
        message["metadata"] = {"had_images": True}
    return message


def _head_records(path: Path) -> list[dict[str, Any]]:
    """Parse only the first ~64KB of a transcript for the scan pass."""
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            chunk = fh.read(_HEAD_BYTES)
    except OSError:
        return records
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
        if len(records) >= 200:
            break
    return records


def _ai_title_from(rec: dict[str, Any]) -> str:
    for key in ("title", "aiTitle", "name"):
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _derive_title(text: str, max_len: int = 80) -> str:
    one_line = re.sub(r"\s+", " ", text).strip()
    if one_line and len(one_line) > max_len:
        return one_line[:max_len].rstrip() + "…"
    return one_line


def _latest_message_time(messages: list[dict[str, Any]]) -> float | None:
    stamps = [m["created_at"] for m in messages if m.get("created_at")]
    return max(stamps) if stamps else None


def _first_message_time(messages: list[dict[str, Any]]) -> float | None:
    for m in messages:
        if m.get("created_at"):
            return m["created_at"]
    return None


def list_workbuddy_sessions(root: Path | None = None) -> list[dict[str, Any]]:
    """Scan ``~/.workbuddy`` and list importable sessions grouped by project.

    Each returned project entry mirrors the browser adapters' ``ProjectGroup``
    shape (``cwd``, ``label``, ``sessions``) so the picker UI can stay the same
    for local and browser sources. Soft-deleted sessions are excluded thanks to
    the DB join.
    """
    base = discover_workbuddy_root(root)
    if base is None:
        return []
    db_rows, deleted_ids = _load_db_rows(base)
    groups: dict[str, dict[str, Any]] = {}

    def _ensure_group(cwd: str) -> dict[str, Any]:
        cwd = cwd or "(unknown)"
        group = groups.get(cwd)
        if group is None:
            group = {"cwd": cwd, "label": _project_label(cwd), "sessions": []}
            groups[cwd] = group
        return group

    projects_dir = base / "projects"
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for file_path in project_dir.glob("*.jsonl"):
            meta = _scan_session_file(file_path, db_rows, deleted_ids)
            if meta is None:
                continue
            _ensure_group(meta["cwd"])["sessions"].append(meta)

    ordered = [
        group
        for group in groups.values()
        if group["sessions"]
    ]
    ordered.sort(
        key=lambda g: max((s["last_modified"] for s in g["sessions"]), default=0),
        reverse=True,
    )
    for group in ordered:
        group["sessions"].sort(
            key=lambda s: s["last_modified"], reverse=True
        )
    return ordered


def _scan_session_file(
    file_path: Path,
    db_rows: dict[str, dict[str, Any]] | None,
    deleted_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    external_id = file_path.stem
    if deleted_ids and external_id in deleted_ids:
        return None  # soft-deleted in WorkBuddy's own database
    db_row = (db_rows or {}).get(external_id) or {}
    stat = file_path.stat()
    last_modified_ms = int(stat.st_mtime * 1000)

    head = _head_records(file_path)
    cwd = ""
    ai_title = ""
    first_user = ""
    first_ts: float | None = None
    for rec in head:
        if rec.get("type") == "ai-title" and not ai_title:
            ai_title = _ai_title_from(rec)
        if rec.get("type") == "message":
            if isinstance(rec.get("cwd"), str) and not cwd:
                cwd = rec["cwd"]
            if rec.get("role") == "user":
                text, _ = _extract_user_text(rec.get("content"))
                if text and not first_user:
                    first_user = text
                ts = rec.get("timestamp")
                if first_ts is None and isinstance(ts, (int, float)) and ts > 0:
                    first_ts = ts / 1000.0

    title = (
        db_row.get("custom_title")
        or ai_title
        or db_row.get("title")
        or _derive_title(first_user)
    )
    created_at = (
        db_row.get("created_at")
        or first_ts
        or last_modified_ms / 1000.0
    )
    updated_at = db_row.get("updated_at") or created_at
    return {
        "external_id": external_id,
        "title": title or external_id,
        "cwd": cwd,
        "date": _epoch_ms_to_date(last_modified_ms),
        "last_modified": last_modified_ms,
        "size_bytes": stat.st_size,
        "created_at": created_at,
        "updated_at": updated_at,
        "model": db_row.get("model") or "",
    }


def parse_workbuddy_session(
    root: Path | None, external_id: str
) -> dict[str, Any] | None:
    """Fully parse one transcript into the imported-session payload shape.

    Returns ``None`` when the session cannot be located or contains no human
    messages. Summary fields mirror the browser adapters' ``NormalizedSession``.
    """
    base = discover_workbuddy_root(root)
    if base is None:
        return None
    candidates = [
        p
        for p in (base / "projects").glob("**/*.jsonl")
        if p.stem == external_id
    ]
    if not candidates:
        return None
    file_path = candidates[0]
    db_rows, deleted_ids = _load_db_rows(base)
    if external_id in deleted_ids:
        return None  # soft-deleted in WorkBuddy's own database
    db_row = db_rows.get(external_id) or {}
    stat = file_path.stat()

    messages: list[dict[str, Any]] = []
    ai_title = ""
    cwd = ""
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                rtype = rec.get("type")
                if rtype == "ai-title" and not ai_title:
                    ai_title = _ai_title_from(rec)
                    continue
                if rtype in _DROP_TYPES:
                    continue
                if not cwd and isinstance(rec.get("cwd"), str):
                    cwd = rec["cwd"]
                message = _message_to_import(rec)
                if message:
                    messages.append(message)
    except OSError:
        return None

    if not messages:
        return None
    fallback_ts = stat.st_mtime
    first_ts = (
        db_row.get("created_at")
        or _first_message_time(messages)
        or fallback_ts
    )
    last_ts = (
        db_row.get("updated_at")
        or _latest_message_time(messages)
        or fallback_ts
    )
    title = (
        db_row.get("custom_title")
        or ai_title
        or db_row.get("title")
        or _derive_title(messages[0].get("content") or "")
    )
    return {
        "external_id": external_id,
        "title": title or external_id,
        "source_cwd": cwd,
        "created_at": first_ts,
        "updated_at": last_ts,
        "messages": messages,
    }


def parse_workbuddy_sessions(
    root: Path | None, external_ids: list[str]
) -> list[dict[str, Any]]:
    """Parse many sessions; unreadable/empty ones are skipped, never fatal."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for external_id in external_ids:
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)
        parsed = parse_workbuddy_session(root, external_id)
        if parsed and parsed["messages"]:
            out.append(parsed)
    return out


def _project_label(cwd: str) -> str:
    cleaned = cwd.replace("\\", "/").rstrip("/")
    parts = [p for p in cleaned.split("/") if p]
    return parts[-1] if parts else (cwd or "(unknown)")


def _epoch_ms_to_date(ms: int) -> str:
    import datetime

    try:
        dt = datetime.datetime.fromtimestamp(ms / 1000.0)
    except (OverflowError, OSError, ValueError):
        return ""
    return dt.strftime("%Y-%m-%d")
