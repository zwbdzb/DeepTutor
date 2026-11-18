import httpx
import pytest

from deeptutor.services.platform_library.client import (
    MAX_RESPONSE_BYTES,
    LibraryClient,
    LibraryError,
)

URL = "http://localhost:5678/api/v1/library"


@pytest.mark.parametrize("status", [302, 404, 503])
def test_error_bodies_and_redirects_are_not_exposed_or_followed(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="secret upstream details", headers={"Location": "http://other/"}
        )

    client = LibraryClient(URL, transport=httpx.MockTransport(handler))
    with pytest.raises(LibraryError) as error:
        client.catalog()
    assert "secret" not in str(error.value)
    assert len(calls) == 1
    assert error.value.status_code == (404 if status == 404 else 502)


def test_discovery_size_and_url_limits():
    oversized = LibraryClient(
        URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}
            )
        ),
    )
    with pytest.raises(LibraryError) as error:
        oversized.catalog()
    assert error.value.status_code == 413
    for invalid in ("ftp://host", "http://user:key@host", "http://host?key=secret"):
        with pytest.raises(LibraryError):
            LibraryClient(invalid)


def test_discovery_accepts_only_complete_weknora_connections():
    row = {
        "id": "42",
        "name": "Study",
        "provider": "weknora",
        "connection": {
            "server_url": "http://localhost:8080/",
            "api_key": "client-read-key",
            "knowledge_base_id": "kb-1",
            "source_api_key": "must-not-copy",
        },
    }

    def read(value):
        return LibraryClient(
            URL,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [value]})
            ),
        ).catalog()[0]

    assert read(row)["connection"] == {
        "server_url": "http://localhost:8080",
        "api_key": "client-read-key",
        "knowledge_base_id": "kb-1",
    }
    for connection in (
        None,
        {},
        {**row["connection"], "api_key": ""},
        {**row["connection"], "server_url": "http://user:key@host"},
        {**row["connection"], "knowledge_base_id": "../other"},
        {**row["connection"], "api_key": "key\r\nInjected: header"},
    ):
        with pytest.raises(LibraryError):
            read({**row, "connection": connection})


def test_legacy_http_rows_are_ignored():
    client = LibraryClient(
        URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"id": "1", "provider": "http"}]})
        ),
    )
    assert client.catalog() == []
