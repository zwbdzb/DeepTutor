"""Fetch WeKnora connection settings published by GeekAI."""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

import httpx

PREFIX = "platform:kb:"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}")


class LibraryError(RuntimeError):
    def __init__(
        self, message="Platform materials are temporarily unavailable.", *, status_code=502
    ):
        super().__init__(message)
        self.status_code = status_code


def library_url() -> str:
    explicit = os.environ.get("THINKBUDDY_LIBRARY_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    site = os.environ.get("THINKBUDDY_API_BASE", "").strip().rstrip("/")
    return f"{site}/api/v1/library" if site else ""


def remote_id(ref: str) -> str:
    value = ref[len(PREFIX) :] if ref.startswith(PREFIX) else ref
    if not _ID.fullmatch(value):
        raise LibraryError("Platform material not found.", status_code=404)
    return value


def _decode(status: int, body: bytes) -> dict:
    if status < 200 or status >= 300:
        raise LibraryError(
            "Platform material not found."
            if status == 404
            else "Could not read platform materials.",
            status_code=status if status in {404, 413} else 502,
        )
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise LibraryError() from exc
    if not isinstance(value, dict):
        raise LibraryError()
    return value


def _check_size(size: int, limit=MAX_RESPONSE_BYTES) -> None:
    if size > limit:
        raise LibraryError("Platform material exceeds the transfer limit.", status_code=413)


def _declared_size(response: httpx.Response, limit: int) -> None:
    try:
        size = int(response.headers.get("content-length", "0"))
    except ValueError:
        return
    _check_size(size, limit)


def _collection(value: object) -> dict | None:
    if not isinstance(value, dict):
        raise LibraryError()
    if value.get("provider") != "weknora":
        return None
    if not isinstance(value.get("id"), str) or not isinstance(value.get("name"), str):
        raise LibraryError()
    remote_id(value["id"])
    from deeptutor.services.rag.pipelines.weknora.config import normalize_base_url

    connection = value.get("connection")
    if not isinstance(connection, dict) or not all(
        isinstance(connection.get(key), str) and connection[key].strip()
        for key in ("server_url", "api_key", "knowledge_base_id")
    ):
        raise LibraryError("Platform WeKnora connection is not configured.")
    server_url = normalize_base_url(connection["server_url"])
    api_key = connection["api_key"].strip()
    if not server_url or len(api_key) > 8192 or any(char in api_key for char in "\r\n"):
        raise LibraryError("Platform WeKnora connection is invalid.")
    return {
        "id": value["id"],
        "name": value["name"],
        "description": str(value.get("description") or ""),
        "connection": {
            "server_url": server_url,
            "api_key": api_key,
            "knowledge_base_id": remote_id(connection["knowledge_base_id"]),
        },
    }


class LibraryClient:
    def __init__(self, url: str | None = None, *, timeout=30.0, transport=None):
        self.url = (library_url() if url is None else url).rstrip("/")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise LibraryError("Platform library URL is not configured.", status_code=503)
        self.timeout = timeout
        self.transport = transport

    def _options(self):
        return {
            "timeout": self.timeout,
            "transport": self.transport,
            "follow_redirects": False,
            "trust_env": False,
            "headers": {"Accept": "application/json", "Accept-Encoding": "identity"},
        }

    def catalog(self) -> list[dict]:
        """Synchronous discovery for existing thread-based resource resolution."""
        try:
            with httpx.Client(**self._options()) as client:
                with client.stream("GET", self.url) as response:
                    _declared_size(response, MAX_RESPONSE_BYTES)
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        _check_size(len(body))
            data = _decode(response.status_code, bytes(body)).get("data")
        except httpx.HTTPError as exc:
            raise LibraryError() from exc
        if not isinstance(data, list):
            raise LibraryError()
        items = [row for item in data if (row := _collection(item)) is not None]
        if len({item["id"] for item in items}) != len(items):
            raise LibraryError()
        return items
