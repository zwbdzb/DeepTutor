from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop.auth import AuthManager
from desktop.thinkbuddy_website import ThinkBuddyWebsiteError


@contextmanager
def blocked_gift_request(gift_error: Exception | None = None):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    threads = []

    def request(*_args):
        threads.append(threading.current_thread())
        started.set()
        try:
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release gift request")
            if gift_error is not None:
                raise gift_error
            return {"status": "granted", "plan_id": 42}
        finally:
            finished.set()

    with patch("desktop.auth.manager.request_first_use_gift", side_effect=request) as claim:
        try:
            yield claim, started, finished
        finally:
            release.set()
            for thread in threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=2)
                    assert not thread.is_alive()


@pytest.mark.parametrize("gift_error", [None, ThinkBuddyWebsiteError("unavailable", 502)])
def test_login_finishes_while_gift_is_pending_and_gift_failure_does_not_fail_login(
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
        blocked_gift_request(gift_error) as (claim, started, finished),
        patch("desktop.auth.manager.fetch_relay_models", return_value=([], {})),
        patch("desktop.auth.manager.ensure_tokengine_catalog"),
    ):
        manager._watchdog(loopback, "http://127.0.0.1/callback", "device")

        assert started.wait(timeout=2)
        assert not finished.is_set()
        assert manager._done.is_set()
        assert manager._result["ok"] is True
        assert manager._store.save.call_args.args[0]["token"] == "sk-ai-token"

    claim.assert_called_once_with("https://website.example.com", "oauth-access")
    assert finished.is_set()


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
        blocked_gift_request() as (claim, started, finished),
        patch("desktop.auth.manager.fetch_relay_models", return_value=([], {})),
        patch("desktop.auth.manager.ensure_tokengine_catalog", return_value=tmp_path / "catalog"),
    ):
        result = manager.refresh_models()

        assert started.wait(timeout=2)
        assert not finished.is_set()
        assert result["ok"] is True
        assert manager._lock.acquire(blocking=False)
        manager._lock.release()

    claim.assert_called_once_with("https://website.example.com", "refreshed-access")
    assert finished.is_set()


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


def test_gift_thread_start_failure_is_ignored(tmp_path: Path) -> None:
    manager = AuthManager(root=tmp_path / "root", home=tmp_path / "home")
    with patch("desktop.auth.manager.threading.Thread", side_effect=RuntimeError("unavailable")):
        manager._claim_first_use_gift("oauth-access")
