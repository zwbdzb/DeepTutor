from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from deeptutor.services import app_update
from deeptutor.services.app_update import (
    DesktopVersionCheckService,
    UpdateJobStore,
    VersionCheckError,
    VersionCheckService,
)


def _client_factory(payload: dict, *, status_code: int = 200):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _desktop_client_factory(payload: dict, *, status_code: int = 200):
    return _client_factory(payload, status_code=status_code)


def _desktop_manifest(**overrides: object) -> dict:
    payload = {
        "app": "ThinkBuddy",
        "version": "0.1.524.1100",
        "url": "https://thinkbuddy.hanyoai.com/update/ThinkBuddySetup-dt0.1.524.1100.exe",
        "sha256": "a" * 64,
        "size": 1024,
        "published_at": "2026-10-16T15:54:00+08:00",
    }
    payload.update(overrides)
    return payload


def _release(**overrides: object) -> dict:
    payload = {
        "tag_name": "v1.7.0",
        "name": "DeepTutor 1.7",
        "published_at": "2026-08-30T00:00:00Z",
        "html_url": "https://github.com/HKUDS/DeepTutor/releases/tag/v1.7.0",
        "body": "A stable release.",
        "draft": False,
        "prerelease": False,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_version_check_caches_success_for_the_ttl(monkeypatch) -> None:
    monkeypatch.setattr(app_update, "__version__", "1.6.15")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_release())

    service = VersionCheckService(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: 100,
    )

    first = await service.check()
    second = await service.check()

    assert calls == 1
    assert first.cached is False
    assert second.cached is True
    assert second.release.version == "1.7.0"
    assert second.update_available is True


