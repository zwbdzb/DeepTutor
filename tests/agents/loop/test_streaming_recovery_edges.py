"""Independent edge-case probes for the stream-stall / budget-escalation fix.

Written by QA to falsify the five claims about the change, not to restate them.
Every case here is a boundary the fix has to actually hold at: a zero or
negative timeout, a settings file that cannot be parsed, a budget that is asked
to escalate repeatedly, and a stall that happens *after* reasoning was already
streamed (where a replay would duplicate visible prose).
"""

from __future__ import annotations

from pathlib import Path
import textwrap
from typing import Any

import pytest

from deeptutor.agents.loop import agent_loop as agent_loop_mod
from deeptutor.agents.loop.budget_escalation import (
    DEFAULT_ESCALATION_CEILING,
    MIN_REASONING_CHARS_FOR_ESCALATION,
    ReasoningBudgetEscalation,
    reasoning_dominated_output,
)
from deeptutor.core.context import UnifiedContext
from deeptutor.runtime.agentic import stream_bounds
from tests.agents.chat.test_agent_loop import (
    _answer_text,
    _llm_chunk,
    _run,
    _ScriptedChatClient,
)
from tests.agents.loop.test_streaming_recovery import _pipeline, _StallingClient


def _streaming_config(**streaming: Any) -> dict[str, Any]:
    return {"capabilities": {"chat": {"streaming": dict(streaming)}}}


# ---- Claim 1: the idle bound is configurable, clamped and never fatal -----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A realistic operator value passes through untouched.
        (600, 600.0),
        (300, 300.0),
        # The documented clamp window, at both ends.
        (1, stream_bounds.MIN_STREAM_IDLE_TIMEOUT_SECONDS),
        (15, stream_bounds.MIN_STREAM_IDLE_TIMEOUT_SECONDS),
        (3_600, stream_bounds.MAX_STREAM_IDLE_TIMEOUT_SECONDS),
        (99_999, stream_bounds.MAX_STREAM_IDLE_TIMEOUT_SECONDS),
        # A numeric string is still a number (lenient read, not a failure).
        ("600", 600.0),
        # Junk falls back to the shipped value instead of failing the turn.
        ("soon", stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS),
        (None, stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS),
        # Zero and negative would mean "time out immediately" if passed through,
        # which would kill *every* turn; they must not do that.
        (0, stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS),
        (-5, stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS),
        (-0.5, stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS),
    ],
)
def test_idle_bound_edge_values_never_produce_a_hostile_timeout(
    raw: Any, expected: float
) -> None:
    resolved = stream_bounds.stream_idle_timeout_seconds(
        _streaming_config(idle_timeout_seconds=raw)
    )
    assert resolved == expected
    # Whatever the input, the resolved bound must be usable: finite and large
    # enough that a model can actually answer inside it.
    assert resolved >= stream_bounds.MIN_STREAM_IDLE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"capabilities": None},
        {"capabilities": "chat"},
        {"capabilities": {"chat": {"streaming": "300"}}},
        {"capabilities": {"chat": {"streaming": [300]}}},
    ],
)
def test_unusable_streaming_section_falls_back_without_raising(
    config: dict[str, Any] | None,
) -> None:
    assert (
        stream_bounds.stream_idle_timeout_seconds(config)
        == stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize("idle", [0, -1, None, "soon"])
def test_connect_wait_never_becomes_degenerate(idle: Any) -> None:
    assert stream_bounds.connect_timeout_seconds(idle) == (
        stream_bounds.DEFAULT_STREAM_CONNECT_TIMEOUT_SECONDS
    )


def test_connect_wait_is_capped_below_the_idle_bound(
) -> None:
    # A raised idle bound must not extend the header wait past its own ceiling.
    assert stream_bounds.connect_timeout_seconds(3_600) == (
        stream_bounds.MAX_STREAM_CONNECT_TIMEOUT_SECONDS
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
    # The manager is a process-wide singleton; rebuild it against tmp_path.
    monkeypatch.setattr(config_manager_mod.ConfigManager, "_instance", None)
    return settings_dir / "main.yaml"


def test_idle_bound_is_read_from_the_real_settings_file(isolated_main_yaml: Path) -> None:
    isolated_main_yaml.write_text(
        textwrap.dedent(
            """
            capabilities:
              chat:
                streaming:
                  idle_timeout_seconds: 900
            """
        ),
        encoding="utf-8",
    )
    assert stream_bounds.configured_stream_idle_timeout_seconds() == 900.0


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Unparseable YAML: a broken settings file must not fail a turn.
        ("capabilities: [unclosed\n", stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS),
        # Absent key.
        ("capabilities:\n  chat: {}\n", stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS),
        # Zero on disk is still not a zero-second bound.
        (
            "capabilities:\n  chat:\n    streaming:\n      idle_timeout_seconds: 0\n",
            stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
        ),
    ],
)
def test_broken_or_silent_settings_file_degrades_to_the_default(
    isolated_main_yaml: Path, body: str, expected: float
) -> None:
    isolated_main_yaml.write_text(body, encoding="utf-8")
    # No exception: the bound is a safety net, not a startup precondition.
    assert stream_bounds.configured_stream_idle_timeout_seconds() == expected


