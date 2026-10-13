"""Remote previews reuse the original format without leaking connection credentials."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest

from deeptutor.api.routers.auth import _learning_surface_for_path
from deeptutor.services.rag.pipelines.weknora import client as remote

knowledge = importlib.import_module("deeptutor.api.routers.knowledge")


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        self.reads += 1
        yield b"%PDF-1.4\n"
        yield b"preview"

    async def aclose(self):
        self.closed = True


@pytest.fixture
def preview_app(monkeypatch):
    entry = {
        "type": "weknora",
        "server_url": "http://localhost:8080",
        "api_key": "secret",
        "knowledge_base_id": "kb-1",
    }
    manager = SimpleNamespace(config={"knowledge_bases": {"remote": entry}})
    manager._load_config = lambda: manager.config
    monkeypatch.setattr(knowledge, "_readable_kb_entry", lambda name: (manager, "remote", entry))
    streams, calls = [], []

    def handler(request):
        assert request.headers["X-API-Key"] == "secret"
        calls.append(request)
        path = request.url.path
        if path.endswith("/knowledge"):
            page = int(request.url.params["page"])
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "data": [
                        {
                            "id": f"doc-{page}",
                            "knowledge_base_id": "kb-1",
                            "file_name": "说明.pdf",
                            "file_type": "pdf",
                            "file_path": "/private/path",
                        }
                    ],
                },
            )
        if path.endswith("/preview"):
            if "large" in path:
                return httpx.Response(
                    200, headers={"Content-Length": str(remote.MAX_PREVIEW_BYTES + 1)}
                )
            if "denied" in path:
                return httpx.Response(403, text="internal secret")
            stream = TrackedStream()
            streams.append(stream)
            headers = {"Content-Type": "application/pdf", "Accept-Ranges": "bytes"}
            status = 200
            if request.headers.get("range"):
                status = 206
                headers["Content-Range"] = "bytes 0-15/16"
            return httpx.Response(status, headers=headers, stream=stream)
        if "/chunks/" in path:
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "data": [
                        {
                            "id": "chunk-2",
                            "knowledge_id": "doc-1",
                            "chunk_index": 1,
                            "content": "第二段",
                        },
                        {
                            "id": "chunk-1",
                            "knowledge_id": "doc-1",
                            "chunk_index": 0,
                            "content": "第一段",
                        },
                    ],
                },
            )
        document_id = path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": document_id,
                    "knowledge_base_id": "kb-other" if document_id == "foreign" else "kb-1",
                    "file_name": "说明.pdf",
                }
            },
        )

    real_client = remote.WeKnoraClient
    monkeypatch.setattr(
        remote,
        "WeKnoraClient",
        lambda config, **kwargs: real_client(
            config, transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    app = FastAPI()
    app.include_router(knowledge.router, prefix="/api")
    with TestClient(app) as client:
        yield client, streams, calls


def test_remote_listing_keeps_same_name_ids_and_all_pages(preview_app):
    client, _, _ = preview_app
    response = client.get("/api/knowledge-bases/remote/files")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["files"]] == ["doc-1", "doc-2"]
    assert response.json()["files"][0]["mime_type"] == "application/pdf"
    assert "private" not in response.text and "secret" not in response.text


def test_remote_card_reports_upstream_document_count(preview_app, monkeypatch):
    info = knowledge.KnowledgeBaseInfo(
        id="admin:kb:remote",
        name="remote",
        is_default=False,
        statistics={"raw_documents": 0},
        metadata={"type": "weknora"},
    )
    monkeypatch.setattr(knowledge, "_list_knowledge_bases", lambda: [info])
    resolve = Mock(wraps=knowledge._readable_kb_entry)
    monkeypatch.setattr(knowledge, "_readable_kb_entry", resolve)
    client, _, calls = preview_app
    response = client.get("/api/knowledge-bases")
    assert response.status_code == 200
    assert response.json()[0]["statistics"]["raw_documents"] == 2
    assert len(calls) == 1
    assert calls[0].url.params["page_size"] == "1"
    assert "secret" not in response.text
    resolve.assert_called_once_with("admin:kb:remote")


def test_remote_details_use_the_same_count_and_keep_assignment_scope(preview_app, monkeypatch):
    manager, _, _ = knowledge._readable_kb_entry("remote")
    manager.get_info = lambda name: {
        "name": name,
        "path": "/private/path",
        "metadata": {"type": "weknora"},
        "statistics": {"raw_documents": 0},
    }
    resource = SimpleNamespace(
        id="admin:kb:remote", name="remote", source="admin", assigned=True, read_only=True
    )
    monkeypatch.setattr(knowledge, "resolve_kb", lambda name: resource)
    monkeypatch.setattr(knowledge, "manager_for_resource", lambda resource: manager)
    client, _, _ = preview_app
    response = client.get("/api/knowledge-bases/admin:kb:remote")
    assert response.status_code == 200
    assert response.json()["statistics"]["raw_documents"] == 2
    assert response.json()["assigned"] is True
    assert "path" not in response.json()


def test_count_failure_stays_unknown_and_does_not_change_local_counts(preview_app, monkeypatch):
    async def failed_count():
        raise remote.WeKnoraAPIError("internal secret")

    monkeypatch.setattr(
        remote,
        "WeKnoraClient",
        lambda *args, **kwargs: SimpleNamespace(document_count=failed_count),
    )
    infos = [
        knowledge.KnowledgeBaseInfo(
            name="remote",
            is_default=False,
            metadata={"type": "weknora"},
            statistics={"raw_documents": 0},
        ),
        knowledge.KnowledgeBaseInfo(
            name="local",
            is_default=False,
            statistics={"raw_documents": 7},
        ),
    ]
    monkeypatch.setattr(knowledge, "_list_knowledge_bases", lambda: infos)
    client, _, _ = preview_app
    response = client.get("/api/knowledge-bases")
    assert response.status_code == 200
    assert "raw_documents" not in response.json()[0]["statistics"]
    assert response.json()[1]["statistics"]["raw_documents"] == 7
    assert "secret" not in response.text


def test_original_preview_streams_range_and_closes_connection(preview_app):
    client, streams, calls = preview_app
    response = client.get(
        "/api/knowledge-bases/remote/remote-file-preview/doc-1", headers={"Range": "bytes=0-15"}
    )
    assert response.status_code == 206
    assert response.content == b"%PDF-1.4\npreview"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-range"] == "bytes 0-15/16"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
    assert calls[-1].headers["Range"] == "bytes=0-15"
    assert streams[-1].closed


def test_head_preview_closes_without_reading_the_body(preview_app):
    client, streams, _ = preview_app
    response = client.head("/api/knowledge-bases/remote/remote-file-preview/doc-1")
    assert response.status_code == 200 and response.content == b""
    assert streams[-1].reads == 0 and streams[-1].closed


@pytest.mark.parametrize(
    "suffix", ["remote-file-preview/foreign", "remote-file-preview-text/foreign"]
)
def test_preview_never_reads_a_foreign_knowledge_base(preview_app, suffix):
    client, _, calls = preview_app
    assert client.get(f"/api/knowledge-bases/remote/{suffix}").status_code == 404
    assert not any(
        request.url.path.endswith("/preview") or "/chunks/" in request.url.path for request in calls
    )


@pytest.mark.parametrize(("identifier", "status"), [("large", 413), ("denied", 502)])
def test_remote_preview_errors_are_bounded_and_sanitized(preview_app, identifier, status):
    client, _, _ = preview_app
    response = client.get(f"/api/knowledge-bases/remote/remote-file-preview/{identifier}")
    assert response.status_code == status
    assert "internal secret" not in response.text


def test_parsed_text_fallback_is_ordered(preview_app):
    client, _, _ = preview_app
    response = client.get("/api/knowledge-bases/remote/remote-file-preview-text/doc-1")
    assert response.status_code == 200 and response.text == "第一段\n\n第二段"


def test_remote_office_conversion_reuses_the_existing_service(preview_app, monkeypatch):
    office = importlib.import_module("deeptutor.api.routers.file_preview")
    seen = []

    async def convert(data, filename, cache):
        from fastapi.responses import Response

        seen.append((data, filename))
        return Response(b"converted-pdf", media_type="application/pdf")

    monkeypatch.setattr(office, "_pdf_response", convert)
    monkeypatch.setattr(office, "_cache_dir", lambda: None)
    client, streams, _ = preview_app
    response = client.get("/api/knowledge-bases/remote/remote-file-preview/doc-1?format=pdf")
    assert response.content == b"converted-pdf"
    assert seen == [(b"%PDF-1.4\npreview", "说明.pdf")]
    assert streams[-1].closed


@pytest.mark.parametrize(
    "suffix", ["files", "remote-file-preview/doc-1", "remote-file-preview-text/doc-1"]
)
def test_remote_preview_routes_keep_local_assignment_checks(monkeypatch, suffix):
    monkeypatch.setattr(knowledge, "_overridden_kb_manager", lambda: None)
    resolve = Mock(side_effect=HTTPException(status_code=403, detail="Not assigned"))
    monkeypatch.setattr(knowledge, "resolve_kb", resolve)
    remote_client = Mock()
    monkeypatch.setattr(remote, "WeKnoraClient", remote_client)
    app = FastAPI()
    app.include_router(knowledge.router, prefix="/api")
    with TestClient(app) as client:
        response = client.get(f"/api/knowledge-bases/admin:kb:remote/{suffix}")
    assert response.status_code == 403
    resolve.assert_called_once_with("admin:kb:remote")
    remote_client.assert_not_called()


@pytest.mark.parametrize(
    "route", ["remote-file-preview/{document_id}", "remote-file-preview-text/{document_id}"]
)
def test_remote_preview_is_a_learner_reading_surface(route):
    template = "/api/knowledge-bases/{kb_name}/" + route
    assert (
        _learning_surface_for_path(
            "/api/knowledge-bases/remote/remote-file-preview/doc-1", "GET", route_path=template
        )
        == "reading"
    )
    if route.startswith("remote-file-preview/"):
        assert (
            _learning_surface_for_path(
                "/api/knowledge-bases/remote/remote-file-preview/doc-1", "HEAD", route_path=template
            )
            == "reading"
        )
