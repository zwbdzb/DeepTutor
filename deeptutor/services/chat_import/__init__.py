"""Local chat-history importers for the desktop / local backend.

The browser adapters under ``web/lib/chat-import`` read the user's coding-CLI
folders through the File System Access API. On the desktop app the backend runs
on the user's own machine, so instead of asking the browser to browse a hidden
folder we can scan and parse the source folders server-side, which works in any
WebView and lets us reach metadata (like a soft-delete flag) that a file scan
alone cannot. The normalized transcripts produced here reuse the very same
``SQLiteSessionStore.import_session`` persistence as the browser adapters, so
imported sessions stay idempotent and re-openable.
"""

__all__ = ["workbuddy"]
