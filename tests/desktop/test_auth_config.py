"""endpoints 键值语义重定位（2026-11）后的 auth.config 回归测试。

语义基线（与 desktop/auth/config.py 对齐）：

* 两个站点：ThinkBuddy 主站（``api_base``）与 Tokengine 认证+token 服务
  （``oauth_base``），优先级链均为 环境变量 > endpoints.json > 内置默认。
* ``update_base`` / ``telemetry_url`` **纯硬编码派生**自主站
  （``<api_base>/update``、``<api_base>/v1/events``），不再吃 env/file 覆盖。
* endpoints.json 中的废弃键（``thinkbuddy_website_url`` / ``update_url`` /
  ``telemetry_url``）只告警、值被忽略。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest
from pytest import MonkeyPatch

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))

from desktop.auth import config

PROD_API_BASE = "https://thinkbuddy.hanyoai.com"
PROD_OAUTH_BASE = "https://tokengine.hanyoai.com"

# 所有会改变端点解析结果的环境变量，测试前统一清空，防宿主机污染。
# THINKBUDDY_UPDATE_URL / TELEMETRY_URL 已退出覆盖链，这里一并清掉以证明
# 即使宿主机残留也不会影响派生结果。
_ENDPOINT_ENV_VARS = (
    "THINKBUDDY_API_BASE",
    "TOKENGINE_OAUTH_BASE",
    "TOKENGINE_AUTHORIZE_URL",
    "TOKENGINE_TOKEN_URL",
    "TOKENGINE_USERINFO_URL",
    "TOKENGINE_REVOKE_URL",
    "TOKENGINE_RELAY_BASE",
    "THINKBUDDY_UPDATE_URL",
    "TELEMETRY_URL",
)


@pytest.fixture(autouse=True)
def _clean_endpoint_env(monkeypatch: MonkeyPatch) -> None:
    for var in _ENDPOINT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def write_endpoints(root: Path, **values: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "endpoints.json").write_text(json.dumps(values), encoding="utf-8")


# --------------------------------------------------------------------------- #
# thinkbuddy_api_base（主站根地址）
# --------------------------------------------------------------------------- #
def test_thinkbuddy_api_base_uses_production_default(tmp_path: Path) -> None:
    assert config.thinkbuddy_api_base(tmp_path) == PROD_API_BASE


def test_thinkbuddy_api_base_reads_endpoints_file(tmp_path: Path) -> None:
    write_endpoints(tmp_path, api_base="  https://website.example.com/  ")

    assert config.thinkbuddy_api_base(tmp_path) == "https://website.example.com"


def test_thinkbuddy_api_base_prefers_nonblank_environment_override(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    write_endpoints(tmp_path, api_base="https://file.example.com")
    monkeypatch.setenv("THINKBUDDY_API_BASE", "  https://env.example.com/  ")

    assert config.thinkbuddy_api_base(tmp_path) == "https://env.example.com"

    monkeypatch.setenv("THINKBUDDY_API_BASE", "   ")
    assert config.thinkbuddy_api_base(tmp_path) == "https://file.example.com"


def test_thinkbuddy_api_base_empty_when_unresolvable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "DEFAULT_API_BASE", "")

    assert config.thinkbuddy_api_base(tmp_path) == ""


# --------------------------------------------------------------------------- #
# endpoints()：oauth 侧（Tokengine 认证 + token 服务）
# --------------------------------------------------------------------------- #
def test_endpoints_oauth_side_derives_from_production_default(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # DEFAULT_OAUTH_BASE 可能被宿主机 TOKENGINE_DEFAULT_OAUTH_BASE 烘焙，
    # 这里强制回生产值，验证的正是"无覆盖 → 生产默认 + 派生"。
    monkeypatch.setattr(config, "DEFAULT_OAUTH_BASE", config.PROD_OAUTH_BASE)

    endpoints = config.endpoints(tmp_path)

    assert endpoints["oauth_base"] == PROD_OAUTH_BASE
    assert endpoints["authorize_url"] == f"{PROD_OAUTH_BASE}/oauth/authorize"
    assert endpoints["token_url"] == f"{PROD_OAUTH_BASE}/oauth/token"
    assert endpoints["userinfo_url"] == f"{PROD_OAUTH_BASE}/oauth/userinfo"
    assert endpoints["revoke_url"] == f"{PROD_OAUTH_BASE}/oauth/revoke"
    assert endpoints["relay_base"] == f"{PROD_OAUTH_BASE}/v1"


def test_endpoints_oauth_base_from_file_drives_derivation(tmp_path: Path) -> None:
    write_endpoints(tmp_path, oauth_base="  http://127.0.0.1:3000/  ")

    endpoints = config.endpoints(tmp_path)

    assert endpoints["oauth_base"] == "http://127.0.0.1:3000"
    assert endpoints["authorize_url"] == "http://127.0.0.1:3000/oauth/authorize"
    assert endpoints["token_url"] == "http://127.0.0.1:3000/oauth/token"
    assert endpoints["userinfo_url"] == "http://127.0.0.1:3000/oauth/userinfo"
    assert endpoints["revoke_url"] == "http://127.0.0.1:3000/oauth/revoke"
    assert endpoints["relay_base"] == "http://127.0.0.1:3000/v1"


def test_endpoints_oauth_base_prefers_env_over_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    write_endpoints(tmp_path, oauth_base="http://127.0.0.1:3000")
    monkeypatch.setenv("TOKENGINE_OAUTH_BASE", "  http://127.0.0.1:4000/  ")

    endpoints = config.endpoints(tmp_path)

    assert endpoints["oauth_base"] == "http://127.0.0.1:4000"
    assert endpoints["authorize_url"] == "http://127.0.0.1:4000/oauth/authorize"
    assert endpoints["relay_base"] == "http://127.0.0.1:4000/v1"


# --------------------------------------------------------------------------- #
# update_base：纯硬编码派生，不吃 env / file
# --------------------------------------------------------------------------- #
def test_update_base_defaults_to_main_site_update_endpoint(tmp_path: Path) -> None:
    assert config.update_base(tmp_path) == f"{PROD_API_BASE}/update"


def test_update_base_ignores_legacy_env_and_file_overrides(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # 负面测试：两个旧覆盖入口都喂了不同的值，必须全部失效。
    monkeypatch.setenv("THINKBUDDY_UPDATE_URL", "https://env.example.com/updates")
    write_endpoints(tmp_path, update_url="https://file.example.com/update")

    assert config.update_base(tmp_path) == f"{PROD_API_BASE}/update"


def test_update_base_follows_main_site_override(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("THINKBUDDY_API_BASE", "http://127.0.0.1:8000")

    assert config.update_base(tmp_path) == "http://127.0.0.1:8000/update"


def test_update_base_empty_when_main_site_unresolvable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "DEFAULT_API_BASE", "")

    assert config.update_base(tmp_path) == ""


# --------------------------------------------------------------------------- #
# telemetry_url：纯硬编码派生，不吃 env / file
# --------------------------------------------------------------------------- #
def test_telemetry_url_defaults_to_main_site_events_endpoint(tmp_path: Path) -> None:
    assert config.telemetry_url(tmp_path) == f"{PROD_API_BASE}/v1/events"


def test_telemetry_url_ignores_legacy_env_and_file_overrides(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # 负面测试：TELEMETRY_URL 环境变量与 endpoints.json#telemetry_url 均已失效。
    monkeypatch.setenv("TELEMETRY_URL", "https://env.example.com/ingest")
    write_endpoints(tmp_path, telemetry_url="https://file.example.com/ingest")

    assert config.telemetry_url(tmp_path) == f"{PROD_API_BASE}/v1/events"


def test_telemetry_url_follows_main_site_override(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("THINKBUDDY_API_BASE", "http://127.0.0.1:8000")

    assert config.telemetry_url(tmp_path) == "http://127.0.0.1:8000/v1/events"


def test_telemetry_url_empty_when_main_site_unresolvable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "DEFAULT_API_BASE", "")

    assert config.telemetry_url(tmp_path) == ""


# --------------------------------------------------------------------------- #
# 废弃键：只告警、不生效
# --------------------------------------------------------------------------- #
def test_legacy_endpoint_keys_warn_and_are_ignored(tmp_path: Path, caplog) -> None:
    write_endpoints(
        tmp_path,
        thinkbuddy_website_url="https://legacy-website.example.com",
        update_url="https://legacy-updates.example.com/update",
        telemetry_url="https://legacy-telemetry.example.com/ingest",
        # 同时写 api_base，验证正常键不受废弃键干扰
        api_base="https://main.example.com",
    )

    # 废弃键的值一律被忽略，api_base 正常生效
    assert config.thinkbuddy_api_base(tmp_path) == "https://main.example.com"
    assert config.update_base(tmp_path) == "https://main.example.com/update"
    assert config.telemetry_url(tmp_path) == "https://main.example.com/v1/events"

    # 单次读文件恰好触发 3 条迁移告警（每个废弃键一条）
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="dt.auth.config"):
        config.thinkbuddy_api_base(tmp_path)

    legacy_keys = [
        record.args[0]
        for record in caplog.records
        if record.levelno == logging.WARNING and "废弃键" in record.getMessage()
    ]
    assert sorted(legacy_keys) == [
        "telemetry_url",
        "thinkbuddy_website_url",
        "update_url",
    ]
