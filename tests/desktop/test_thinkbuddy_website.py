from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop.thinkbuddy_website import ThinkBuddyWebsiteError, checkin


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_checkin_posts_only_provider_user_id() -> None:
    response = FakeResponse(
        b'{"reward":{"points_awarded":100},"already_checked_in":false}'
    )
    with patch(
        "desktop.thinkbuddy_website.urllib.request.urlopen",
        return_value=response,
    ) as urlopen:
        result = checkin(" https://website.example.com/ ", " u_314 ", timeout=3.5)

    request = urlopen.call_args.args[0]
    assert request.full_url == "https://website.example.com/api/v1/checkin"
    assert request.method == "POST"
    assert json.loads(request.data) == {"provider_user_id": "u_314"}
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Authorization") is None
    assert urlopen.call_args.kwargs == {"timeout": 3.5}
    assert result["reward"]["points_awarded"] == 100


@pytest.mark.parametrize(
    "provider_user_id",
    ["", "314", "u_0", "u_0314", "u_-1", "u_invalid"],
)
def test_checkin_rejects_invalid_provider_user_id(provider_user_id: str) -> None:
    with patch("desktop.thinkbuddy_website.urllib.request.urlopen") as urlopen:
        with pytest.raises(ThinkBuddyWebsiteError, match="invalid provider user id"):
            checkin("https://website.example.com", provider_user_id)

    urlopen.assert_not_called()


def test_checkin_preserves_http_status_and_service_error() -> None:
    error = urllib.error.HTTPError(
        "https://website.example.com/api/v1/checkin",
        409,
        "Conflict",
        {},
        io.BytesIO(b'{"error":"checkin_disabled"}'),
    )
    with patch(
        "desktop.thinkbuddy_website.urllib.request.urlopen",
        side_effect=error,
    ):
        with pytest.raises(ThinkBuddyWebsiteError) as caught:
            checkin("https://website.example.com", "u_314")

    assert caught.value.status_code == 409
    assert str(caught.value) == "checkin_disabled"
    assert error.fp.closed


def test_checkin_wraps_invalid_url_error() -> None:
    with patch(
        "desktop.thinkbuddy_website.urllib.request.urlopen",
        side_effect=ValueError("unknown url type"),
    ):
        with pytest.raises(ThinkBuddyWebsiteError, match="is unavailable") as caught:
            checkin("not-a-url", "u_314")

    assert caught.value.status_code == 0


@pytest.mark.parametrize("payload", [b"not-json", b"[]"])
def test_checkin_rejects_invalid_response(payload: bytes) -> None:
    with patch(
        "desktop.thinkbuddy_website.urllib.request.urlopen",
        return_value=FakeResponse(payload),
    ):
        with pytest.raises(ThinkBuddyWebsiteError) as caught:
            checkin("https://website.example.com", "u_314")

    assert caught.value.status_code == 502
