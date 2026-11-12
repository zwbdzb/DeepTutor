"""Run from GeekAI's TestPlatformLibraryIntegration with temporary HTTP servers."""

import asyncio
import importlib
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import httpx
import pytest

from deeptutor.core.errors import AccessError
from deeptutor.multi_user.knowledge_access import current_kb_manager, resolve_kb
from deeptutor.services.platform_library import catalog
from deeptutor.services.workspace import ContentWorkspaceService
from deeptutor.services.workspace.context import workspace_context
from deeptutor.tools.rag_tool import rag_search

pytestmark = pytest.mark.skipif(
    not os.environ.get("THINKBUDDY_TEST_GEEKAI_URL"),
    reason="launched by GeekAI integration harness",
)


def test_publish_discover_read_search_and_unpublish(as_user, monkeypatch):
    provider = "weknora"
    geekai = os.environ["THINKBUDDY_TEST_GEEKAI_URL"]
    monkeypatch.setenv("THINKBUDDY_LIBRARY_URL", geekai + "/api/v1/library")
    catalog._catalog.cache_clear()
    knowledge = importlib.import_module("deeptutor.api.routers.knowledge")
    app = FastAPI()
    app.add_exception_handler(
        AccessError,
        lambda request, exc: JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail}
        ),
    )
    app.include_router(knowledge.router, prefix="/api")

    with httpx.Client(
        base_url=geekai, headers={"Admin-Authorization": "integration-admin"}
    ) as admin:

        def save(kind, value):
            response = admin.post("/api/admin/library/" + kind, json=value)
            assert response.status_code == 200, response.text
            return response.json()["data"]

        source = save(
            "sources",
            {
                "name": provider,
                "provider": provider,
                "base_url": os.environ["THINKBUDDY_TEST_UPSTREAM_URL"],
                "api_key": "upstream-read-key",
                "enabled": True,
            },
        )
        assert source["has_api_key"] and "api_key" not in source
        tested = admin.get(f"/api/admin/library/sources/{source['id']}/collections")
        selected = tested.json()["data"][0]
        assert selected["id"] == "remote-alpha"
        resource = save(
            "resources",
            {
                "source_id": source["id"],
                "remote_id": selected["id"],
                "name": "Gift " + provider,
                "published": True,
            },
        )
        ref = "account:kb:Gift weknora"
        assert "upstream-read-key" not in str(resource)
        published = admin.get("/api/v1/library").json()["data"]
        connection = next(item for item in published if item["id"] == str(resource["id"]))[
            "connection"
        ]
        assert connection == {
            "server_url": os.environ["THINKBUDDY_TEST_UPSTREAM_URL"],
            "api_key": "upstream-read-key",
            "knowledge_base_id": selected["id"],
        }
        with as_user("learner"), TestClient(app) as client:
            row = next(
                item
                for item in client.get("/api/knowledge-bases").json()
                if item["name"] == "Gift " + provider
            )
            assert row["name"] == "Gift " + provider
            assert not row["read_only"]
            assert row["id"] == "user:kb:Gift weknora" and row["source"] == "user"
            assert "upstream-read-key" not in str(row)
            base = "/api/knowledge-bases/" + ref
            assert client.get(base + "/files").json()["files"][0]["name"] == "guide.pdf"
            response = client.get(
                base + "/remote-file-preview/doc1", headers={"Range": "bytes=0-3"}
            )
            assert response.status_code == 206 and response.content == b"%PDF"
            assert (
                client.get(base + "/remote-file-preview-text/doc1").text == "Shared learning guide"
            )
            assert client.get(base + "/remote-file-preview/foreign").status_code == 404
            workspace = ContentWorkspaceService().create_workspace(
                "Reading", resources={"knowledge_bases": [ref]}
            )
            with workspace_context(workspace["workspace_id"]):
                assert not resolve_kb(ref).read_only
                result = asyncio.run(rag_search("question", ref, rewrite=False))
                assert result["content"] == "Shared answer" and result["provider"] == "weknora"
                assert result["sources"][0]["knowledge_id"] == "doc1"
            save("resources", {**resource, "published": False})
            catalog._catalog.cache_clear()
            assert client.get(base + "/remote-file-preview/doc1").status_code == 200
            assert resolve_kb(ref).id == ref
            assert current_kb_manager().list_knowledge_bases() == ["Gift weknora"]
        save("resources", {**resource, "published": True})
        source = save("sources", {**source, "api_key": "", "enabled": False})
        assert source["has_api_key"]
        assert admin.get(f"/api/v1/library/{resource['id']}").status_code == 404
        assert admin.get(f"/api/v1/library/{resource['id']}/documents").status_code == 404
    catalog._catalog.cache_clear()
