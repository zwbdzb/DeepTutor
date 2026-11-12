"""Discover shared materials and register received WeKnora connections normally."""

from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import logging
import threading
import time

from .client import PREFIX, LibraryClient, LibraryError, library_url

logger = logging.getLogger(__name__)


class _Catalog:
    def __init__(self, url):
        self.url = url
        self.rows = []
        self.expires = 0.0
        self.lock = threading.Lock()

    def read(self):
        with self.lock:
            if time.monotonic() >= self.expires:
                try:
                    self.rows = LibraryClient(self.url, timeout=5.0).catalog()
                except LibraryError:
                    logger.warning("Platform library discovery is temporarily unavailable.")
                self.expires = time.monotonic() + 30
            return deepcopy(self.rows)


@lru_cache(maxsize=8)
def _catalog(url):
    return _Catalog(url)


def sync_weknora_connections() -> None:
    from deeptutor.multi_user.knowledge_access import account_kb_manager, current_kb_manager
    from deeptutor.services.file_io import atomic_write_json
    from deeptutor.services.workspace.activity import data_activity
    from deeptutor.services.workspace.context import workspace_context
    from deeptutor.services.workspace.knowledge import (
        _move_aliases_path,
        canonical_kb_id,
        move_aliases,
        parse_kb_id,
    )
    from deeptutor.services.workspace.models import WorkspaceError

    url = library_url()
    if not url:
        return
    rows = _catalog(url).read()
    if not rows:
        return
    account = account_kb_manager()
    with data_activity(), account._catalog_lock:
        aliases = move_aliases()
        for row in rows:
            ref = PREFIX + row["id"]
            try:
                if ref not in aliases:
                    name = _register_weknora(account, row, aliases)
                    aliases[ref] = f"account:kb:{name}"
                    atomic_write_json(_move_aliases_path(), aliases)
                # Existing aliases also remember deletion and follow subsequent moves.
                workspace_id, name = parse_kb_id(canonical_kb_id(ref))
                with workspace_context(workspace_id) as scope:
                    if scope.archived:
                        continue
                    manager = current_kb_manager()
                    with manager._catalog_lock:
                        entry = manager.get_kb_entry(name)
                        if not entry or entry.get("platform_resource_id") != ref:
                            continue
                        connection = row["connection"]
                        if all(entry.get(key) == value for key, value in connection.items()):
                            continue
                        manager.config = manager._load_config()
                        manager.config["knowledge_bases"][name].update(
                            **connection, updated_at=datetime.now().isoformat()
                        )
                        manager._save_config()
            except (OSError, ValueError, WorkspaceError):
                logger.warning("Could not register received WeKnora connection %s.", row["id"])


def _register_weknora(manager, row, aliases):
    from deeptutor.knowledge.naming import validate_knowledge_base_name
    from deeptutor.services.rag.pipelines.weknora.config import normalize_base_url

    entries = manager._load_config().get("knowledge_bases", {})
    connection = row["connection"]
    for name, entry in entries.items():
        if (
            entry.get("type") == "weknora"
            and normalize_base_url(entry.get("server_url", "")) == connection["server_url"]
            and entry.get("knowledge_base_id") == connection["knowledge_base_id"]
        ):
            return name
    try:
        preferred = validate_knowledge_base_name(row["name"])
    except ValueError:
        preferred = f"WeKnora {row['id'][:112]}"
    name, suffix = preferred, 1
    while name in entries or f"account:kb:{name}" in aliases:
        suffix += 1
        ending = f" ({suffix})"
        name = preferred[: 120 - len(ending)] + ending
    entry = manager.register_weknora_kb(name, **connection, description=row["description"])
    entry["platform_resource_id"] = PREFIX + row["id"]
    manager._save_config()
    return name
