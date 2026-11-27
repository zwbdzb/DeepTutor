"""Read-only turn latency analysis.

The analyzer is intentionally read-only: it opens existing databases with
``mode=ro`` and derives a turn's time breakdown from data the runtime already
persists (``turn_events``, ``turns``, ``llm_calls``). It never writes, never
takes a write lock, and never creates a database, so running it against a live
installation cannot disturb a turn in flight.

Entry points:

``derive_turn(turn, events, llm_calls)``
    Pure function over already-loaded rows; the CLI and the HTTP API both use it.
``store.load_turn`` / ``store.load_events`` / ``store.load_llm_calls``
    Read-only loaders that feed ``derive_turn``.
"""

from __future__ import annotations

from .derive import derive_recent, derive_session, derive_turn

__all__ = ["derive_recent", "derive_session", "derive_turn"]
