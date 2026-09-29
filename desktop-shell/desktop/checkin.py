"""Desktop check-in orchestration independent of the native GUI stack."""
from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

from desktop.thinkbuddy_website import (
    ThinkBuddyWebsiteError,
    checkin as request_website_checkin,
)

log = logging.getLogger("dt.checkin")

_GRANT_MESSAGES = {
    "succeeded": "，额度已到账",
    "pending": "，额度发放中",
    "processing": "，额度发放中",
    "failed": "，额度发放失败",
}


class OAuthSubject(Protocol):
    """Source of the stable user ID received from OAuth userinfo."""

    def provider_user_id(self) -> str:
        """Return the current Tokengine OAuth subject, or an empty string."""
        ...


class CheckinCoordinator:
    """Coordinate one direct desktop check-in without exposing OAuth tokens."""

    def __init__(self, auth: OAuthSubject, website_url: str) -> None:
        self._auth = auth
        self._website_url = str(website_url or "").strip().rstrip("/")
        self._lock = threading.Lock()

    def checkin(self) -> dict[str, Any]:
        """Submit one check-in while rejecting concurrent clicks."""
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "in_progress", "message": "签到处理中，请稍候"}
        try:
            provider_user_id = self._auth.provider_user_id()
            if not provider_user_id:
                return {
                    "ok": False,
                    "error": "missing_provider_user_id",
                    "message": "无法读取登录用户信息，请切换账号后重试",
                }
            try:
                payload = request_website_checkin(
                    self._website_url,
                    provider_user_id,
                )
            except ThinkBuddyWebsiteError as exc:
                return self._provider_error(exc)
            return self._result(payload)
        finally:
            self._lock.release()

    @staticmethod
    def _provider_error(exc: ThinkBuddyWebsiteError) -> dict[str, Any]:
        detail = str(exc)
        log.warning(
            "thinkbuddy-website check-in failed: status=%s",
            exc.status_code,
        )
        if detail == "invalid provider user id":
            message = "登录用户信息无效，请切换账号后重试"
        elif exc.status_code == 409 or detail == "checkin_disabled":
            message = "签到功能暂未开放"
        elif exc.status_code == 0:
            message = "签到服务连接失败，请稍后重试"
        else:
            message = "签到服务暂不可用，请稍后重试"
        return {
            "ok": False,
            "error": "checkin_failed",
            "message": message,
            "status_code": exc.status_code,
        }

    @staticmethod
    def _result(payload: dict[str, Any]) -> dict[str, Any]:
        reward = payload.get("reward")
        already_checked_in = payload.get("already_checked_in")
        if not isinstance(reward, dict) or not isinstance(already_checked_in, bool):
            log.warning("thinkbuddy-website returned an invalid check-in response")
            return _invalid_response()
        points = reward.get("points_awarded")
        grant_status = reward.get("grant_status")
        if (
            not isinstance(points, int)
            or isinstance(points, bool)
            or points <= 0
            or grant_status not in _GRANT_MESSAGES
        ):
            log.warning("thinkbuddy-website returned an invalid reward response")
            return _invalid_response()
        return {
            "ok": True,
            "already_checked_in": already_checked_in,
            "points_awarded": points,
            "grant_status": grant_status,
        }


def _invalid_response() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "invalid_response",
        "message": "签到服务返回异常，请稍后重试",
    }


def checkin_message(result: dict[str, Any]) -> str:
    """Build the user-facing result text, including asynchronous grant state."""
    if not result.get("ok"):
        return str(result.get("message") or "签到失败，请稍后重试")
    suffix = _GRANT_MESSAGES.get(str(result.get("grant_status") or ""), "")
    if result.get("already_checked_in"):
        return f"今日已签到，本次未重复增加积分{suffix}"
    points = int(result.get("points_awarded") or 0)
    return f"签到成功，获得 {points} 积分{suffix}"
