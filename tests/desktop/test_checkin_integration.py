from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

DESKTOP_SHELL = Path(__file__).resolve().parents[2] / "desktop-shell"
sys.path.insert(0, str(DESKTOP_SHELL))


from desktop.checkin import CheckinCoordinator, checkin_message
from desktop.thinkbuddy_website import ThinkBuddyWebsiteError
from desktop.titlebar_account import MENU_ACTIONS, account_menu_model


class StubAuth:
    def __init__(self, provider_user_id: str = "u_314") -> None:
        self.user_id = provider_user_id

    def provider_user_id(self) -> str:
        return self.user_id


def test_coordinator_uses_oauth_subject_and_configured_website() -> None:
    coordinator = CheckinCoordinator(StubAuth(), "https://website.example.com/")
    response = {
        "reward": {"points_awarded": 100, "grant_status": "pending"},
        "already_checked_in": False,
    }
    with patch("desktop.checkin.request_website_checkin", return_value=response) as request:
        result = coordinator.checkin()

    request.assert_called_once_with("https://website.example.com", "u_314")
    assert result == {
        "ok": True,
        "already_checked_in": False,
        "points_awarded": 100,
        "grant_status": "pending",
    }


def test_coordinator_requires_oauth_subject() -> None:
    coordinator = CheckinCoordinator(StubAuth(""), "https://website.example.com")
    with patch("desktop.checkin.request_website_checkin") as request:
        result = coordinator.checkin()

    request.assert_not_called()
    assert result["error"] == "missing_provider_user_id"


def test_coordinator_rejects_concurrent_click() -> None:
    coordinator = CheckinCoordinator(StubAuth(), "https://website.example.com")
    request_started = threading.Event()
    release_request = threading.Event()
    first_result: list[dict[str, object]] = []

    def blocking_request(_url: str, _user_id: str) -> dict[str, object]:
        request_started.set()
        release_request.wait(timeout=2)
        return {
            "reward": {"points_awarded": 100, "grant_status": "pending"},
            "already_checked_in": False,
        }

    with patch("desktop.checkin.request_website_checkin", side_effect=blocking_request):
        first = threading.Thread(target=lambda: first_result.append(coordinator.checkin()))
        first.start()
        assert request_started.wait(timeout=1)
        second_result = coordinator.checkin()
        release_request.set()
        first.join(timeout=2)

    assert not first.is_alive()
    assert first_result[0]["ok"] is True
    assert second_result["error"] == "in_progress"


@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (ThinkBuddyWebsiteError("checkin_disabled", 409), "签到功能暂未开放"),
        (ThinkBuddyWebsiteError("network unavailable"), "签到服务连接失败，请稍后重试"),
        (ThinkBuddyWebsiteError("server failure", 500), "签到服务暂不可用，请稍后重试"),
    ],
)
def test_coordinator_maps_service_errors(
    error: ThinkBuddyWebsiteError,
    expected_message: str,
) -> None:
    coordinator = CheckinCoordinator(StubAuth(), "https://website.example.com")
    with patch("desktop.checkin.request_website_checkin", side_effect=error):
        result = coordinator.checkin()

    assert result["ok"] is False
    assert result["message"] == expected_message
    assert result["status_code"] == error.status_code


def test_coordinator_rejects_invalid_service_response() -> None:
    coordinator = CheckinCoordinator(StubAuth(), "https://website.example.com")
    with patch(
        "desktop.checkin.request_website_checkin",
        return_value={"reward": {}, "already_checked_in": False},
    ):
        result = coordinator.checkin()

    assert result["error"] == "invalid_response"


def test_logged_in_menu_exposes_checkin_action() -> None:
    menu = account_menu_model(
        {
            "logged_in": True,
            "configured": False,
            "account": {"username": "learner", "models": []},
        }
    )

    actions = [item.get("action") for item in menu["items"]]
    assert "checkin" in MENU_ACTIONS
    assert "checkin" in actions


def test_checkin_message_reports_async_grant_status() -> None:
    result = {
        "ok": True,
        "already_checked_in": False,
        "points_awarded": 100,
        "grant_status": "pending",
    }

    assert checkin_message(result) == "签到成功，获得 100 积分，额度发放中"


@pytest.mark.parametrize(
    ("grant_status", "suffix"),
    [
        ("pending", "，额度发放中"),
        ("processing", "，额度发放中"),
        ("succeeded", "，额度已到账"),
        ("failed", "，额度发放失败"),
    ],
)
def test_duplicate_checkin_message_keeps_grant_status(
    grant_status: str,
    suffix: str,
) -> None:
    result = {
        "ok": True,
        "already_checked_in": True,
        "points_awarded": 100,
        "grant_status": grant_status,
    }

    assert checkin_message(result) == f"今日已签到，本次未重复增加积分{suffix}"
