import asyncio
import importlib
import json

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import httpx
import pytest

from deeptutor.core.errors import AccessError
from deeptutor.multi_user.knowledge_access import (
    current_kb_manager,
    list_visible_knowledge_bases,
    resolve_kb,
)
from deeptutor.services.platform_library import catalog
from deeptutor.services.platform_library.client import LibraryClient
from deeptutor.services.workspace import ContentWorkspaceService
from deeptutor.services.workspace.context import workspace_context
from deeptutor.services.workspace.knowledge import knowledge_catalog
from deeptutor.tools.rag_tool import rag_search


def expire():
    catalog._catalog("http://localhost:5678/api/v1/library").expires = 0


@pytest.fixture
def direct_platform(monkeypatch):
    from deeptutor.services.rag.pipelines.weknora.client import WeKnoraClient

    platform = {
        "published": True,
        "online": True,
        "name": "Free study material",
        "calls": [],
        "direct_calls": [],
        "accepted_key": "client-read-key",
        "connection": {
            "server_url": "http://localhost:8080",
            "api_key": "client-read-key",
            "knowledge_base_id": "kb-1",
        },
    }
    monkeypatch.setenv("THINKBUDDY_LIBRARY_URL", "http://localhost:5678/api/v1/library")
    catalog._catalog.cache_clear()
    original = LibraryClient._options

    def discovery(request):
        platform["calls"].append(request)
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        assert request.url.path == "/api/v1/library"
        if not platform["online"]:
            raise httpx.ConnectError("offline", request=request)
        row = {
            "id": "42",
            "name": platform["name"],
            "provider": "weknora",
            "connection": platform["connection"],
        }
        return httpx.Response(200, json={"data": [row] if platform["published"] else []})

    monkeypatch.setattr(
        LibraryClient,
        "_options",
        lambda self: {**original(self), "transport": httpx.MockTransport(discovery)},
    )

    def handler(request):
        platform["direct_calls"].append(request)
        if request.headers.get("x-api-key") != platform["accepted_key"]:
            return httpx.Response(401, json={"error": "revoked key"})
        path = request.url.path
        if path == "/api/v1/knowledge-bases/kb-1/knowledge":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "data": [
                        {
                            "id": "doc-1",
                            "file_name": "guide.pdf",
                            "file_size": 4,
                            "knowledge_base_id": "kb-1",
                        }
                    ],
                },
            )
        if path == "/api/v1/knowledge/doc-1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "doc-1",
                        "file_name": "guide.pdf",
                        "knowledge_base_id": "kb-1",
                        "type": "manual",
                        "metadata": {"content": "Direct learning guide"},
                    }
                },
            )
        if path == "/api/v1/knowledge/doc-1/preview":
            ranged = "range" in request.headers
            return httpx.Response(
                206 if ranged else 200,
                content=b"%PDF",
                headers={
                    "Content-Type": "application/pdf",
                    **({"Content-Range": "bytes 0-3/4"} if ranged else {}),
                },
            )
        assert path == "/api/v1/knowledge-search"
        assert json.loads(request.content) == {"query": "study", "knowledge_base_id": "kb-1"}
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "chunk-1",
                        "content": "Direct answer",
                        "knowledge_id": "doc-1",
                        "knowledge_base_id": "kb-1",
                        "knowledge_title": "Guide",
                    }
                ]
            },
        )

    monkeypatch.setattr(
        WeKnoraClient,
        "_open",
        lambda self: httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={"X-API-Key": self._config.api_key},
            transport=httpx.MockTransport(handler),
        ),
    )
    yield platform
    catalog._catalog.cache_clear()


def knowledge_app():
    knowledge = importlib.import_module("deeptutor.api.routers.knowledge")
    app = FastAPI()
    app.add_exception_handler(
        AccessError,
        lambda request, exc: JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail}
        ),
    )
    app.include_router(knowledge.router, prefix="/api")
    return app


@pytest.mark.parametrize("role", ["user", "admin"])
def test_first_list_registers_normal_weknora_connection(as_user, direct_platform, role):
    with as_user("learner", role=role), TestClient(knowledge_app()) as client:
        response = client.get("/api/knowledge-bases")
        assert response.status_code == 200
        row = response.json()[0]
        assert row["metadata"]["type"] == row["statistics"]["rag_provider"] == "weknora"
        assert row["source"] == ("admin" if role == "admin" else "user")
        assert not row["read_only"] and "client-read-key" not in response.text
        assert current_kb_manager().get_kb_entry(row["name"])["api_key"] == "client-read-key"
        assert client.get("/api/knowledge-bases").json() == response.json()
        assert len(direct_platform["calls"]) == 1


