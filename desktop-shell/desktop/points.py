"""Client for the local DeepTutor points proxy.

The desktop shell forwards the short-lived Tokengine OAuth access token to
DeepTutor's local ``/api/points`` routes.  The local backend owns the remote
points-service URL, so the shell never needs a second production endpoint and
never exposes the token to a remote page.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class PointsApiError(RuntimeError):
    """A local points proxy failure with its HTTP status preserved."""

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
    backend_base: str, access_token: str, timeout: float = 12.0
) -> dict[str, Any]:
    """Trigger one idempotent check-in through DeepTutor's local backend."""
    token = str(access_token or "").strip()
    if not token:
        raise PointsApiError("Tokengine OAuth access token is required", 401)

    url = str(backend_base or "").strip().rstrip("/") + "/api/points/checkin"
    request = urllib.request.Request(
        url,
        data=b"",
        headers={
            "Accept": "application/json",
            "X-Tokengine-Access-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        fallback = f"Points request failed (HTTP {exc.code})"
        raise PointsApiError(_error_detail(raw, fallback), exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise PointsApiError(f"Local points proxy is unavailable: {reason}") from exc

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise PointsApiError("Local points proxy returned invalid data", 502) from exc
    if not isinstance(payload, dict):
        raise PointsApiError("Local points proxy returned invalid data", 502)
    return payload
