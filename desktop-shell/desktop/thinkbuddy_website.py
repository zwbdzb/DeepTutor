"""Minimal desktop client for the external thinkbuddy-website service."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

_PROVIDER_USER_ID = re.compile(r"u_[1-9][0-9]*\Z")


class ThinkBuddyWebsiteError(RuntimeError):
    """A thinkbuddy-website request failure with its HTTP status preserved."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


def _error_detail(raw: bytes, fallback: str) -> str:
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (TypeError, ValueError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    detail = payload.get("detail") or payload.get("error") or payload.get("message")
    return str(detail).strip() if detail else fallback


def checkin(
    base_url: str,
    provider_user_id: str,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Check in one OAuth user directly with thinkbuddy-website."""
    user_id = str(provider_user_id or "").strip()
    if not _PROVIDER_USER_ID.fullmatch(user_id):
        raise ThinkBuddyWebsiteError("invalid provider user id")

    url = str(base_url or "").strip().rstrip("/")
    if not url:
        raise ThinkBuddyWebsiteError("thinkbuddy-website URL is required")

    body = json.dumps(
        {"provider_user_id": user_id},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        request = urllib.request.Request(
            f"{url}/api/v1/checkin",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        finally:
            exc.close()
        fallback = f"thinkbuddy-website request failed (HTTP {exc.code})"
        raise ThinkBuddyWebsiteError(
            _error_detail(raw, fallback),
            exc.code,
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ThinkBuddyWebsiteError(
            f"thinkbuddy-website is unavailable: {reason}"
        ) from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ThinkBuddyWebsiteError(
            "thinkbuddy-website returned invalid data",
            502,
        ) from exc
    if not isinstance(payload, dict):
        raise ThinkBuddyWebsiteError(
            "thinkbuddy-website returned invalid data",
            502,
        )
    return payload