@pytest.mark.parametrize("automatic", [False, True])
def test_manual_and_automatic_connections_use_existing_routes_and_engine(
    as_user, direct_platform, automatic
):
    with as_user("learner"), TestClient(knowledge_app()) as client:
        manager = current_kb_manager()
        if not automatic:
            manager.register_weknora_kb("Manual", **direct_platform["connection"])
        row = client.get("/api/knowledge-bases").json()[0]
        ref = "account:kb:" + row["name"]
        assert len(manager.list_knowledge_bases()) == 1
        entry = manager.get_kb_entry(row["name"])
        assert all(entry[key] == value for key, value in direct_platform["connection"].items())
        base = "/api/knowledge-bases/" + ref
        assert "client-read-key" not in client.get(base).text
        assert client.put("/api/knowledge-bases/default/" + ref).status_code == 200
        assert client.get(base + "/files").json()["files"][0]["name"] == "guide.pdf"
        response = client.get(base + "/remote-file-preview/doc-1", headers={"Range": "bytes=0-3"})
        assert response.status_code == 206 and response.content == b"%PDF"
        assert client.get(base + "/remote-file-preview-text/doc-1").text == "Direct learning guide"
        workspace = ContentWorkspaceService().create_workspace(
            "Study", resources={"knowledge_bases": [ref]}
        )
        with workspace_context(workspace["workspace_id"]):
            assert not resolve_kb(ref, require_write=True).read_only
            result = asyncio.run(rag_search("study", ref, rewrite=False))
            assert result["provider"] == "weknora" and result["content"] == "Direct answer"
            assert result["sources"][0]["knowledge_id"] == "doc-1"
        assert client.delete(base).status_code == 200
        assert client.get("/api/knowledge-bases").json() == []
        assert all(request.url.path == "/api/v1/library" for request in direct_platform["calls"])


def test_connection_updates_and_survives_discovery_outage_or_unpublishing(as_user, direct_platform):
    with as_user("learner"):
        ref = knowledge_catalog()[0]["id"]
        direct_platform["connection"]["api_key"] = direct_platform["accepted_key"] = "rotated-key"
        expire()
        assert knowledge_catalog()[0]["id"] == ref
        assert current_kb_manager().get_kb_entry(resolve_kb(ref).name)["api_key"] == "rotated-key"
        direct_platform["online"] = False
        catalog._catalog.cache_clear()
        assert knowledge_catalog()[0]["id"] == ref
        assert asyncio.run(rag_search("study", ref, rewrite=False))["content"] == "Direct answer"
        direct_platform["online"], direct_platform["published"] = True, False
        expire()
        assert knowledge_catalog()[0]["id"] == ref
        assert asyncio.run(rag_search("study", ref, rewrite=False))["content"] == "Direct answer"


def test_existing_manual_connection_is_not_overwritten(as_user, direct_platform):
    with as_user("learner"):
        manager = current_kb_manager()
        manager.register_weknora_kb("My name", **direct_platform["connection"])
        before = manager.config_file.read_bytes()
        assert [row["name"] for row in knowledge_catalog()] == ["My name"]
        direct_platform["connection"]["api_key"] = "updated-gift-key"
        expire()
        knowledge_catalog()
        assert manager.config_file.read_bytes() == before


@pytest.mark.parametrize("name", ["Free study material", "invalid/name"])
def test_name_collisions_do_not_replace_existing_knowledge(as_user, direct_platform, name):
    with as_user("learner"):
        manager = current_kb_manager()
        manager.register_connected_entry("Free study material", {"type": "obsidian"})
        direct_platform["name"] = name
        rows = knowledge_catalog()
        imported = next(row for row in rows if row["name"] != "Free study material")
        assert imported["name"] == ("WeKnora 42" if "/" in name else "Free study material (2)")
        before = manager.config_file.read_bytes()
        knowledge_catalog()
        assert manager.config_file.read_bytes() == before
        assert manager.get_kb_entry("Free study material")["type"] == "obsidian"


def test_moved_connection_is_updated_without_importing_again(as_user, direct_platform):
    from deeptutor.multi_user.knowledge_access import account_kb_manager
    from deeptutor.services.workspace.kb_move import move_kb

    with as_user("learner"):
        ref = knowledge_catalog()[0]["id"]
        workspace = ContentWorkspaceService().create_workspace(
            "Study", resources={"knowledge_bases": [ref]}
        )
        move_kb(ref, workspace["workspace_id"])
        direct_platform["connection"]["api_key"] = direct_platform["accepted_key"] = "rotated-key"
        expire()
        rows = knowledge_catalog()
        assert len(rows) == 1 and rows[0]["id"].startswith("workspace:")
        assert account_kb_manager().list_knowledge_bases() == []
        with workspace_context(workspace["workspace_id"]):
            assert list_visible_knowledge_bases()[0]["id"] == rows[0]["id"]
            assert current_kb_manager().get_kb_entry(rows[0]["name"])["api_key"] == "rotated-key"
            assert (
                asyncio.run(rag_search("study", rows[0]["id"], rewrite=False))["content"]
                == "Direct answer"
            )


def test_workspace_selection_uses_normal_access_rules(as_user, direct_platform):
    with as_user("learner"):
        ref = knowledge_catalog()[0]["id"]
        workspace = ContentWorkspaceService().create_workspace(
            "Empty", resources={"knowledge_bases": []}
        )
        with workspace_context(workspace["workspace_id"]):
            with pytest.raises(AccessError) as error:
                asyncio.run(rag_search("study", ref, rewrite=False))
            assert error.value.status_code == 403
        assert not direct_platform["direct_calls"]
