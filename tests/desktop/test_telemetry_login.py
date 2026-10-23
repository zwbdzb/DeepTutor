from __future__ import annotations

from pathlib import Path
import sys
import time
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop-shell"))

from desktop.auth import AuthManager
from desktop.auth.client import OAuthError


def login_manager(tmp_path, callback):
    manager = AuthManager(root=tmp_path / "root", home=tmp_path / "home", on_login=callback)
    manager._pending_verifier = "pkce-verifier"
    manager._store = Mock()
    payload = {}
    manager._store.save.side_effect = lambda data: payload.update(data)
    manager._store.load.side_effect = lambda: dict(payload)
    manager._client = Mock()
    manager._client.exchange.return_value = {"access_token": "oauth-only", "expires_in": 3600}
    manager._client.userinfo.return_value = {"sub": "u_148", "ai_token": "sk-business"}
    loopback = Mock()
    loopback.wait.return_value = {"ok": True, "code": "auth-code"}
    return manager, loopback


@pytest.mark.parametrize("catalog_error", [False, True])
def test_successful_login_reports_even_if_catalog_write_fails(tmp_path, catalog_error):
    on_login = Mock()
    manager, loopback = login_manager(tmp_path, on_login)
    with patch("desktop.auth.manager.fetch_relay_models", return_value=(["model"], {})), patch(
        "desktop.auth.manager.ensure_tokengine_catalog",
        side_effect=OSError("read-only") if catalog_error else None,
    ):
        manager._watchdog(loopback, "http://localhost/callback", "machine")
    assert manager._result["ok"] is True
    on_login.assert_called_once_with("oauth-only")
    assert manager.telemetry_access_token() == "oauth-only"


def test_failed_login_does_not_report(tmp_path):
    on_login = Mock()
    manager, loopback = login_manager(tmp_path, on_login)
    manager._client.exchange.side_effect = OAuthError("exchange failed")
    manager._watchdog(loopback, "http://localhost/callback", "machine")
    assert manager._result["ok"] is False
    on_login.assert_not_called()


def test_reporting_failure_does_not_fail_login(tmp_path):
    on_login = Mock(side_effect=RuntimeError("reporting failed"))
    manager, loopback = login_manager(tmp_path, on_login)
    with patch("desktop.auth.manager.fetch_relay_models", return_value=([], {})), patch(
        "desktop.auth.manager.ensure_tokengine_catalog"
    ):
        manager._watchdog(loopback, "http://localhost/callback", "machine")
    assert manager._result["ok"] is True


def test_manual_model_refresh_does_not_report_login(tmp_path):
    on_login = Mock()
    manager, loopback = login_manager(tmp_path, on_login)
    manager._store.load.side_effect = lambda: {"token": "sk-business", "access_token": "oauth-only"}
    manager._refresh_access_token = Mock(return_value=False)
    with patch("desktop.auth.manager.fetch_relay_models", return_value=([], {})), patch(
        "desktop.auth.manager.ensure_tokengine_catalog", return_value=tmp_path
    ):
        assert manager.refresh_models()["ok"] is True
    on_login.assert_not_called()


def test_telemetry_never_uses_business_or_expired_token(tmp_path):
    manager, loopback = login_manager(tmp_path, Mock())
    manager._store.load.side_effect = lambda: {"token": "sk-business"}
    assert manager.telemetry_access_token() == ""
    manager._store.load.side_effect = lambda: {
        "access_token": "expired-oauth", "expires_at": time.time() - 1,
    }
    assert manager.telemetry_access_token() == ""
    manager._client.userinfo.assert_not_called()


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_boot_reports_once_after_background_refresh_and_manual_refresh_does_not(tmp_path, monkeypatch, refresh_fails):
    pytest.importorskip("webview")
    from desktop import main as app

    monkeypatch.setattr(app, "_shared", {})
    monkeypatch.setattr(app.rt, "ensure_runtime", Mock(return_value=(tmp_path, tmp_path)))
    monkeypatch.setattr(app.rt, "default_workspace", Mock(return_value=tmp_path))
    monkeypatch.setattr(app.rt, "resolve_deeptutor_version", Mock(return_value="test"))
    process = Mock()
    process.wait_ready.return_value = ("http://127.0.0.1:3782", "ready")
    monkeypatch.setattr(app, "DeepTutorProcess", Mock(return_value=process))
    for name in ("EngineLogTail", "AccountStatusSync", "ToastInjector", "run_session"):
        monkeypatch.setattr(app, name, Mock())
    reporter = Mock()
    reporter_factory = Mock(return_value=reporter)
    monkeypatch.setattr(app, "TelemetryReporter", reporter_factory)
    threads = []

    def defer_thread(**kwargs):
        threads.append(kwargs)
        return Mock()

    monkeypatch.setattr(app.threading, "Thread", defer_thread)
    auth = Mock()
    if refresh_fails:
        auth.refresh_models.side_effect = OSError("offline")
    else:
        auth.refresh_models.return_value = {"ok": True}
    app.bootstrap(Mock(), Mock(), auth)
    process.supervise.assert_called_once()
    reporter.report.assert_not_called()  # boot never waits for the network task
    refresh = next(thread for thread in threads if thread.get("name") == "dt-boot-refresh")
    refresh["target"]()
    reporter.report.assert_called_once_with("app_start")
    assert reporter_factory.call_args.kwargs["access_token"] == auth.telemetry_access_token
    auth.refresh_models.side_effect = None
    bridge = app.Api("http://127.0.0.1:3782", auth, website_url="http://website.example")
    bridge.refresh_models()
    reporter.report.assert_called_once_with("app_start")
