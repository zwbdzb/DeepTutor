from __future__ import annotations

import json
import sys
from pathlib import Path

from pytest import MonkeyPatch

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop.auth import config


def write_endpoints(root: Path, **values: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "endpoints.json").write_text(json.dumps(values), encoding="utf-8")


def test_thinkbuddy_website_url_uses_production_default(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("THINKBUDDY_WEBSITE_URL", raising=False)

    assert config.thinkbuddy_website_url(tmp_path) == "https://thinkbuddy.hanyoai.com"


def test_thinkbuddy_website_url_reads_endpoints_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("THINKBUDDY_WEBSITE_URL", raising=False)
    write_endpoints(
        tmp_path,
        thinkbuddy_website_url="  https://website.example.com/  ",
    )

    assert config.thinkbuddy_website_url(tmp_path) == "https://website.example.com"


def test_thinkbuddy_website_url_prefers_nonblank_environment_override(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    write_endpoints(tmp_path, thinkbuddy_website_url="https://file.example.com")
    monkeypatch.setenv("THINKBUDDY_WEBSITE_URL", "  https://env.example.com/  ")

    assert config.thinkbuddy_website_url(tmp_path) == "https://env.example.com"

    monkeypatch.setenv("THINKBUDDY_WEBSITE_URL", "   ")
    assert config.thinkbuddy_website_url(tmp_path) == "https://file.example.com"
