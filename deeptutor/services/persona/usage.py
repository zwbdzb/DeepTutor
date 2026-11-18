"""
Per-account persona usage counters.
===================================

Each account's persona picks are tallied in its own settings file
(``settings/persona_usage.json``), keyed by persona name → usage count. The
composer's "Response style" list reads these counts to surface the most-used
personas first; picking a persona increments its count.

The file is per-account (unlike the global runtime settings) so the "most
used" ranking follows the user, not the machine.
"""

from __future__ import annotations

import json
from pathlib import Path

from deeptutor.multi_user.paths import get_account_path_service

USAGE_FILE = "persona_usage.json"


def _usage_path() -> Path:
    return get_account_path_service().get_settings_file(USAGE_FILE)


def load_persona_usage() -> dict[str, int]:
    """Return a ``{name: count}`` map, safest to treat as read-only."""
    path = _usage_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # A hand-edited or partially-written file must never break the
        # ranking; fall back to nothing rather than crash the list.
        return {}
    if not isinstance(payload, dict):
        return {}
    counts: dict[str, int] = {}
    for name, count in payload.items():
        if isinstance(name, str) and isinstance(count, int) and count > 0:
            counts[name] = count
    return counts


def bump_persona_usage(name: str) -> int:
    """Increment ``name``'s count and return the new value."""
    counts = load_persona_usage()
    next_count = counts.get(name, 0) + 1
    counts[name] = next_count
    _write_persona_usage(counts)
    return next_count


def _write_persona_usage(counts: dict[str, int]) -> None:
    from deeptutor.services.file_io import atomic_write_json

    atomic_write_json(_usage_path(), counts)
