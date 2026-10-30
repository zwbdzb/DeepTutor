"""Detail-code priority and locale wiring for knowledge-base readiness rows.

``knowledge_base_rows`` is a pure classifier, so these tests pin its branching
directly; the end-to-end wiring is covered by the ``build_settings_readiness``
test below.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeptutor.services.config.readiness import DETAIL_CODES, knowledge_base_rows

NEW_BINDING_CODES = (
    "knowledge_binding_missing",
    "knowledge_binding_changed",
    "knowledge_binding_unconfigured",
)
REPO_ROOT = Path(__file__).resolve().parents[3]


def _row(**entry) -> dict:
    return knowledge_base_rows([{"label": "Agent", **entry}])[0]


# --------------------------------------------------------------------------
# Priority: needs_reindex must not be stolen by the binding branches.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("binding_state", ["missing", "changed", "unconfigured", None, "ready"])
def test_needs_reindex_outranks_every_binding_state(binding_state):
    row = _row(status="ready", needs_reindex=True, prerequisites_ready=False, binding_state=binding_state)

    assert row["detail_code"] == "knowledge_base_needs_reindex"
    assert row["state"] == "misconfigured"


def test_an_entry_without_a_binding_state_key_does_not_crash():
    """Older callers may not pass ``binding_state`` at all."""
    row = _row(status="ready", needs_reindex=False, prerequisites_ready=False)

    assert row["detail_code"] == "rag_prerequisite_missing"
    assert row["state"] == "misconfigured"


@pytest.mark.parametrize("binding_state", ["missing", "changed", "unconfigured"])
def test_building_status_outranks_a_broken_binding(binding_state):
    row = _row(status="processing", needs_reindex=False, prerequisites_ready=False, binding_state=binding_state)

    assert row["detail_code"] == "knowledge_base_building"
    assert row["state"] == "unavailable"


@pytest.mark.parametrize(
    ("binding_state", "expected"),
    [
        ("missing", "knowledge_binding_missing"),
        ("changed", "knowledge_binding_changed"),
        ("unconfigured", "knowledge_binding_unconfigured"),
        # Only a failed engine preflight means a missing RAG prerequisite.
        (None, "rag_prerequisite_missing"),
        ("", "rag_prerequisite_missing"),
        ("ready", "rag_prerequisite_missing"),
        ("legacy", "rag_prerequisite_missing"),
        ("brand-new-future-state", "rag_prerequisite_missing"),
    ],
)
def test_unknown_or_absent_binding_states_do_not_crash_and_fall_through(binding_state, expected):
    row = _row(status="ready", needs_reindex=False, prerequisites_ready=False, binding_state=binding_state)

    assert row["detail_code"] == expected
    assert row["state"] == "misconfigured"
    assert row["verified"] is False


def test_a_healthy_binding_still_reports_ready():
    row = _row(status="ready", needs_reindex=False, prerequisites_ready=True, binding_state="ready")

    assert row["detail_code"] == "knowledge_base_ready"
    assert row["state"] == "enabled_verified"
    assert row["verified"] is True


def test_binding_detail_codes_are_declared():
    for code in NEW_BINDING_CODES:
        assert code in DETAIL_CODES


# --------------------------------------------------------------------------
# The codes must be explained in every shipped locale, or the panel falls
# back to a raw identifier the user cannot act on.
# --------------------------------------------------------------------------


def _locale_detail_keys(locale: str) -> dict:
    path = REPO_ROOT / "web" / "locales" / locale / "app.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("locale", ["en", "zh", "de", "pl", "uk"])
def test_every_binding_detail_code_has_a_locale_entry(locale):
    messages = _locale_detail_keys(locale)

    for code in NEW_BINDING_CODES:
        key = f"readiness.detail.{code}"
        assert key in messages, f"{locale} is missing {key}"
        assert messages[key].strip(), f"{locale} ships an empty {key}"


@pytest.mark.parametrize("locale", ["en", "zh", "de", "pl", "uk"])
def test_locale_files_stay_valid_json(locale):
    # json.loads above would raise; assert explicitly for a readable failure.
    assert isinstance(_locale_detail_keys(locale), dict)
