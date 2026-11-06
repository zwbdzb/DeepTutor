"""Tests for the WorkBuddy local importer (``services/chat_import/workbuddy.py``).

Uses a synthetic ``~/.workbuddy``-shaped fixture tree (JSONL transcripts + a
small ``workbuddy.db``), so nothing touches the real machine data.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from deeptutor.services.chat_import import workbuddy

# Minimal realistic transcript: tool/thinking noise should be dropped, only
# user/assistant message rows survive, and the user question is wrapped in a
# system-reminder context block that must be stripped.
SESSION_A = "aaaaaaaa-0000-0000-0000-000000000001"
SESSION_B = "bbbbbbbb-0000-0000-0000-000000000002"


def _msg(role: str, text: str, ts_ms: int, *, cwd: str = "/repo") -> dict:
    return {
        "id": f"id-{role}-{ts_ms}",
        "timestamp": ts_ms,
        "type": "message",
        "role": role,
        "sessionId": SESSION_A,
        "cwd": cwd,
        "content": [{"type": "text" if role == "user" else "output_text", "text": text}],
    }


def _user_with_context(query: str, ts_ms: int) -> dict:
    return {
        "id": f"id-user-{ts_ms}",
        "timestamp": ts_ms,
        "type": "message",
        "role": "user",
        "sessionId": SESSION_A,
        "cwd": "/repo",
        "content": [
            {
                "type": "input_text",
                "text": (
                    "<system-reminder data-role=\"user-context\">\n"
                    "<user_info>OS: win32 | Workspace Folder: /repo</user_info>\n"
                    "</system-reminder>\n"
                    "<system-reminder data-role=\"additional-data\">\n"
                    "<current_time>2026-01-01</current_time>\n"
                    "</system-reminder>\n"
                    f"<user_query>{query}</user_query>\n"
                ),
            }
        ],
    }


def _ai_title(title: str) -> dict:
    return {"id": "ai-title-1", "timestamp": 1000, "type": "ai-title", "title": title}


def _tool_noise() -> list[dict]:
    return [
        {"id": "r1", "type": "reasoning", "content": [{"type": "reasoning", "text": "think"}]},
        {"id": "c1", "type": "function_call", "name": "bash", "callId": "c1", "arguments": "{}"},
        {"id": "x1", "type": "function_call_result", "callId": "c1", "output": "done"},
        {"id": "s1", "type": "session-meta", "meta": {}},
        {"id": "h1", "type": "file-history-snapshot", "snapshot": []},
        {"id": "f1", "type": "resend-fork-notice", "message": "forked"},
    ]


def _session_a_lines() -> list[str]:
    records: list[dict] = [
        _msg("assistant", "Hello! How can I help?", 1000, cwd="/repo"),
        _user_with_context("What is the answer to 2+2?", 2000),
        _msg("assistant", "It is 4.", 3000, cwd="/repo"),
        *_tool_noise(),
        _ai_title("Arithmetic answer"),
    ]
    return [json.dumps(rec, ensure_ascii=False) for rec in records]


def _session_b_lines() -> list[str]:
    records: list[dict] = [
        _msg("user", "No context wrap, plain user", 11000, cwd="/other"),
        _msg("assistant", "Acknowledged.", 12000, cwd="/other"),
    ]
    return [json.dumps(rec, ensure_ascii=False) for rec in records]


@pytest.fixture
def wb_home(tmp_path: Path) -> Path:
    """A synthetic ~/.workbuddy with two projects, three transcripts."""
    root = tmp_path / ".workbuddy"
    proj_a = root / "projects" / "c-repo"
    proj_b = root / "projects" / "c-other"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    (proj_a / f"{SESSION_A}.jsonl").write_text(
        "\n".join(_session_a_lines()), encoding="utf-8"
    )
    # A second transcript in project A with no DB row (title must fall back).
    (proj_a / "noclaimee-0000-0000-0000-000000000010.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    _user_with_context("Fallback title question", 5000), ensure_ascii=False
                ),
                json.dumps(_msg("assistant", "ok", 6000, cwd="/repo"), ensure_ascii=False),
            ]
        ),
        encoding="utf-8",
    )
    (proj_b / f"{SESSION_B}.jsonl").write_text(
        "\n".join(_session_b_lines()), encoding="utf-8"
    )
    _write_db(root)
    return root


def _write_db(root: Path) -> None:
    con = sqlite3.connect(root / "workbuddy.db")
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, custom_title TEXT, "
        "deleted_at TEXT, model TEXT, expert_id TEXT, created_at TEXT, updated_at TEXT, "
        "last_activity_at TEXT, status TEXT)"
    )
    # Session A: custom title wins over the file ai-title.
    con.execute(
        "INSERT INTO sessions (id,title,custom_title,deleted_at,model,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (SESSION_A, "db title", "user renamed", None, "deepseek-v4", "2000", "3000"),
    )
    # session_b is soft-deleted: must not be listed / imported.
    con.execute(
        "INSERT INTO sessions (id,title,custom_title,deleted_at,model,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (SESSION_B, "deleted session", None, "2026-02-02", "m", "11000", "12000"),
    )
    con.commit()
    con.close()


def test_discover_requires_projects_dir(tmp_path: Path) -> None:
    root = tmp_path / ".workbuddy"
    root.mkdir()
    assert workbuddy.discover_workbuddy_root(root) is None
    (root / "projects").mkdir()
    assert workbuddy.discover_workbuddy_root(root) == root


def test_list_filters_deleted_and_groups(wb_home: Path) -> None:
    groups = workbuddy.list_workbuddy_sessions(wb_home)
    all_sessions = [s for g in groups for s in g["sessions"]]
    ids = {s["external_id"] for s in all_sessions}
    # Session B is soft-deleted and must be absent; the fallback-title session
    # is present (no DB row).
    assert SESSION_B not in ids
    assert SESSION_A in ids
    assert len(all_sessions) == 2

    # Grouped by cwd: repo project holds session A + fallback one.
    repo = next(g for g in groups if g["label"] == "repo")
    assert len(repo["sessions"]) == 2
    fallback = next(s for s in repo["sessions"] if s["external_id"].startswith("noclaimee"))
    assert fallback["title"] == "Fallback title question"


def test_list_uses_db_metadata(wb_home: Path) -> None:
    groups = workbuddy.list_workbuddy_sessions(wb_home)
    sess_a = next(s for g in groups for s in g["sessions"] if s["external_id"] == SESSION_A)
    assert sess_a["title"] == "user renamed"  # custom_title > ai-title > title
    assert sess_a["model"] == "deepseek-v4"
    assert sess_a["created_at"] == pytest.approx(2.0)


def test_parse_is_clean_human_conversation(wb_home: Path) -> None:
    parsed = workbuddy.parse_workbuddy_session(wb_home, SESSION_A)
    assert parsed is not None
    roles = [m["role"] for m in parsed["messages"]]
    assert roles == ["assistant", "user", "assistant"]
    # No tool/thinking/meta rows leak in.
    assert all(m["role"] in ("user", "assistant") for m in parsed["messages"])


def test_parse_strips_context_and_extracts_user_query(wb_home: Path) -> None:
    parsed = workbuddy.parse_workbuddy_session(wb_home, SESSION_A)
    assert parsed is not None
    user_msg = next(m for m in parsed["messages"] if m["role"] == "user")
    assert user_msg["content"] == "What is the answer to 2+2?"
    assert "<system-reminder" not in user_msg["content"]


def test_parse_title_precedence_and_timestamps(wb_home: Path) -> None:
    parsed = workbuddy.parse_workbuddy_session(wb_home, SESSION_A)
    assert parsed is not None
    assert parsed["title"] == "user renamed"
    assert parsed["created_at"] == pytest.approx(2.0)  # db created_at ms -> s
    assert parsed["updated_at"] == pytest.approx(3.0)  # db updated_at
    assert parsed["source_cwd"] == "/repo"


def test_parse_fallback_title_without_db_row(wb_home: Path) -> None:
    parsed = workbuddy.parse_workbuddy_session(
        wb_home, "noclaimee-0000-0000-0000-000000000010"
    )
    assert parsed is not None
    assert parsed["title"] == "Fallback title question"
    assert parsed["created_at"] == pytest.approx(5.0)
    assert parsed["updated_at"] == pytest.approx(6.0)


def test_parse_missing_session_returns_none(wb_home: Path) -> None:
    assert workbuddy.parse_workbuddy_session(wb_home, "does-not-exist") is None


def test_parse_soft_deleted_session_returns_none(wb_home: Path) -> None:
    # Session B is soft-deleted in workbuddy.db — parsing must be refused too.
    assert workbuddy.parse_workbuddy_session(wb_home, SESSION_B) is None


def test_parse_missing_root_returns_none(tmp_path: Path) -> None:
    assert workbuddy.parse_workbuddy_session(tmp_path, SESSION_A) is None


def test_parse_batch_skips_unreadable_and_dedupes(wb_home: Path) -> None:
    parsed = workbuddy.parse_workbuddy_sessions(
        wb_home, [SESSION_A, SESSION_A, "nope", "nope"]
    )
    assert [p["external_id"] for p in parsed] == [SESSION_A]


def test_images_flagged_not_materialized(wb_home: Path) -> None:
    (wb_home / "projects" / "c-repo" / f"{SESSION_A}.jsonl").write_text(
        json.dumps(
            {
                "id": "i1",
                "timestamp": 7000,
                "type": "message",
                "role": "user",
                "cwd": "/repo",
                "content": [
                    {"type": "image_blob_ref", "imageId": "abc123"},
                    {
                        "type": "input_text",
                        "text": "<user_query>看图说话</user_query>",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    parsed = workbuddy.parse_workbuddy_session(wb_home, SESSION_A)
    assert parsed is not None
    last = parsed["messages"][-1]
    assert last["content"].strip() == "看图说话"
    assert last["metadata"] == {"had_images": True}
