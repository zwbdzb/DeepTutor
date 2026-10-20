from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop.auth import AuthManager
from desktop.thinkbuddy_website import ThinkBuddyWebsiteError


@pytest.mark.parametrize("gift_error", [None, ThinkBuddyWebsiteError("unavailable", 502)])
def test_login_requests_gift_with_access_token_and_gift_failure_does_not_fail_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gift_error: Exception | None,
) -> None:
    monkeypatch.setenv("THINKBUDDY_WEBSITE_URL", "https://website.example.com")
    manager = AuthManager(root=tmp_path / "root", home=tmp_path / "home")
    manager._store = MagicMock()
    manager._store.load.return_value = {}
    manager._client = MagicMock()
    manager._client.exchange.return_value = {"access_token": "oauth-access", "expires_in": 60}
    manager._client.userinfo.return_value = {"sub": "u_314", "ai_token": "sk-ai-token"}
    manager._pending_verifier = "verifier"
    loopback = MagicMock()
    loopback.wait.return_value = {"ok": True, "code": "auth-code"}

    with (
        patch("desktop.auth.manager.request_first_use_gift", side_effect=gift_error,
              return_value={"status": "granted", "plan_id": 42}) as claim,
        patch("desktop.auth.manager.fetch_relay_models", return_value=([], {})),
        patch("desktop.auth.manager.ensure_tokengine_catalog"),
    ):
        manager._watchdog(loopback, "http://127.0.0.1/callback", "device")

    claim.assert_called_once_with("https://website.example.com", "oauth-access")
    assert manager._result["ok"] is True
    assert manager._store.save.call_args.args[0]["token"] == "sk-ai-token"


def test_restored_login_claims_gift_using_refreshed_access_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THINKBUDDY_WEBSITE_URL", "https://website.example.com")
    manager = AuthManager(root=tmp_path / "root", home=tmp_path / "home")
    manager._store = MagicMock()
    manager._store.load.return_value = {
        "token": "sk-ai-token", "access_token": "old-access", "refresh_token": "refresh-token",
    }
    manager._client = MagicMock()
    manager._client.refresh.return_value = {"access_token": "refreshed-access", "expires_in": 60}
    manager._client.userinfo.return_value = {"sub": "u_314", "ai_token": "sk-ai-token"}

    with (
        patch("desktop.auth.manager.request_first_use_gift", return_value={"status": "already_received"}) as claim,
        patch("desktop.auth.manager.fetch_relay_models", return_value=([], {})),
        patch("desktop.auth.manager.ensure_tokengine_catalog", return_value=tmp_path / "catalog"),
    ):
        result = manager.refresh_models()

    assert result["ok"] is True
    claim.assert_called_once_with("https://website.example.com", "refreshed-access")


def test_ai_token_only_does_not_request_gift(tmp_path: Path) -> None:
    manager = AuthManager(root=tmp_path / "root", home=tmp_path / "home")
    manager._store = MagicMock()
    manager._store.load.return_value = {"token": "sk-manual-token"}
    with (
        patch("desktop.auth.manager.request_first_use_gift") as claim,
        patch("desktop.auth.manager.fetch_relay_models", return_value=([], {})),
        patch("desktop.auth.manager.ensure_tokengine_catalog", return_value=tmp_path / "catalog"),
    ):
        assert manager.refresh_models()["ok"] is True
    claim.assert_not_called()
