"""Per-account persona usage counters: tolerant load, atomic bump, safe writes."""

from __future__ import annotations

import json

import pytest

from deeptutor.multi_user.models import CurrentUser, UserScope
from deeptutor.multi_user.paths import get_account_path_service, user_context
from deeptutor.services.persona.usage import (
    USAGE_FILE,
    bump_persona_usage,
    load_persona_usage,
)


@pytest.fixture
def usage_path(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEEPTUTOR_WORKSPACE_ROOT", raising=False)
    user = CurrentUser(
        "usage-test", "test", "admin", UserScope("admin", "usage-test", tmp_path)
    )
    with user_context(user):
        yield get_account_path_service().get_settings_file(USAGE_FILE)


def test_load_missing_file_is_empty(usage_path) -> None:
    assert load_persona_usage() == {}


def test_bump_increments_and_persists(usage_path) -> None:
    assert bump_persona_usage("teacher") == 1
    assert bump_persona_usage("teacher") == 2
    assert bump_persona_usage("peer") == 1
    assert load_persona_usage() == {"teacher": 2, "peer": 1}
    # The file landed in the account's settings dir as a real JSON document.
    payload = json.loads(usage_path.read_text(encoding="utf-8"))
    assert payload == {"teacher": 2, "peer": 1}


def test_corrupt_file_falls_back_to_empty(usage_path) -> None:
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text("{not json", encoding="utf-8")
    assert load_persona_usage() == {}
    # A bump repairs the file instead of crashing the ranking.
    assert bump_persona_usage("coach") == 1


def test_invalid_payload_is_ignored(usage_path) -> None:
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_persona_usage() == {}
    usage_path.write_text(
        json.dumps(
            {
                "good": 3,
                "zero": 0,  # never a real pick
                "negative": -1,
                "float": 2.5,
            }
        ),
        encoding="utf-8",
    )
    assert load_persona_usage() == {"good": 3}

    usage_path.write_text(json.dumps({"also_nested": {"inner": 1}}), encoding="utf-8")
    assert load_persona_usage() == {}