@pytest.mark.asyncio
async def test_version_check_falls_back_to_latest_redirect_when_api_is_rate_limited() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.url.host == "api.github.com":
            return httpx.Response(403, json={"message": "API rate limit exceeded"})
        return httpx.Response(
            302,
            headers={"location": "https://github.com/HKUDS/DeepTutor/releases/tag/v1.6.1"},
        )

    service = VersionCheckService(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await service.check()

    assert requests == [
        ("GET", app_update.GITHUB_LATEST_RELEASE_URL),
        ("HEAD", app_update.GITHUB_LATEST_RELEASE_WEB_URL),
    ]
    assert result.release.version == "1.6.1"
    assert result.release.url == "https://github.com/HKUDS/DeepTutor/releases/tag/v1.6.1"
    assert result.update_available is False


@pytest.mark.asyncio
async def test_version_check_rejects_untrusted_latest_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(403, json={"message": "API rate limit exceeded"})
        return httpx.Response(302, headers={"location": "https://example.com/tag/v9.9.9"})

    service = VersionCheckService(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(VersionCheckError, match="invalid URL"):
        await service.check()


@pytest.mark.asyncio
async def test_version_check_rejects_prereleases_and_untrusted_urls() -> None:
    prerelease = VersionCheckService(
        client_factory=_client_factory(_release(prerelease=True)),
    )
    bad_url = VersionCheckService(
        client_factory=_client_factory(_release(html_url="https://example.com/v1.7.0")),
    )

    with pytest.raises(VersionCheckError):
        await prerelease.check()
    with pytest.raises(VersionCheckError):
        await bad_url.check()


@pytest.mark.asyncio
async def test_release_marks_migration_notes() -> None:
    service = VersionCheckService(
        client_factory=_client_factory(_release(body="Breaking changes: run migration first.")),
    )

    result = await service.check()

    assert result.release.migration_warning is True


@pytest.mark.asyncio
async def test_release_excerpt_is_clean_plain_text() -> None:
    service = VersionCheckService(
        client_factory=_client_factory(
            _release(
                body=(
                    "# DeepTutor release notes\n\n"
                    "**Release Date:** 2026.08.30\n\n"
                    "Use the [new updater](https://example.com) with **one confirmation**."
                )
            )
        ),
    )

    result = await service.check()

    assert result.release.excerpt == "Use the new updater with one confirmation."


def test_detect_installation_keeps_source_and_docker_host_managed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app_update, "_running_in_container", lambda: True)
    assert app_update.detect_installation().mode == "docker"

    monkeypatch.setattr(app_update, "_running_in_container", lambda: False)
    monkeypatch.setattr(app_update, "_running_from_source_checkout", lambda: False)
    monkeypatch.setattr(
        app_update,
        "_distribution_direct_url",
        lambda: {"dir_info": {"editable": True}},
    )
    assert app_update.detect_installation().mode == "source"

    monkeypatch.setattr(app_update, "_distribution_direct_url", lambda: {})
    monkeypatch.setattr(app_update, "running_under_systemd_service", lambda: False)
    monkeypatch.setattr(app_update.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(app_update.sys, "base_prefix", str(tmp_path / "base"))
    installation = app_update.detect_installation()
    assert installation.mode == "pypi"
    assert installation.automatic_update is True


def test_systemd_service_cgroup_disables_in_app_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert (
        app_update._systemd_service_unit("0::/system.slice/deeptutor.service\n")
        == "deeptutor.service"
    )
    assert (
        app_update._systemd_service_unit(
            "1:name=systemd:/user.slice/user@1000.service/app.slice/deeptutor.service\n"
        )
        == "deeptutor.service"
    )
    assert app_update._systemd_service_unit("0::/user.slice/session-2.scope\n") is None
    assert (
        app_update._systemd_service_unit(
            "0::/user.slice/user@1000.service/app.slice/app-gnome-terminal-123.scope\n"
        )
        is None
    )
    assert app_update._systemd_service_unit("0::/user.slice/user@1000.service/app.slice\n") is None
    assert (
        app_update._systemd_service_unit("0::/system.slice/deeptutor.service/worker\n")
        == "deeptutor.service"
    )

    monkeypatch.setattr(app_update, "_running_in_container", lambda: False)
    monkeypatch.setattr(app_update, "_running_from_source_checkout", lambda: False)
    monkeypatch.setattr(app_update, "_distribution_direct_url", lambda: {})
    monkeypatch.setattr(app_update, "running_under_systemd_service", lambda: True)
    monkeypatch.setattr(app_update.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(app_update.sys, "base_prefix", str(tmp_path / "base"))

    installation = app_update.detect_installation()
    assert installation.mode == "pypi"
    assert installation.automatic_update is False
    assert "systemctl" in installation.reason


def test_desktop_scope_is_not_mistaken_for_systemd_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_update.sys, "platform", "linux")
    monkeypatch.setenv("INVOCATION_ID", "desktop-app-unit")
    monkeypatch.setattr(
        app_update.Path,
        "read_text",
        lambda self, **kwargs: (
            "0::/user.slice/user@1000.service/app.slice/app-gnome-terminal-123.scope\n"
        ),
    )
    assert app_update.running_under_systemd_service() is False

    monkeypatch.setattr(app_update.Path, "read_text", lambda self, **kwargs: "0::/\n")
    assert app_update.running_under_systemd_service() is True


def test_detect_installation_prefers_source_checkout_when_metadata_is_shadowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout_root = tmp_path / "DeepTutor"
    module_path = checkout_root / "deeptutor" / "services" / "app_update.py"
    module_path.parent.mkdir(parents=True)
    module_path.touch()
    (checkout_root / ".git").mkdir()
    (checkout_root / "pyproject.toml").write_text(
        '[project]\nname = "deeptutor"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(app_update, "__file__", str(module_path))
    monkeypatch.setattr(app_update, "_running_in_container", lambda: False)
    monkeypatch.setattr(app_update, "_distribution_direct_url", lambda: {})
    monkeypatch.setattr(app_update.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(app_update.sys, "base_prefix", str(tmp_path / "base"))

    installation = app_update.detect_installation()

    assert installation.mode == "source"
    assert installation.automatic_update is False
    assert installation.command == "git pull && pip install -e ."


def test_update_job_store_persists_trusted_lifecycle(tmp_path: Path) -> None:
    store = UpdateJobStore(tmp_path / "update")
    job = store.create(current_version="1.6.1", target_version="1.7.0")
    home = tmp_path / "home"

    handoff = store.prepare_handoff(
        job.id,
        home=home,
        restart_argv=["start", "--home", str(home.resolve())],
    )
    running = store.mark_running(job.id)
    restarting = store.mark_restarting(job.id)
    succeeded = store.mark_succeeded(job.id)

    assert handoff.status == "handoff"
    assert running.started_at
    assert restarting.restart_count == 1
    assert succeeded.status == "succeeded"
    assert not store.active_path.exists()


def test_update_job_rejects_tampered_restart_arguments(tmp_path: Path) -> None:
    store = UpdateJobStore(tmp_path / "update")
    job = store.create(current_version="1.6.1", target_version="1.7.0")
    home = tmp_path / "home"

    with pytest.raises(ValueError, match="Invalid restart arguments"):
        store.prepare_handoff(
            job.id,
            home=home,
            restart_argv=["start", "--home", str(home.resolve()), "--port", "9999"],
        )


def test_launcher_available_uses_the_read_only_process_probe(monkeypatch) -> None:
    probed: list[int] = []
    monkeypatch.setenv(app_update.LAUNCHER_PID_ENV, "4242")
    monkeypatch.setattr(
        app_update,
        "is_process_alive",
        lambda pid: probed.append(pid) or True,
    )

    assert app_update.launcher_available() is True
    assert probed == [4242]


def _desktop_env(monkeypatch: pytest.MonkeyPatch, *, version: str | None = "0.1.524.1030") -> None:
    monkeypatch.setenv(app_update.DESKTOP_SHELL_MANAGED_ENV, "1")
    if version is None:
        monkeypatch.delenv(app_update.DESKTOP_VERSION_ENV, raising=False)
    else:
        monkeypatch.setenv(app_update.DESKTOP_VERSION_ENV, version)


@pytest.mark.asyncio
async def test_desktop_check_reports_available_for_newer_manifest(monkeypatch) -> None:
    _desktop_env(monkeypatch)
    service = DesktopVersionCheckService(
        client_factory=_desktop_client_factory(_desktop_manifest()),
    )

    result = await service.check()

    assert result.enabled is True
    assert result.update_available is True
    assert result.version == "0.1.524.1100"
    assert result.error == ""
    assert result.url.startswith("https://thinkbuddy.hanyoai.com/update/")


@pytest.mark.asyncio
async def test_desktop_check_reports_not_available_when_same_or_older(monkeypatch) -> None:
    _desktop_env(monkeypatch, version="0.1.524.1100")
    service = DesktopVersionCheckService(
        client_factory=_desktop_client_factory(_desktop_manifest()),
    )

    result = await service.check()

    assert result.enabled is True
    assert result.update_available is False
    assert result.version == "0.1.524.1100"

    older = DesktopVersionCheckService(
        client_factory=_desktop_client_factory(
            _desktop_manifest(version="0.1.524.0999"),
        ),
    )
    older_result = await older.check()
    assert older_result.update_available is False


@pytest.mark.asyncio
async def test_desktop_check_caches_success_for_the_ttl(monkeypatch) -> None:
    _desktop_env(monkeypatch)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_desktop_manifest())

    service = DesktopVersionCheckService(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: 100,
    )

    first = await service.check()
    second = await service.check()

    assert calls == 1
    assert first.cached is False
    assert second.cached is True


@pytest.mark.asyncio
async def test_desktop_check_degrades_to_unavailable_on_error(monkeypatch) -> None:
    _desktop_env(monkeypatch)
    service = DesktopVersionCheckService(
        client_factory=_desktop_client_factory({"detail": "boom"}, status_code=503),
    )

    result = await service.check()

    assert result.enabled is True
    assert result.update_available is False
    assert result.version == ""
    assert result.error


@pytest.mark.asyncio
async def test_desktop_check_rejects_invalid_manifest(monkeypatch) -> None:
    _desktop_env(monkeypatch)
    service = DesktopVersionCheckService(
        client_factory=_desktop_client_factory(_desktop_manifest(version="")),
    )

    result = await service.check()

    assert result.update_available is False
    assert result.version == ""
    assert "missing" in result.error.lower()


@pytest.mark.asyncio
async def test_desktop_check_is_disabled_without_desktop_environment(monkeypatch) -> None:
    monkeypatch.delenv(app_update.DESKTOP_SHELL_MANAGED_ENV, raising=False)
    monkeypatch.delenv(app_update.DESKTOP_VERSION_ENV, raising=False)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_desktop_manifest())

    service = DesktopVersionCheckService(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await service.check()

    assert calls == 0
    assert result.enabled is False
    assert result.update_available is False
    assert result.version == ""


@pytest.mark.asyncio
async def test_desktop_check_selects_macos_arch_entry(monkeypatch) -> None:
    _desktop_env(monkeypatch, version="0.1.524.1030")
    monkeypatch.setattr(app_update.sys, "platform", "darwin")
    monkeypatch.setattr(app_update.platform, "machine", lambda: "arm64")
    payload = {
        "app": "ThinkBuddy",
        "version": "0.1.524.1000",
        "url": "https://thinkbuddy.hanyoai.com/update/ThinkBuddySetup-dt0.1.524.1000.exe",
        "sha256": "a" * 64,
        "mac": {
            "arm64": {
                "version": "0.1.524.1200",
                "url": "https://thinkbuddy.hanyoai.com/update/ThinkBuddyDesktop-mac-arm64-0.1.524.1200.dmg",
                "sha256": "a" * 64,
            },
            "x86_64": {
                "version": "0.1.524.1100",
                "url": "https://thinkbuddy.hanyoai.com/update/ThinkBuddyDesktop-mac-x86_64-0.1.524.1100.dmg",
                "sha256": "a" * 64,
            },
        },
    }
    service = DesktopVersionCheckService(
        client_factory=_desktop_client_factory(payload),
    )

    result = await service.check()

    assert result.update_available is True
    assert result.version == "0.1.524.1200"
    assert "arm64" in result.url
