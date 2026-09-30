from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop.auth import AuthManager


class StubStore:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def load(self) -> dict[str, Any]:
        return self.payload


def manager_with_account(tmp_path: Path, account: Any) -> AuthManager:
    manager = AuthManager(root=tmp_path / "root", home=tmp_path / "home")
    manager._store = StubStore({"account": account})
    return manager


def test_provider_user_id_reads_oauth_subject(tmp_path: Path) -> None:
    manager = manager_with_account(tmp_path, {"raw": {"sub": "  u_314  "}})

    assert manager.provider_user_id() == "u_314"


def test_provider_user_id_rejects_missing_or_invalid_raw_account(
    tmp_path: Path,
) -> None:
    assert manager_with_account(tmp_path, {}).provider_user_id() == ""
    assert manager_with_account(tmp_path, {"raw": "invalid"}).provider_user_id() == ""
    assert manager_with_account(tmp_path, {"raw": {"sub": None}}).provider_user_id() == ""
