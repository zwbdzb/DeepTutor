"""把 Tokengine 登录拿到的业务令牌与模型名单写入 model catalog。

桌面端的同名逻辑在 ``desktop-shell/desktop/auth/catalog.py``（由 Electron
登录流程调用）。手机端没有桌面壳，登录在引擎进程内完成，因此这里保留
**同样的写入结构**（connection + 各服务 profile + 活动指针），但通过
``ModelCatalogService.update`` 落盘，保证与设置页写入走同一套规范化。

与桌面端的差异：不做 reconcile（那是设置页整包写入的保护，手机端没有
设置页快照覆盖的路径）。其余分流规则、字段名、默认值逐一保持同步。
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import Any, Mapping
from uuid import uuid4

from .model_catalog import get_model_catalog_service

log = logging.getLogger(__name__)

CONNECTION_ID = "tokengine"
CONNECTION_NAME = "Tokengine（领先的MaaS服务和一站式大模型API聚合分发平台）"
_LEGACY_CONNECTION_NAMES = {"Tokengine"}

# 平台 model_type：1=文生文 2=文生图 3=文生视频 4=重排序 5=向量 6=语音识别 7=语音合成
_MODEL_TYPE_SERVICE: dict[int, str] = {
    1: "llm",
    2: "imagegen",
    3: "videogen",
    5: "embedding",
    6: "stt",
    7: "tts",
}

# 除 llm 外由登录流程自动挂 profile 的服务（task 恒为空授权：留空即 inherit）。
_TYPED_SERVICES = ("task", "embedding", "imagegen", "videogen", "stt", "tts")

_TTS_DEFAULT_VOICE_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"


def _service_shell() -> dict[str, Any]:
    return {"active_profile_id": None, "active_model_id": None, "profiles": []}


def _connection_id_of(profile: Mapping[str, Any]) -> str:
    connection_id = str(profile.get("connection_id") or "")
    if connection_id:
        return connection_id
    ref = profile.get("provider_ref")
    if isinstance(ref, Mapping):
        return str(ref.get("connection_id") or "")
    return ""


def _normalized_model_type(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def split_models_by_service(
    models: list[str],
    model_types: Mapping[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """按平台 ``model_type`` 把模型名单分拣到各服务。

    4（重排序）DeepTutor 没有对应服务，确定性丢弃；缺失/未知类型保守归
    对话（llm）。``task`` 恒为空列表，驱动后续「空授权即摘除」分支。
    """
    types = model_types or {}
    split: dict[str, list[dict[str, Any]]] = {name: [] for name in _TYPED_SERVICES}
    split["llm"] = []
    for raw in models:
        name = str(raw or "").strip()
        if not name:
            continue
        model_type = _normalized_model_type(types.get(name))
        if model_type == 4:
            continue
        service = _MODEL_TYPE_SERVICE.get(model_type if model_type is not None else 1, "llm")
        split[service].append({"name": name, "model_type": model_type})
    return split


def _model_entries(
    entries: list[dict[str, Any]],
    prefix: str,
    previous: Mapping[str, Mapping[str, Any]] | None = None,
    model_context_windows: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """构造 catalog 的模型行；复用上一次的配置（音色、上下文窗口等）。"""
    stamp = int(time.time() * 1000)
    detected_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        row: dict[str, Any] = {
            "id": f"{prefix}-model-{stamp}-{index}",
            "name": name,
            "model": name,
        }
        model_type = entry.get("model_type")
        if isinstance(model_type, int) and not isinstance(model_type, bool):
            row["model_type"] = model_type
        row.update((previous or {}).get(name, {}))
        if prefix == "tts" and name == _TTS_DEFAULT_VOICE_MODEL and not row.get("voice"):
            row["voice"] = "Eric"
        if model_context_windows is not None:
            window = model_context_windows.get(name)
            if isinstance(window, int) and not isinstance(window, bool) and window > 0:
                row["context_window"] = str(window)
                row["context_window_source"] = "metadata"
                row["context_window_detected_at"] = detected_at
                row.pop("context_window_tokens", None)
            elif row.get("context_window_source") == "metadata":
                for key in (
                    "context_window",
                    "context_window_tokens",
                    "context_window_source",
                    "context_window_detected_at",
                ):
                    row.pop(key, None)
        rows.append(row)
    return rows


def _remove_profile(catalog: dict[str, Any], service_name: str, connection_id: str) -> None:
    service = (catalog.get("services") or {}).get(service_name)
    if not isinstance(service, dict):
        return
    profiles = list(service.get("profiles") or [])
    kept = [
        profile
        for profile in profiles
        if not (isinstance(profile, dict) and _connection_id_of(profile) == connection_id)
    ]
    if len(kept) == len(profiles):
        return
    service["profiles"] = kept
    active = service.get("active_profile_id")
    if active and not any(isinstance(p, dict) and p.get("id") == active for p in kept):
        nxt = next((p.get("id") for p in kept if isinstance(p, dict) and p.get("api_key")), None)
        service["active_profile_id"] = nxt or None
        if not nxt:
            service["active_model_id"] = None


def _upsert_profile(
    catalog: dict[str, Any],
    service_name: str,
    connection_id: str,
    base_url: str,
    api_key: str,
    model_entries: list[dict[str, Any]],
    model_context_windows: Mapping[str, Any] | None = None,
) -> None:
    service = catalog.setdefault("services", {}).setdefault(service_name, _service_shell())
    profiles = service.setdefault("profiles", [])
    owned = [
        profile
        for profile in profiles
        if isinstance(profile, dict) and _connection_id_of(profile) == connection_id
    ]
    profile = next(
        (p for p in owned if p.get("id") == service.get("active_profile_id")),
        owned[0] if owned else None,
    )
    if profile is None:
        profile = {
            "id": f"{service_name}-profile-tokengine-{uuid4().hex[:8]}",
            "name": "Tokengine (OpenAI API)",
            "binding": "openai",
            "base_url": base_url,
            "api_key": api_key,
            "api_version": "",
            "extra_headers": {},
            "models": [],
            "connection_id": connection_id,
        }
        profiles.append(profile)
    else:
        profile["connection_id"] = connection_id
        profile["api_key"] = api_key
        if base_url:
            profile["base_url"] = base_url
        profiles[:] = [
            candidate
            for candidate in profiles
            if candidate is profile
            or not (
                isinstance(candidate, dict)
                and _connection_id_of(candidate) == connection_id
            )
        ]

    if not model_entries:
        return
    previous: dict[str, dict[str, Any]] = {
        str(m.get("model")): m
        for p in reversed(owned)
        for m in p.get("models") or []
        if isinstance(m, dict)
    }
    previous.update(
        {str(m.get("model")): m for m in profile.get("models") or [] if isinstance(m, dict)}
    )
    profile["models"] = _model_entries(
        model_entries,
        service_name,
        previous=previous,
        model_context_windows=model_context_windows,
    )
    if not service.get("active_profile_id"):
        service["active_profile_id"] = profile["id"]
        service["active_model_id"] = profile["models"][0]["id"]
    elif service.get("active_profile_id") == profile["id"]:
        active = service.get("active_model_id")
        if not any(m.get("id") == active for m in profile["models"]):
            service["active_model_id"] = profile["models"][0]["id"]


def _owned_llm_profile(
    catalog: dict[str, Any], connection_id: str, api_key: str
) -> dict[str, Any] | None:
    llm = catalog.get("services", {}).get("llm") or {}
    profiles = llm.get("profiles") or []
    active_id = str(llm.get("active_profile_id") or "")
    owned = [
        profile
        for profile in profiles
        if isinstance(profile, dict) and _connection_id_of(profile) == connection_id
    ]
    if owned:
        return next((p for p in owned if p.get("id") == active_id), owned[0])
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if (
            not profile.get("connection_id")
            and not profile.get("provider_ref")
            and str(profile.get("api_key") or "") == api_key
        ):
            return profile
    return None


def ensure_tokengine_catalog(
    api_key: str,
    base_url: str,
    models: list[str] | None = None,
    model_types: Mapping[str, Any] | None = None,
    model_context_windows: Mapping[str, Any] | None = None,
    connection_id: str = CONNECTION_ID,
) -> dict[str, Any]:
    """写入 Tokengine 连接与模型，返回 ``{"path", "services", "models"}``。"""

    def mutate(catalog: dict[str, Any]) -> None:
        connections = catalog.setdefault("connections", [])
        conn = next((c for c in connections if c.get("id") == connection_id), None)
        if conn is None:
            conn = {
                "id": connection_id,
                "provider": "openai",
                "name": CONNECTION_NAME,
                "api_key": api_key,
                "base_url": base_url,
                "api_version": "",
                "extra_headers": {},
            }
            connections.append(conn)
        else:
            conn["api_key"] = api_key
            if base_url:
                conn["base_url"] = base_url
            if conn.get("name") in _LEGACY_CONNECTION_NAMES:
                conn["name"] = CONNECTION_NAME

        llm = catalog["services"].setdefault("llm", _service_shell())
        profiles = llm.setdefault("profiles", [])
        profile = _owned_llm_profile(catalog, connection_id, api_key)
        if profile is None:
            profile = {
                "id": f"llm-profile-tokengine-{uuid4().hex[:8]}",
                "name": "Tokengine (OpenAI API)",
                "binding": "openai",
                "base_url": base_url,
                "api_key": api_key,
                "api_version": "",
                "extra_headers": {},
                "wire_api": "auto",
                "api_format": "auto",
                "models": [],
                "connection_id": connection_id,
            }
            profiles.append(profile)
        else:
            profile["connection_id"] = connection_id
            profile["api_key"] = api_key
            if base_url:
                profile["base_url"] = base_url
            profiles[:] = [
                candidate
                for candidate in profiles
                if candidate is profile
                or _connection_id_of(candidate) != connection_id
            ]

        split = split_models_by_service(models or [], model_types)
        if models is not None and split["llm"]:
            previous = {
                str(row.get("model")): row
                for row in profile.get("models") or []
                if isinstance(row, dict)
            }
            profile["models"] = _model_entries(
                split["llm"], "llm", previous=previous,
                model_context_windows=model_context_windows,
            )
            if not any(row["id"] == llm.get("active_model_id") for row in profile["models"]):
                llm["active_model_id"] = profile["models"][0]["id"]
        elif models is not None:
            profile["models"] = []
            llm["active_model_id"] = None
            log.warning("授权模型中没有对话类型，llm 服务留空")

        if models is not None:
            for service_name in _TYPED_SERVICES:
                if split.get(service_name):
                    _upsert_profile(
                        catalog, service_name, connection_id, base_url,
                        api_key, split[service_name],
                        model_context_windows=model_context_windows,
                    )
                else:
                    _remove_profile(catalog, service_name, connection_id)

        if not llm.get("active_profile_id"):
            llm["active_profile_id"] = profile["id"]

    service = get_model_catalog_service()
    catalog = service.update(mutate)
    llm_profiles = (catalog.get("services", {}).get("llm", {}) or {}).get("profiles") or []
    llm_models = [
        row.get("model")
        for profile in llm_profiles
        if isinstance(profile, dict) and _connection_id_of(profile) == connection_id
        for row in profile.get("models") or []
        if isinstance(row, dict)
    ]
    return {
        "path": str(getattr(service, "path", "")),
        "models": [name for name in llm_models if name],
    }
