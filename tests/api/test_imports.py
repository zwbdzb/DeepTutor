"""Tests for the chat-history import endpoints.

The handlers are exercised directly (no TestClient) with the per-user store
factory monkeypatched to a tmp database, so nothing touches the real chat DB.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from deeptutor.api.routers import imports as imports_router
from deeptutor.api.routers.imports import (
    ChatHistoryImportRequest,
    import_chat_history,
    list_imported_chat_history,
)
from deeptutor.services.session.sqlite_store import SQLiteSessionStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SQLiteSessionStore:
    instance = SQLiteSessionStore(db_path=tmp_path / "test.db")
    monkeypatch.setattr(imports_router, "get_sqlite_session_store", lambda: instance)
    return instance


def _payload(
    external_id: str = "s1",
    messages=None,
    agent_id: str = "",
    agent_name: str = "",
) -> ChatHistoryImportRequest:
    return ChatHistoryImportRequest(
        source="codex",
        agent_id=agent_id,
        agent_name=agent_name,
        sessions=[
            {
                "external_id": external_id,
                "title": "T",
                "source_cwd": "/p",
                "created_at": 1.0,
                "updated_at": 2.0,
                "messages": messages
                if messages is not None
                else [
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": "a"},
                ],
            }
        ],
    )


def _imported_prefs(store: SQLiteSessionStore) -> dict:
    listed = asyncio.run(list_imported_chat_history(limit=50, offset=0))
    return listed["sessions"][0]["preferences"]["import"]


def test_source_validation_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        ChatHistoryImportRequest(source="opencode", sessions=[])


def test_source_is_normalized_lowercase() -> None:
    assert ChatHistoryImportRequest(source="Claude_Code", sessions=[]).source == ("claude_code")
    assert ChatHistoryImportRequest(source="ChatGPT", sessions=[]).source == "chatgpt"


def test_import_endpoint_persists_and_dedups(store: SQLiteSessionStore) -> None:
    res = asyncio.run(import_chat_history(_payload()))
    assert res["imported"] == 1
    assert res["skipped"] == 0

    listed = asyncio.run(list_imported_chat_history(limit=50, offset=0))
    assert len(listed["sessions"]) == 1
    assert listed["sessions"][0]["message_count"] == 2

    # Re-import the same session → deduped, not duplicated.
    again = asyncio.run(import_chat_history(_payload()))
    assert again["imported"] == 0
    assert again["skipped"] == 1
    assert len(asyncio.run(list_imported_chat_history(limit=50, offset=0))["sessions"]) == 1


def test_empty_content_messages_are_dropped(store: SQLiteSessionStore) -> None:
    res = asyncio.run(
        import_chat_history(
            _payload(
                messages=[
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": "   "},  # whitespace only
                    {"role": "assistant", "content": "a"},
                ]
            )
        )
    )
    assert res["imported"] == 1
    listed = asyncio.run(list_imported_chat_history(limit=50, offset=0))
    assert listed["sessions"][0]["message_count"] == 2


def test_session_with_only_empty_messages_is_skipped(
    store: SQLiteSessionStore,
) -> None:
    res = asyncio.run(import_chat_history(_payload(messages=[{"role": "user", "content": "  "}])))
    assert res["imported"] == 0
    assert res["skipped"] == 1


def test_import_rejects_empty_request(store: SQLiteSessionStore) -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(import_chat_history(ChatHistoryImportRequest(source="codex", sessions=[])))
    assert exc.value.status_code == 400


def test_agent_attribution_persisted(store: SQLiteSessionStore) -> None:
    asyncio.run(import_chat_history(_payload(agent_id="codex-a1", agent_name="Research")))
    meta = _imported_prefs(store)
    assert meta["agent_id"] == "codex-a1"
    assert meta["agent_name"] == "Research"
    assert meta["source"] == "codex"


def test_reimport_backfills_agent_attribution(store: SQLiteSessionStore) -> None:
    # First import had no agent (legacy client) — no attribution stored.
    asyncio.run(import_chat_history(_payload()))
    assert "agent_id" not in _imported_prefs(store)

    # Re-syncing under an agent backfills attribution without duplicating the
    # session or re-adding messages.
    again = asyncio.run(import_chat_history(_payload(agent_id="codex-a1", agent_name="Research")))
    assert again["imported"] == 0
    assert again["skipped"] == 1
    listed = asyncio.run(list_imported_chat_history(limit=50, offset=0))
    assert len(listed["sessions"]) == 1
    assert listed["sessions"][0]["message_count"] == 2
    meta = listed["sessions"][0]["preferences"]["import"]
    assert meta["agent_id"] == "codex-a1"
    assert meta["agent_name"] == "Research"


def test_agent_rename_propagates_on_resync(store: SQLiteSessionStore) -> None:
    asyncio.run(import_chat_history(_payload(agent_id="codex-a1", agent_name="Old name")))
    asyncio.run(import_chat_history(_payload(agent_id="codex-a1", agent_name="New name")))
    assert _imported_prefs(store)["agent_name"] == "New name"


# ── WorkBuddy local-scan endpoints ────────────────────────────────────────


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.client = _FakeClient(host)


def _fake_wb_home(tmp_path: Path) -> Path:
    """A one-session WorkBuddy home for the local endpoints."""
    import json

    root = tmp_path / ".wb"
    (root / "projects" / "c-repo").mkdir(parents=True, exist_ok=True)
    session = "aaaaaaaa-0000-0000-0000-000000000001"
    lines = [
        json.dumps(
            {
                "id": "1",
                "timestamp": 1000,
                "type": "message",
                "role": "user",
                "sessionId": session,
                "cwd": "/repo",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "<system-reminder data-role=\"user-context\">ctx</system-reminder>"
                            "<user_query>hello world</user_query>"
                        ),
                    }
                ],
            }
        ),
        json.dumps(
            {
                "id": "2",
                "timestamp": 2000,
                "type": "message",
                "role": "assistant",
                "sessionId": session,
                "cwd": "/repo",
                "content": [{"type": "output_text", "text": "hi"}],
            }
        ),
    ]
    (root / "projects" / "c-repo" / f"{session}.jsonl").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    return root


def test_workbuddy_source_accepted_by_browser_endpoint() -> None:
    payload = ChatHistoryImportRequest(
        source="WorkBuddy",  # normalized to lower snake
        sessions=[
            {
                "external_id": "s1",
                "title": "T",
                "source_cwd": "/p",
                "created_at": 1.0,
                "updated_at": 2.0,
                "messages": [{"role": "user", "content": "q"}],
            }
        ],
    )
    assert payload.source == "workbuddy"


def test_local_sources_reports_workbuddy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deeptutor.api.routers import imports as imports_router
    from deeptutor.api.routers.imports import list_local_import_sources
    from deeptutor.services.chat_import import workbuddy as wb

    monkeypatch.setattr(wb, "discover_workbuddy_root", lambda _root=None: _fake_wb_home(tmp_path))
    res = asyncio.run(list_local_import_sources(_FakeRequest()))
    assert len(res["sources"]) == 1
    src = res["sources"][0]
    assert src["source"] == "workbuddy"
    assert len(src["projects"]) == 1
    assert src["projects"][0]["label"] == "repo"
    assert len(src["projects"][0]["sessions"]) == 1


def test_local_sources_blocked_off_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deeptutor.api.routers import imports as imports_router
    from deeptutor.api.routers.imports import list_local_import_sources
    from deeptutor.services.chat_import import workbuddy as wb

    monkeypatch.setattr(wb, "discover_workbuddy_root", lambda _root=None: _fake_wb_home(tmp_path))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(list_local_import_sources(_FakeRequest("203.0.113.9")))
    assert exc.value.status_code == 403


def test_local_import_end_to_end_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: SQLiteSessionStore
) -> None:
    from deeptutor.api.routers.imports import LocalImportRequest, import_local_sources
    from deeptutor.services.chat_import import workbuddy as wb

    session = "aaaaaaaa-0000-0000-0000-000000000001"
    monkeypatch.setattr(wb, "discover_workbuddy_root", lambda _root=None: _fake_wb_home(tmp_path))

    first = asyncio.run(
        import_local_sources(
            LocalImportRequest(source="workbuddy", agent_name="WorkBuddy", session_ids=[session]),
            _FakeRequest(),
        )
    )
    assert first["imported"] == 1
    assert first["skipped"] == 0

    # Imported under the workbuddy namespace, re-openable like native chats.
    listed = asyncio.run(list_imported_chat_history(limit=50, offset=0))
    assert listed["sessions"][0]["id"].startswith("imported_workbuddy_")
    assert listed["sessions"][0]["message_count"] == 2
    assert listed["sessions"][0]["preferences"]["import"]["agent_name"] == "WorkBuddy"

    # Re-import → idempotent skip.
    again = asyncio.run(
        import_local_sources(
            LocalImportRequest(source="workbuddy", session_ids=[session]),
            _FakeRequest(),
        )
    )
    assert again["imported"] == 0
    assert again["skipped"] == 1
    assert len(asyncio.run(list_imported_chat_history(limit=50, offset=0))["sessions"]) == 1


def test_local_import_requires_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deeptutor.api.routers.imports import LocalImportRequest, import_local_sources
    from deeptutor.services.chat_import import workbuddy as wb

    monkeypatch.setattr(wb, "discover_workbuddy_root", lambda _root=None: _fake_wb_home(tmp_path))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            import_local_sources(
                LocalImportRequest(source="workbuddy", session_ids=["x"]),
                _FakeRequest("203.0.113.9"),
            )
        )
    assert exc.value.status_code == 403


def test_local_import_unknown_source_rejected() -> None:
    from pydantic import ValidationError

    from deeptutor.api.routers.imports import LocalImportRequest

    with pytest.raises(ValidationError):
        LocalImportRequest(source="claude_code", session_ids=["x"])
