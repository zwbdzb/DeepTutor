"""Thin HTTP client for Tencent WeKnora's documented REST API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .config import WeKnoraConfig

logger = logging.getLogger(__name__)

# An external service is not allowed to make the DeepTutor process buffer an
# unbounded body. Four MiB is ample for a page of KB summaries or retrieval
# chunks while keeping a compromised/misconfigured server cheap to reject.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_BYTES = 64 * 1024 * 1024


class WeKnoraAPIError(RuntimeError):
    """Raised when WeKnora returns an error or unexpected payload."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class WeKnoraPreview:
    response: httpx.Response
    filename: str


class WeKnoraClient:
    def __init__(
        self,
        config: WeKnoraConfig,
        *,
        timeout: float = 60.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._config = config
        self._timeout = timeout
        self._transport = transport

    def _open(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={
                "Accept": "application/json",
                "X-API-Key": self._config.api_key,
            },
            timeout=self._timeout,
            transport=self._transport,
        )

    async def _validate_server_url(self) -> None:
        # Re-resolve on every request. Checking only when the connection is
        # saved would leave retrieval vulnerable to DNS rebinding later.
        from deeptutor.services.mcp.network import validate_mcp_url_async

        ok, error = await validate_mcp_url_async(self._config.base_url)
        if not ok:
            raise WeKnoraAPIError(f"Unsafe WeKnora server URL: {error}")

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        await self._validate_server_url()
        async with self._open() as client:
            async with client.stream(method, path, **kwargs) as resp:
                declared = resp.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > MAX_RESPONSE_BYTES:
                            raise WeKnoraAPIError("WeKnora response exceeds the 4 MiB limit.")
                    except ValueError:
                        pass
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise WeKnoraAPIError("WeKnora response exceeds the 4 MiB limit.")

        if resp.status_code >= 400:
            preview = bytes(body[:300]).decode("utf-8", errors="replace")
            raise WeKnoraAPIError(
                f"WeKnora returned {resp.status_code}: {preview}",
                status_code=resp.status_code,
            )
        try:
            data = json.loads(body)
        except Exception as exc:
            raise WeKnoraAPIError(f"WeKnora returned a non-JSON response: {exc}") from exc
        if not isinstance(data, dict):
            raise WeKnoraAPIError(f"WeKnora returned unexpected payload: {data!r}")
        if data.get("success") is False:
            raise WeKnoraAPIError("WeKnora reported an unsuccessful request.")
        return data

    @staticmethod
    def _document_id(document_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", document_id):
            raise WeKnoraAPIError("Document not found.", status_code=404)
        return quote(document_id, safe="")

    async def _list_pages(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        size = 0
        for page in range(1, 101):
            payload = await self._request_json("GET", path, params={"page": page, "page_size": 100})
            batch, total = payload.get("data"), payload.get("total")
            if batch is None and total == 0:
                batch = []
            if not isinstance(batch, list) or type(total) is not int or total < 0:
                raise WeKnoraAPIError("WeKnora returned invalid document pagination.")
            size += len(json.dumps(batch, ensure_ascii=False).encode("utf-8"))
            if size > MAX_RESPONSE_BYTES:
                raise WeKnoraAPIError("WeKnora document collection is too large.", status_code=413)
            for item in batch:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not item["id"]
                    or item["id"] in seen
                ):
                    raise WeKnoraAPIError(
                        "WeKnora returned missing or repeated document identifiers."
                    )
                seen.add(item["id"])
            items.extend(batch)
            if len(items) >= total:
                return items
            if not batch:
                raise WeKnoraAPIError("WeKnora returned incomplete document pagination.")
        raise WeKnoraAPIError(
            "WeKnora document collection exceeds the preview page limit.", status_code=413
        )

    async def list_documents(self) -> list[dict[str, Any]]:
        """List documents in the connected KB, including all folders."""
        kb_id = quote(self._config.knowledge_base_id, safe="")
        documents = await self._list_pages(f"/api/v1/knowledge-bases/{kb_id}/knowledge")
        if any(
            document.get("knowledge_base_id") != self._config.knowledge_base_id
            for document in documents
        ):
            raise WeKnoraAPIError(
                "WeKnora returned documents outside the connected knowledge base."
            )
        return documents

    async def document_count(self) -> int:
        """Read the remote total without downloading every document page."""
        kb_id = quote(self._config.knowledge_base_id, safe="")
        payload = await self._request_json(
            "GET", f"/api/v1/knowledge-bases/{kb_id}/knowledge", params={"page": 1, "page_size": 1}
        )
        total = payload.get("total")
        if type(total) is not int or total < 0:
            raise WeKnoraAPIError("WeKnora returned an invalid document count.")
        return total

    async def _document(self, document_id: str) -> dict[str, Any]:
        encoded_id = self._document_id(document_id)
        document = (await self._request_json("GET", f"/api/v1/knowledge/{encoded_id}")).get("data")
        if not isinstance(document, dict):
            raise WeKnoraAPIError("WeKnora returned invalid document metadata.")
        if (
            document.get("id") != document_id
            or document.get("knowledge_base_id") != self._config.knowledge_base_id
        ):
            raise WeKnoraAPIError("Document not found.", status_code=404)
        return document

    @asynccontextmanager
    async def preview_file(
        self,
        document_id: str,
        *,
        byte_range: str | None = None,
        max_bytes: int = MAX_PREVIEW_BYTES,
    ) -> AsyncIterator[WeKnoraPreview]:
        """Keep a scoped original file stream open until the consumer finishes."""
        document = await self._document(document_id)
        encoded_id = self._document_id(document_id)

        await self._validate_server_url()
        headers = {"Accept": "*/*", "Accept-Encoding": "identity"}
        if byte_range:
            headers["Range"] = byte_range
        async with self._open() as client:
            async with client.stream(
                "GET", f"/api/v1/knowledge/{encoded_id}/preview", headers=headers
            ) as resp:
                if resp.status_code not in {200, 206}:
                    raise WeKnoraAPIError(
                        "WeKnora could not preview this document.", status_code=resp.status_code
                    )
                declared = resp.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > max_bytes:
                            raise WeKnoraAPIError(
                                "WeKnora file is too large to preview.", status_code=413
                            )
                    except ValueError:
                        pass
                filename = str(document.get("file_name") or document.get("title") or document_id)
                yield WeKnoraPreview(resp, filename)

    async def document_text(self, document_id: str) -> str:
        """Return parsed text as a fallback for formats browsers cannot render."""
        encoded_id = self._document_id(document_id)
        document = await self._document(document_id)
        metadata = document.get("metadata")
        if (
            document.get("type") == "manual"
            and isinstance(metadata, dict)
            and isinstance(metadata.get("content"), str)
        ):
            return metadata["content"]

        chunks = await self._list_pages(f"/api/v1/chunks/{encoded_id}")
        if any(
            chunk.get("knowledge_id") != document_id
            or chunk.get("knowledge_base_id", self._config.knowledge_base_id)
            != self._config.knowledge_base_id
            or type(chunk.get("chunk_index")) is not int
            or not isinstance(chunk.get("content"), str)
            for chunk in chunks
        ):
            raise WeKnoraAPIError("WeKnora returned invalid document chunks.")
        return "\n\n".join(
            chunk["content"] for chunk in sorted(chunks, key=lambda item: item["chunk_index"])
        )

    async def list_knowledge_bases(self) -> list[dict[str, Any]]:
        data = (await self._request_json("GET", "/api/v1/knowledge-bases")).get("data")
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise WeKnoraAPIError("WeKnora returned an unexpected knowledge-base list.")
        return data

    async def search(self, query: str) -> list[dict[str, Any]]:
        data = (
            await self._request_json(
                "POST",
                "/api/v1/knowledge-search",
                params={"resource_urls": "handle"},
                json={
                    "query": query,
                    "knowledge_base_id": self._config.knowledge_base_id,
                },
            )
        ).get("data")
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise WeKnoraAPIError("WeKnora returned unexpected search results.")
        return data


__all__ = [
    "MAX_PREVIEW_BYTES",
    "MAX_RESPONSE_BYTES",
    "WeKnoraAPIError",
    "WeKnoraClient",
    "WeKnoraPreview",
]
