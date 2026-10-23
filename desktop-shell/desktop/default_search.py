"""Temporary shared IQS search for fresh ThinkBuddy workspaces."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from desktop.auth.catalog import _atomic_write_json, _default_catalog, catalog_path

log = logging.getLogger("dt.default_search")

# Temporary trial credential, bundled with the desktop shell.
_DEFAULT_IQS_API_KEY = (
    'wJLDzrDxTQFQ3omG0oKD8_XG0zxmHhg2YWRiMWVkOQ'
)
_IQS_BASE_URL = "https://cloud-iqs.aliyuncs.com/search/genericSearch"


def ensure_default_search(home: Path) -> bool:
    """Seed search once without replacing existing or subsequently deleted settings."""
    path = catalog_path(home)
    marker = path.parent / ".default-search-initialized.json"
    if marker.exists():
        return False
    # Do not use login's recovery loader: malformed settings must remain intact.
    catalog = (
        json.loads(path.read_text(encoding="utf-8-sig"))
        if path.exists() else _default_catalog()
    )
    search = catalog.setdefault("services", {}).setdefault(
        "search", {"active_profile_id": None, "profiles": []},
    )
    if search.get("active_profile_id") or search.get("profiles"):
        _atomic_write_json(marker, {"version": 1})
        return False
    if not _DEFAULT_IQS_API_KEY.strip():
        raise ValueError("The bundled IQS search key is missing.")
    profile = {
        "id": "search-profile-thinkbuddy-aliyun-iqs",
        "name": "阿里云搜索",
        "provider": "aliyun_iqs",
        "api_key": _DEFAULT_IQS_API_KEY,
        "base_url": _IQS_BASE_URL,
        "proxy": "",
    }
    search["profiles"] = [profile]
    search["active_profile_id"] = profile["id"]
    _atomic_write_json(path, catalog)
    _atomic_write_json(marker, {"version": 1})
    log.info("Default Aliyun IQS search configured")
    return True
