"""Account-level indexes across content stores, without changing their isolation.

Only account management (Settings) opts in; navigation surfaces stay within
the request's workspace. Content reads and mutations still resolve exactly
one workspace from the request. Every row names its authoritative source,
including legacy records whose preferences contain no workspace id.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from deeptutor.services.workspace import get_content_workspace_service
from deeptutor.services.workspace.context import workspace_context

logger = logging.getLogger(__name__)


async def read_workspace_indexes(
    reader: Callable[[], Awaitable[list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    catalog = get_content_workspace_service().list_workspaces()
    # Archived workspaces are hidden everywhere else (the picker and the
    # catalog resolve path both skip them), so polling them here only reports
    # a folder the learner has already retired. Invalid paths are carried
    # through instead: their sessions still belong to the account, and the
    # reading attempt is what reports the folder as unavailable.
    meta: dict[str, dict[str, Any]] = {
        "": {
            "display_name": "",
            "status": "ready",
            "error": "",
        }
    }
    for row in catalog:
        if row["kind"] != "workspace" or row.get("archived"):
            continue
        meta[row["workspace_id"]] = {
            "display_name": row["display_name"],
            "status": row.get("status", "ready"),
            "error": row.get("error", ""),
        }
    ids = list(meta.keys())
    rows: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for workspace_id in dict.fromkeys(ids):
        info = meta[workspace_id]
        try:
            with workspace_context(workspace_id):
                rows.extend(
                    {
                        **row,
                        "content_workspace_id": workspace_id,
                        "content_workspace_name": info["display_name"],
                        "content_workspace_status": info["status"],
                        "content_workspace_error": info["error"],
                    }
                    for row in await reader()
                )
        except Exception:
            # One offline folder must not hide the rest of the account history.
            logger.warning(
                "Workspace navigation index unavailable: %s", workspace_id, exc_info=True
            )
            unavailable.append(workspace_id)
    return rows, unavailable


async def session_index(limit: int, offset: int) -> dict[str, Any]:
    from deeptutor.services.session import get_session_store

    async def read():
        store = get_session_store()
        rows = []
        # Read only the prefix needed for the globally sorted page. Store APIs
        # have their own page caps; never pass an unbounded offset+limit to them.
        for start in range(0, offset + limit, 100):
            size = min(100, offset + limit - start)
            page = await store.list_sessions(limit=size, offset=start)
            rows.extend(page)
            if len(page) < size:
                break
        return rows

    rows, unavailable = await read_workspace_indexes(read)
    rows.sort(key=lambda row: (-row["updated_at"], row["content_workspace_id"], row["session_id"]))
    return {
        "sessions": rows[offset : offset + limit],
        "limit": limit,
        "offset": offset,
        "unavailable_workspaces": unavailable,
    }
