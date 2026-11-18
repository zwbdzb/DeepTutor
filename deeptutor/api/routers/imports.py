"""
Import chat histories from ChatGPT exports, external coding CLIs (Claude
Code, Codex), and the desktop backend's local scan of WorkBuddy, into the
user's learning space as normal, re-openable sessions.

Reading the user's local ``~/.claude`` / ``~/.codex`` or ChatGPT export happens
in the browser — those files live on the user's machine, not the server. The
browser normalizes each conversation to the small JSON shape below and POSTs it
here; this router only validates and persists. Imported sessions share the
session tables with native chats (so the chat loop can re-open and continue
them) but carry an ``imported_`` id prefix that keeps them in their own Space
category. Re-importing the same folder is idempotent (dedup by id).

WorkBuddy is the exception: on the desktop build the backend runs on the user's
machine, so we scan and parse ``~/.workbuddy`` server-side (no File System
Access dependency) through the loopback-only ``/local-sources`` and
``/local-import`` endpoints, normalized to the same payload shape and persisted
through the same path.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from deeptutor.services.session import (
    get_sqlite_session_store,
    make_imported_session_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Browser adapters emit these; reject anything else so a malformed payload can
# never seed an unsupported provider category.
_ALLOWED_SOURCES = {"chatgpt", "claude_code", "codex", "workbuddy"}

# Defensive ceilings — a single import request should never grow unbounded.
_MAX_SESSIONS_PER_REQUEST = 1000
_MAX_MESSAGES_PER_SESSION = 10000

# The local backend may only read the user's files when the request itself
# comes from this machine (the desktop app / a same-host browser).
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class ImportedMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = ""
    created_at: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImportedSession(BaseModel):
    external_id: str = Field(..., min_length=1, max_length=256)
    title: str = ""
    source_cwd: str = ""
    created_at: float
    updated_at: float
    messages: list[ImportedMessage] = Field(default_factory=list)


class ChatHistoryImportRequest(BaseModel):
    source: str = Field(..., min_length=1)
    # All sessions in one request belong to one agent (a named, scoped slice of
    # a source folder). Empty when the client hasn't adopted the agent model yet
    # — the backend stays backwards-compatible by simply omitting attribution.
    agent_id: str = Field(default="", max_length=256)
    agent_name: str = Field(default="", max_length=256)
    sessions: list[ImportedSession] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def _normalize_source(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in _ALLOWED_SOURCES:
            raise ValueError(f"Unsupported import source: {value!r}")
        return normalized


class LocalImportRequest(BaseModel):
    """Selection of local sessions to import, identified by external id.

    The server does the file parsing for local sources (the +client only sends
    the ids it listed from ``/local-sources``), then persists through the same
    idempotent path as the browser adapters.
    """

    source: str = Field(..., min_length=1)
    agent_name: str = Field(default="", max_length=256)
    session_ids: list[str] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def _normalize_source(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized != "workbuddy":
            raise ValueError(f"Unsupported local import source: {value!r}")
        return normalized


def _require_loopback(request: Request) -> None:
    host = (request.client.host if request.client else "") or ""
    if host.lower() not in _LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="Local source import is only available from this machine",
        )


async def _persist_imports(
    source: str,
    agent_id: str,
    agent_name: str,
    sessions: list[ImportedSession],
) -> dict[str, Any]:
    store = get_sqlite_session_store()
    imported = 0
    skipped = 0
    results: list[dict[str, Any]] = []

    for incoming in sessions:
        # Drop content-less rows (e.g. tool-only turns the adapter could not
        # reduce to text) so the transcript stays a clean human conversation.
        messages = [m for m in incoming.messages if (m.content or "").strip()]
        if not messages:
            skipped += 1
            results.append(
                {"external_id": incoming.external_id, "imported": False, "reason": "empty"}
            )
            continue
        if len(messages) > _MAX_MESSAGES_PER_SESSION:
            messages = messages[:_MAX_MESSAGES_PER_SESSION]

        session_id = make_imported_session_id(source, incoming.external_id)
        import_meta: dict[str, Any] = {
            "source": source,
            "source_cwd": incoming.source_cwd,
            "external_id": incoming.external_id,
        }
        if agent_id:
            import_meta["agent_id"] = agent_id
        if agent_name:
            import_meta["agent_name"] = agent_name
        preferences = {"import": import_meta}
        try:
            result = await store.import_session(
                session_id,
                incoming.title,
                incoming.created_at,
                incoming.updated_at,
                preferences,
                [m.model_dump() for m in messages],
            )
        except Exception:
            logger.exception("failed to import session %s", incoming.external_id)
            skipped += 1
            results.append(
                {"external_id": incoming.external_id, "imported": False, "reason": "error"}
            )
            continue

        if result.get("imported"):
            imported += 1
        else:
            skipped += 1
        results.append({"external_id": incoming.external_id, **result})

    return {"imported": imported, "skipped": skipped, "sessions": results}


@router.post("/chat-history")
async def import_chat_history(payload: ChatHistoryImportRequest) -> dict[str, Any]:
    if not payload.sessions:
        raise HTTPException(status_code=400, detail="No sessions to import")
    if len(payload.sessions) > _MAX_SESSIONS_PER_REQUEST:
        raise HTTPException(
            status_code=413,
            detail=f"Too many sessions in one request (max {_MAX_SESSIONS_PER_REQUEST})",
        )
    return await _persist_imports(
        payload.source,
        payload.agent_id,
        payload.agent_name,
        payload.sessions,
    )


@router.post("/local-import")
async def import_local_sources(
    payload: LocalImportRequest, request: Request
) -> dict[str, Any]:
    """Import selected locally-discovered sessions.

    The browser sends the source + the ``session_ids`` it listed via
    ``/local-sources``; we parse the transcripts on this machine and persist
    them idempotently.
    """
    _require_loopback(request)
    if not payload.session_ids:
        raise HTTPException(status_code=400, detail="No sessions to import")

    from deeptutor.services.chat_import.workbuddy import (
        discover_workbuddy_root,
        parse_workbuddy_sessions,
    )

    root = discover_workbuddy_root()
    if root is None:
        raise HTTPException(
            status_code=404,
            detail="WorkBuddy data not found on this machine",
        )
    parsed = parse_workbuddy_sessions(root, payload.session_ids)
    if len(parsed) > _MAX_SESSIONS_PER_REQUEST:
        parsed = parsed[:_MAX_SESSIONS_PER_REQUEST]
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="No readable WorkBuddy sessions matched the selection",
        )
    incoming = [ImportedSession(**item) for item in parsed]
    return await _persist_imports(payload.source, "", payload.agent_name, incoming)


@router.get("/local-sources")
async def list_local_import_sources(request: Request) -> dict[str, Any]:
    """Discover synchronizable agent sources on this machine (loopback only).

    Currently WorkBuddy. This is the desktop-oriented counterpart of the
    browser File System Access flow: no hidden-folder picking, and server-side
    reads can consult the source's SQLite metadata (soft-delete flags, real
    titles) that a pure file scan cannot see.
    """
    _require_loopback(request)
    from deeptutor.services.chat_import.workbuddy import (
        discover_workbuddy_root,
        list_workbuddy_sessions,
    )

    sources: list[dict[str, Any]] = []
    root = discover_workbuddy_root()
    if root is not None:
        sources.append(
            {
                "source": "workbuddy",
                "label": "WorkBuddy",
                "root": str(root),
                "projects": list_workbuddy_sessions(root),
            }
        )
    return {"sources": sources}


@router.get("/chat-history")
async def list_imported_chat_history(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    store = get_sqlite_session_store()
    sessions = await store.list_imported_sessions(limit=limit, offset=offset)
    return {"sessions": sessions}
