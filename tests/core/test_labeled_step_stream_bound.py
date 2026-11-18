"""The labeled-step path shares the configurable stream bound.

``deep_question`` runs through ``run_labeled_step``, not the chat answer loop,
so this path carried its own 90s literal long after the chat loop's bound moved
to ``capabilities.chat.streaming.idle_timeout_seconds``. These tests pin the
four things that can silently go wrong in that hand-over:

1. the resolved value comes from main.yaml, not from a literal;
2. the module seam a test sets still wins (otherwise every timeout test here
   would block for the configured minutes);
3. the *connect* wait stays capped below the idle bound;
4. the raised error is still a retryable transport error, so the existing retry
   paths keep working.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import textwrap
from typing import Any

import pytest

from deeptutor.runtime.agentic import stream_bounds
import deeptutor.runtime.agentic.labeled_step as labeled_step_module
from deeptutor.runtime.agentic.usage import UsageTracker
from deeptutor.runtime.stream_bus import StreamBus
from deeptutor.services.llm import LLMProviderTransportError
from deeptutor.services.llm.exceptions import LLMStreamIdleTimeout
from tests.core.test_labeled_step_finish_usage import (
    _chunk,
    _Client,
    _run_step,
    _Stream,
)


@pytest.fixture
def isolated_main_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``ConfigManager`` at a throwaway ``main.yaml`` for one test."""
    from deeptutor.utils import config_manager as config_manager_mod

    settings_dir = tmp_path / "data" / "user" / "settings"
    settings_dir.mkdir(parents=True)
    monkeypatch.setattr(
        config_manager_mod, "get_runtime_settings_dir", lambda *a, **k: settings_dir
    )
    monkeypatch.setattr(config_manager_mod.ConfigManager, "_instance", None)
    return settings_dir / "main.yaml"


def _write_idle(file_path: Path, seconds: Any) -> None:
    file_path.write_text(
        textwrap.dedent(
            f"""
            capabilities:
              chat:
                streaming:
                  idle_timeout_seconds: {seconds}
            """
        ),
        encoding="utf-8",
    )


# ---- (1) configured value, not a hardcoded literal ----------------------


def test_labeled_step_resolves_the_bound_from_main_yaml(
    monkeypatch: pytest.MonkeyPatch, isolated_main_yaml: Path
) -> None:
    monkeypatch.setattr(labeled_step_module, "_STREAM_IDLE_TIMEOUT_S", None)
    _write_idle(isolated_main_yaml, 900)

    resolved = labeled_step_module._stream_idle_timeout_s()

    assert resolved == 900.0
    # The old literal must be gone: 90s was the value that turned a slow
    # reasoning prefill into a dead step.
    assert resolved != 90.0


def test_labeled_step_falls_back_to_the_shipped_default(
    monkeypatch: pytest.MonkeyPatch, isolated_main_yaml: Path
) -> None:
    monkeypatch.setattr(labeled_step_module, "_STREAM_IDLE_TIMEOUT_S", None)

    assert labeled_step_module._stream_idle_timeout_s() == (
        stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    )


def test_labeled_step_no_longer_carries_a_ninety_second_constant() -> None:
    assert labeled_step_module._STREAM_IDLE_TIMEOUT_S is None


# ---- (2) the test seam still wins ---------------------------------------


def test_labeled_step_seam_overrides_the_configuration(
    monkeypatch: pytest.MonkeyPatch, isolated_main_yaml: Path
) -> None:
    _write_idle(isolated_main_yaml, 900)
    monkeypatch.setattr(labeled_step_module, "_STREAM_IDLE_TIMEOUT_S", 0.1)

    assert labeled_step_module._stream_idle_timeout_s() == pytest.approx(0.1)


# ---- (3) connect wait is capped below the idle bound --------------------


@pytest.mark.asyncio
async def test_labeled_step_connect_wait_receives_the_idle_bound_not_a_literal(
    monkeypatch: pytest.MonkeyPatch, isolated_main_yaml: Path
) -> None:
    """A 600s idle configuration must not make a dead endpoint wait 600s."""
    _write_idle(isolated_main_yaml, 600)
    monkeypatch.setattr(labeled_step_module, "_STREAM_IDLE_TIMEOUT_S", None)

    seen: list[float] = []
    real = labeled_step_module.connect_timeout_seconds

    def _recording(value: float) -> float:
        seen.append(float(value))
        # Keep the test fast while still exercising the real wiring.
        return min(real(float(value)), 0.05)

    monkeypatch.setattr(labeled_step_module, "connect_timeout_seconds", _recording)

    class HangingCreate(_Client):
        def __init__(self) -> None:
            super().__init__([])

            async def create(**_kwargs: Any) -> _Stream:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            self.chat.completions.create = create  # type: ignore[method-assign]

    with pytest.raises(LLMProviderTransportError, match="connection timed out"):
        await asyncio.wait_for(
            _run_step(HangingCreate(), UsageTracker(model="gpt-test")), timeout=2.0
        )

    # The idle bound (600) is what was handed in; the cap is applied inside.
    assert seen == [600.0]
    assert real(600) == stream_bounds.MAX_STREAM_CONNECT_TIMEOUT_SECONDS


# ---- (4) the error stays a retryable transport error -------------------


def test_stream_idle_timeout_is_a_retryable_transport_error() -> None:
    error = LLMStreamIdleTimeout(12.0)
    assert isinstance(error, LLMProviderTransportError)
    assert error.retryable is True
    assert error.error_code == "provider_stream_idle"
    assert error.idle_seconds == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_stalled_labeled_step_raises_the_retryable_idle_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(labeled_step_module, "_STREAM_IDLE_TIMEOUT_S", 0.05)
    stall = _Stream([_chunk(content="``THINK``\n")], stall_after_chunks=True)

    with pytest.raises(LLMStreamIdleTimeout) as raised:
        await asyncio.wait_for(
            _run_step(_Client([stall]), UsageTracker(model="gpt-test")), timeout=2.0
        )

    assert raised.value.retryable is True
    assert raised.value.idle_seconds == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_labeled_step_completes_inside_the_configured_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary streaming must not be touched by the raised bound."""
    monkeypatch.setattr(labeled_step_module, "_STREAM_IDLE_TIMEOUT_S", 0.3)
    stream = _Stream([_chunk(content="``FINISH``\nDone.", finish_reason="stop")])

    result = await asyncio.wait_for(
        _run_step(_Client([stream]), UsageTracker(model="gpt-test")), timeout=2.0
    )

    assert result.label == "FINISH"
    assert result.text == "Done."
    assert stream.closed
