from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop.auth import AuthManager
from desktop.auth.store import TokenStore


class StubStore:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def load(self) -> dict[str, Any]:
        return self.payload


def manager_with_account(tmp_path: Path, account: Any) -> AuthManager:
    manager = AuthManager(root=tmp_path / "root", home=tmp_path / "home")
    manager._store = StubStore({"account": account})
    return manager


def test_machine_id_reuses_existing_oauth_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    existing = "0123456789abcdef0123456789abcdef"
    (root / "machine_id").write_text(existing, encoding="utf-8")
    manager = AuthManager(root=root, home=tmp_path / "home")

    assert manager.machine_id() == existing
    assert manager.machine_id() == TokenStore(root).machine_id()
    assert not (root / "telemetry_device_id").exists()


def test_machine_id_is_generated_once_and_survives_logout(tmp_path: Path) -> None:
    root = tmp_path / "root"
    manager = AuthManager(root=root, home=tmp_path / "home")
    first = manager.machine_id()

    assert len(first) == 32
    assert int(first, 16) >= 0
    TokenStore(root).clear()
    assert AuthManager(root=root, home=tmp_path / "home").machine_id() == first
    assert (root / "machine_id").read_text(encoding="utf-8") == first


def test_provider_user_id_reads_oauth_subject(tmp_path: Path) -> None:
    manager = manager_with_account(tmp_path, {"raw": {"sub": "  u_314  "}})

    assert manager.provider_user_id() == "u_314"


def test_provider_user_id_rejects_missing_or_invalid_raw_account(
    tmp_path: Path,
) -> None:
    assert manager_with_account(tmp_path, {}).provider_user_id() == ""
    assert manager_with_account(tmp_path, {"raw": "invalid"}).provider_user_id() == ""
    assert manager_with_account(tmp_path, {"raw": {"sub": None}}).provider_user_id() == ""