def test_missing_settings_file_degrades_to_the_default(
    isolated_main_yaml: Path,
) -> None:
    assert not isolated_main_yaml.exists()
    assert (
        stream_bounds.configured_stream_idle_timeout_seconds()
        == stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    )


def test_a_raising_settings_stack_still_yields_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.utils import config_manager as config_manager_mod

    class _Exploding:
        def load_config(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("settings unavailable")

    monkeypatch.setattr(config_manager_mod, "ConfigManager", _Exploding)
    assert (
        stream_bounds.configured_stream_idle_timeout_seconds()
        == stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    )


# ---- Claim 4: the escalation is bounded, capped and off by no more than one -


def test_repeated_escalation_converges_on_the_ceiling_instead_of_compounding() -> None:
    policy = ReasoningBudgetEscalation()
    budgets = [8_000]
    for _ in range(8):
        budgets.append(policy.next_budget(budgets[-1]))

    # Monotonic, and every step is bounded by the ceiling: no runaway growth.
    assert budgets == sorted(budgets)
    assert max(budgets) == DEFAULT_ESCALATION_CEILING
    assert all(budget <= DEFAULT_ESCALATION_CEILING for budget in budgets)
    # Once at the ceiling the policy reports "no escalation left" by returning
    # the input unchanged, so a caller can tell the two apart.
    assert policy.next_budget(DEFAULT_ESCALATION_CEILING) == DEFAULT_ESCALATION_CEILING
    # ... and a single step from a budget already above the ceiling cannot
    # shrink the round.
    assert policy.next_budget(40_000) == 40_000


def test_disabled_escalation_never_touches_the_budget() -> None:
    policy = ReasoningBudgetEscalation(enabled=False)
    assert policy.next_budget(8_000) == 8_000


def test_escalation_ceiling_cannot_exceed_the_provider_limit() -> None:
    # ``next_budget`` re-applies MAX_OUTPUT_TOKENS even if a policy was built
    # by hand with a larger ceiling than the request layer would accept.
    policy = ReasoningBudgetEscalation(multiplier=8.0, ceiling=1_000_000)
    assert policy.next_budget(50_000) == 131_072


def test_reasoning_dominance_character_floor_is_inclusive() -> None:
    # Exactly the documented floor counts; one character short does not.
    assert reasoning_dominated_output(
        max_tokens=8_000,
        reasoning_chars=MIN_REASONING_CHARS_FOR_ESCALATION,
        content_chars=0,
    )
    assert not reasoning_dominated_output(
        max_tokens=8_000,
        reasoning_chars=MIN_REASONING_CHARS_FOR_ESCALATION - 1,
        content_chars=0,
    )


def test_character_floor_is_applied_before_the_tool_call_ratio() -> None:
    """The 2000-char floor is a hard gate *ahead* of the share test.

    A round that streamed a huge answer and barely any reasoning must not clear
    the lowered tool-call bar just because the denominator is large: 1999
    reasoning characters is below the floor, so no ratio can rescue it.
    """
    assert not reasoning_dominated_output(
        max_tokens=8_000,
        reasoning_chars=MIN_REASONING_CHARS_FOR_ESCALATION - 1,
        content_chars=99_999,
        tool_call_truncated=True,
    )
    # At the floor with almost no visible text, the tool-call bar (25% of the
    # streamed text) is trivially met — the floor is the only thing that could
    # have rejected it, and it does not.
    assert reasoning_dominated_output(
        max_tokens=8_000,
        reasoning_chars=MIN_REASONING_CHARS_FOR_ESCALATION,
        content_chars=0,
        tool_call_truncated=True,
    )


def test_reasoning_dominance_reaching_the_cap_is_still_judged_by_share() -> None:
    # completion_tokens == max_tokens means the cap *was* reached, so the share
    # test applies rather than being short-circuited away.
    assert reasoning_dominated_output(
        max_tokens=8_000,
        reasoning_chars=7_000,
        content_chars=1_000,
        completion_tokens=8_000,
    )
    assert not reasoning_dominated_output(
        max_tokens=8_000,
        reasoning_chars=2_000,
        content_chars=6_000,
        completion_tokens=8_000,
    )


def test_tool_call_truncation_lowers_the_reasoning_share_it_demands() -> None:
    # 2000 reasoning tokens (25%) is enough when the round was writing a tool
    # call, because the argument payload also has to fit — but not otherwise.
    assert reasoning_dominated_output(
        max_tokens=8_000, reasoning_tokens=2_000, tool_call_truncated=True
    )
    assert not reasoning_dominated_output(max_tokens=8_000, reasoning_tokens=2_000)


def test_tool_call_truncation_lowers_the_bar_in_the_character_fallback() -> None:
    # 2500 of 4000 streamed characters is 62.5%: dominant by the ordinary
    # half-of-the-text test, and a cut-off tool call needs *less* than that,
    # not more.
    assert reasoning_dominated_output(
        max_tokens=8_000,
        reasoning_chars=2_500,
        content_chars=1_500,
        tool_call_truncated=True,
    )


def test_tool_call_truncation_direction_is_distinguishable() -> None:
    """The regression guard for the inverted ratio.

    At a third of the streamed text the two readings disagree, so this case
    fails if ``int(streamed * (1.0 - ratio))`` ever comes back: a truncated
    tool-call round is exactly the round whose budget the argument payload also
    eats, so reasoning at 33% must count as dominant there and not in an
    ordinary round. ``reasoning_chars`` stays above the 2000-char floor, since
    below that no reading is meaningful.
    """
    reasoning_chars, content_chars = 2_000, 4_000  # ~33% reasoning

    assert reasoning_dominated_output(
        max_tokens=8_000,
        reasoning_chars=reasoning_chars,
        content_chars=content_chars,
        tool_call_truncated=True,
    )
    assert not reasoning_dominated_output(
        max_tokens=8_000,
        reasoning_chars=reasoning_chars,
        content_chars=content_chars,
        tool_call_truncated=False,
    )


def test_a_non_positive_round_budget_is_never_escalated() -> None:
    assert not reasoning_dominated_output(max_tokens=0, reasoning_tokens=9_999)
    assert not reasoning_dominated_output(max_tokens=-1, reasoning_chars=50_000)


# ---- Claim 3: a stall after streamed output must not duplicate prose ------


@pytest.mark.asyncio
async def test_stall_after_reasoning_and_partial_text_does_not_replay_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dangerous shape: thinking already shown, half an answer already shown.

    A replay would render "First half " twice. The continuation asks for the
    rest only, so the reader sees each character once.
    """
    client = _StallingClient(
        [
            [
                _llm_chunk(reasoning_content="private plan: step one", content="First half "),
            ],
            [_llm_chunk(content="second half.", finish_reason="stop")],
        ]
    )
    pipeline = _pipeline(monkeypatch, client)
    monkeypatch.setattr(agent_loop_mod, "_STREAM_IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(agent_loop_mod, "_PROVIDER_RETRY_DELAYS", (0, 0))

    events = await _run(pipeline, UnifiedContext(session_id="s1", user_message="Explain"))

    assert client.call_count == 2
    answer_text = _answer_text(events)
    assert answer_text == "First half second half."
    # Published exactly once, not once per round.
    assert answer_text.count("First half") == 1
    # Hidden reasoning must not leak into the visible answer on either round.
    assert "private plan" not in answer_text
    # The continuation knows what the previous round already said.
    prior_assistant = [
        message
        for message in client.calls[1]["messages"]
        if message.get("role") == "assistant"
    ]
    assert any(
        message.get("content") == "First half " for message in prior_assistant
    )
    # And it was told *why* it is continuing.
    follow_up = str(client.calls[1]["messages"][-1]["content"])
    assert "stopped" in follow_up.lower()


# ---- Claim 5: a truncated tool call never reaches the dispatcher ---------


@pytest.mark.asyncio
async def test_truncated_tool_call_publishes_no_argument_guard_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-visible symptom was a bare "Rejected exec ... missing args" row."""
    client = _ScriptedChatClient(
        [
            [
                _llm_chunk(
                    content="Running it.",
                    tool_calls=[{"id": "call_1", "name": "web_search", "arguments": '{"que'}],
                    finish_reason="length",
                ),
                _llm_chunk(
                    tool_calls=[], usage=None, finish_reason=None
                ),
            ],
            [_llm_chunk(content="Answer", finish_reason="stop")],
        ]
    )
    pipeline = _pipeline(monkeypatch, client)
    events = await _run(
        pipeline,
        UnifiedContext(session_id="s1", user_message="Search", enabled_tools=["web_search"]),
    )

    assert pipeline.registry.executed == []
    rendered = " ".join(str(event.content) for event in events)
    assert "Rejected" not in rendered
    assert "missing required args" not in rendered
    assert not any(
        event.metadata.get("trace_kind") in {"tool_call", "tool_result"} for event in events
    )


# ---- sanity: the loop-level bound is the one that is actually applied ----


def test_agent_loop_resolves_the_configured_bound_not_a_hardcoded_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_loop_mod, "_STREAM_IDLE_TIMEOUT_SECONDS", None)
    monkeypatch.setattr(
        agent_loop_mod,
        "configured_stream_idle_timeout_seconds",
        lambda: 4321.0,
    )
    assert agent_loop_mod._stream_idle_timeout_seconds() == 4321.0
    # The test seam still wins, because tests need a millisecond bound.
    monkeypatch.setattr(agent_loop_mod, "_STREAM_IDLE_TIMEOUT_SECONDS", 0.05)
    assert agent_loop_mod._stream_idle_timeout_seconds() == 0.05
