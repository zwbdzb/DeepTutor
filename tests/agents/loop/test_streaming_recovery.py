"""Recovery paths for a round the provider cut off mid-output.

Three defects share one mechanism: the loop treated "the provider stopped before
it finished" as if the round had finished, or as if the turn were dead.

* a round whose whole budget went to hidden reasoning was retried at the same
  budget, which fails the same way, and then ended the turn;
* a tool call cut off by that budget was dispatched, so a whole round was spent
  on an argument-guard rejection the model never saw;
* a provider that went silent mid-answer killed the turn, even when half an
  answer was already on the reader's screen.

These tests pin the behaviour on the other side of each fix.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeptutor.agents.chat.agentic_pipeline import AgenticChatPipeline
from deeptutor.agents.loop import agent_loop as agent_loop_mod
from deeptutor.agents.loop import pipeline as pipeline_module
from deeptutor.agents.loop.budget_escalation import (
    ReasoningBudgetEscalation,
    reasoning_dominated_output,
)
from deeptutor.core.context import UnifiedContext
from deeptutor.runtime.agentic import stream_bounds
from deeptutor.runtime.stream_bus import StreamBus
from deeptutor.services.llm import LLMProviderTransportError, LLMReasoningBudgetExhausted
from tests.agents.chat.test_agent_loop import (
    _answer_text,
    _collect_bus_events,
    _llm_chunk,
    _Registry,
    _run,
    _ScriptedChatClient,
    _usage_only_chunk,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run the loop in an initialised install, independent of developer data."""
    from deeptutor.capabilities.setup.binding import mark_intro_shown
    from deeptutor.services.path_service import PathService

    monkeypatch.setenv("DEEPTUTOR_HOME", str(tmp_path))
    paths = PathService(workspace_root=tmp_path / "data")
    monkeypatch.setattr(PathService, "_instance", paths)
    paths.ensure_all_directories()
    mark_intro_shown()
    monkeypatch.setattr(
        "deeptutor.agents.loop.pipeline.get_llm_config",
        lambda: SimpleNamespace(
            binding="openai",
            model="gpt-test",
            api_key="k",
            base_url="u",
            api_version=None,
            extra_headers={},
            reasoning_effort=None,
        ),
    )


def _round_budgets(events: list[Any]) -> list[int]:
    """The ``max_tokens`` each round actually requested, in order."""
    return [
        int(event.metadata["requested_max_tokens"])
        for event in events
        if event.metadata.get("trace_kind") == "call_status"
        and event.metadata.get("call_state") == "complete"
        and "requested_max_tokens" in event.metadata
    ]


def _warnings(events: list[Any]) -> list[str]:
    return [
        str(event.content) for event in events if event.metadata.get("trace_kind") == "warning"
    ]


def _recovery_events(events: list[Any]) -> list[Any]:
    return [event for event in events if event.metadata.get("recovery_kind") == "reasoning_budget"]


def _pipeline(monkeypatch: pytest.MonkeyPatch, client: Any) -> AgenticChatPipeline:
    pipeline = AgenticChatPipeline(language="en")
    pipeline.registry = _Registry()
    monkeypatch.setattr(pipeline, "_compose_enabled_tools", lambda _context: ["web_search"])
    monkeypatch.setattr(pipeline, "_build_openai_client", lambda: client)
    return pipeline


# ---- A. reasoning-aware budget escalation --------------------------------


@pytest.mark.asyncio
async def test_reasoning_exhausted_round_raises_the_retry_budget_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default ``prompt`` policy must still escalate: same cap repeats the failure."""
    client = _ScriptedChatClient(
        [
            [
                _llm_chunk(reasoning_content="private plan", finish_reason="length"),
                _usage_only_chunk(prompt=100, completion=8000, total=8100),
            ],
            [_llm_chunk(content="Answer", finish_reason="stop")],
        ]
    )
    pipeline = _pipeline(monkeypatch, client)
    events = await _run(
        pipeline, UnifiedContext(session_id="s1", user_message="Explain it", enabled_tools=[])
    )

    base = pipeline.loop_max_tokens
    assert _round_budgets(events) == [base, base * 2]
    recovery = _recovery_events(events)
    assert len(recovery) == 1
    assert recovery[0].metadata["escalation"] == "automatic"
    assert recovery[0].metadata["previous_max_tokens"] == base
    assert recovery[0].metadata["next_max_tokens"] == base * 2
    # The notice is about cost, so it must not carry reasoning text.
    assert "private plan" not in str(recovery[0].content)
    assert _answer_text(events) == "Answer"


@pytest.mark.asyncio
async def test_escalation_is_bounded_to_one_round_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that reasons past every bound must not make the budget compound."""

    def _exhausted_round() -> list[Any]:
        return [
            _llm_chunk(reasoning_content="private", finish_reason="length"),
            _usage_only_chunk(prompt=100, completion=8000, total=8100),
        ]

    client = _ScriptedChatClient([_exhausted_round(), _exhausted_round()])
    pipeline = _pipeline(monkeypatch, client)
    with pytest.raises(LLMReasoningBudgetExhausted) as raised:
        await pipeline.run(
            UnifiedContext(session_id="s1", user_message="Explain it"), StreamBus()
        )

    base = pipeline.loop_max_tokens
    assert client.call_count == 2
    assert raised.value.diagnostics["requested_max_tokens"] == base * 2


@pytest.mark.asyncio
async def test_truncated_answer_without_reasoning_keeps_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long answer is not a starved one: verbosity must not buy more budget."""
    long_text = "word " * 400
    client = _ScriptedChatClient(
        [
            [_llm_chunk(content=long_text, finish_reason="length")],
            [_llm_chunk(content=" finished", finish_reason="stop")],
        ]
    )
    pipeline = _pipeline(monkeypatch, client)
    events = await _run(pipeline, UnifiedContext(session_id="s1", user_message="Write a lot"))

    base = pipeline.loop_max_tokens
    assert _round_budgets(events) == [base, base]
    assert _recovery_events(events) == []
    assert any("token limit" in text for text in _warnings(events))


@pytest.mark.asyncio
async def test_escalation_respects_its_configured_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline_module,
        "get_chat_params",
        lambda: {
            "responding": {"max_tokens": 1024},
            "reasoning_budget_escalation": {"enabled": True, "multiplier": 4.0, "ceiling": 2500},
        },
    )
    client = _ScriptedChatClient(
        [
            [
                _llm_chunk(reasoning_content="private", finish_reason="length"),
                _usage_only_chunk(prompt=100, completion=1024, total=1124),
            ],
            [_llm_chunk(content="Answer", finish_reason="stop")],
        ]
    )
    pipeline = _pipeline(monkeypatch, client)
    events = await _run(pipeline, UnifiedContext(session_id="s1", user_message="Go"))

    # 1024 * 4 would be 4096; the ceiling wins.
    assert _round_budgets(events) == [1024, 2500]


@pytest.mark.asyncio
async def test_disabled_escalation_retries_at_the_same_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline_module,
        "get_chat_params",
        lambda: {
            "responding": {"max_tokens": 1024},
            "reasoning_budget_escalation": {"enabled": False},
        },
    )
    client = _ScriptedChatClient(
        [
            [
                _llm_chunk(reasoning_content="private", finish_reason="length"),
                _usage_only_chunk(prompt=100, completion=1024, total=1124),
            ],
            [_llm_chunk(content="Answer", finish_reason="stop")],
        ]
    )
    pipeline = _pipeline(monkeypatch, client)
    events = await _run(pipeline, UnifiedContext(session_id="s1", user_message="Go"))

    assert _round_budgets(events) == [1024, 1024]
    assert _recovery_events(events) == []


@pytest.mark.asyncio
async def test_explicit_recovery_policy_still_wins_over_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline_module,
        "get_chat_params",
        lambda: {
            "responding": {"max_tokens": 1024},
            "reasoning_budget_recovery": {"strategy": "increase_output_budget", "max_tokens": 2048},
        },
    )
    client = _ScriptedChatClient(
        [
            [
                _llm_chunk(reasoning_content="private", finish_reason="length"),
                _usage_only_chunk(prompt=100, completion=1024, total=1124),
            ],
            [_llm_chunk(content="Answer", finish_reason="stop")],
        ]
    )
    pipeline = _pipeline(monkeypatch, client)
    events = await _run(pipeline, UnifiedContext(session_id="s1", user_message="Go"))

    # The operator's number (2048), not 1024 * 2.0 rounded or doubled again.
    assert _round_budgets(events) == [1024, 2048]
    assert [event.metadata["escalation"] for event in _recovery_events(events)] == ["configured"]


@pytest.mark.parametrize(
    "config",
    [
        {"enabled": "yes"},
        {"multiplier": 1.0},
        {"multiplier": 9},
        {"ceiling": 0},
        {"ceiling": 131073},
        {"ceiling": 2048.5},
    ],
)
def test_malformed_escalation_configuration_is_rejected(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ReasoningBudgetEscalation.from_config(config, base_max_tokens=1024)


@pytest.mark.parametrize("base", [32768, 40000, 131072])
def test_a_budget_at_or_above_the_ceiling_is_no_headroom_not_an_error(base: int) -> None:
    """The shipped ceiling must never fail a turn a user configured legally.

    ``capabilities.chat`` is read deep-merged with the defaults, so this block
    is present on every install; raising when ``responding.max_tokens`` is at or
    above its ceiling would break chat for anyone who asked for a long output
    budget without touching escalation at all.
    """
    escalation = ReasoningBudgetEscalation.from_config(
        {"ceiling": 32768}, base_max_tokens=base
    )

    assert not escalation.enabled
    assert escalation.next_budget(base) == base

    # And the same shape arrived at through the merged defaults.
    from deeptutor.services.config.loader import DEFAULT_CHAT_PARAMS

    merged = ReasoningBudgetEscalation.from_config(
        dict(DEFAULT_CHAT_PARAMS["reasoning_budget_escalation"]), base_max_tokens=base
    )
    assert merged.next_budget(base) == base


def test_reasoning_dominance_reads_tokens_before_characters() -> None:
    # Provider-reported reasoning tokens: half the budget is the line.
    assert reasoning_dominated_output(max_tokens=8000, reasoning_tokens=8000)
    assert reasoning_dominated_output(max_tokens=8000, reasoning_tokens=4000)
    assert not reasoning_dominated_output(max_tokens=8000, reasoning_tokens=3999)
    # A tool call makes the payload the thing that must also fit.
    assert reasoning_dominated_output(
        max_tokens=8000, reasoning_tokens=2000, tool_call_truncated=True
    )
    # Characters, when the gateway omits the breakdown: dominant *and* long
    # enough that it could have been the cause.
    assert reasoning_dominated_output(max_tokens=8000, reasoning_chars=20000, content_chars=500)
    assert not reasoning_dominated_output(max_tokens=8000, reasoning_chars=7, content_chars=0)
    assert not reasoning_dominated_output(max_tokens=8000, reasoning_chars=500, content_chars=20000)
    # A round that never reached the cap was not truncated by it.
    assert not reasoning_dominated_output(
        max_tokens=8000, reasoning_chars=20000, content_chars=100, completion_tokens=1000
    )


# ---- B. a truncated tool call is never dispatched ------------------------


@pytest.mark.asyncio
async def test_truncated_tool_call_is_dropped_and_asked_for_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its arguments are whatever arrived before the cut; nothing may run."""
    client = _ScriptedChatClient(
        [
            [
                _llm_chunk(
                    content="Running the script now.",
                    tool_calls=[{"id": "a", "name": "web_search", "arguments": '{"que'}],
                    finish_reason="length",
                )
            ],
            [_llm_chunk(content="Answer", finish_reason="stop")],
        ]
    )
    pipeline = _pipeline(monkeypatch, client)
    events = await _run(
        pipeline,
        UnifiedContext(session_id="s1", user_message="Search", enabled_tools=["web_search"]),
    )

    # The guard never saw the call, and no tool row was produced for it.
    assert pipeline.registry.executed == []
    assert not any(
        event.metadata.get("trace_kind") == "tool_call" for event in events
    )
    # The model was told, by name, which call to issue again.
    follow_up = str(client.calls[1]["messages"][-1]["content"])
    assert "web_search" in follow_up
    assert "NOT executed" in follow_up
    # And the reader was told why the turn paused rather than seeing a lone
    # argument-guard error.
    assert any("tool call" in text for text in _warnings(events))
    assert _answer_text(events) == "Running the script now.Answer"


@pytest.mark.asyncio
async def test_complete_tool_call_is_still_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The truncation check must not touch an ordinary tool round."""
    client = _ScriptedChatClient(
        [
            [
                _llm_chunk(
                    tool_calls=[
                        {"id": "a", "name": "web_search", "arguments": '{"query":"x"}'}
                    ],
                    finish_reason="tool_calls",
                )
            ],
            [_llm_chunk(content="Answer", finish_reason="stop")],
        ]
    )
    pipeline = _pipeline(monkeypatch, client)
    events = await _run(
        pipeline,
        UnifiedContext(session_id="s1", user_message="Search", enabled_tools=["web_search"]),
    )

    assert [call["name"] for call in pipeline.registry.executed] == ["web_search"]
    assert _answer_text(events) == "Answer"


# ---- C. a stalled stream settles instead of killing the turn -------------


class _StallingClient:
    """Stalls after the scripted chunks of the current call are consumed."""

    def __init__(self, scripts: list[list[Any]]) -> None:
        self._scripts = list(scripts)
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

        class _Completions:
            def __init__(self, parent: "_StallingClient") -> None:
                self.parent = parent

            async def create(self, **kwargs):
                self.parent.call_count += 1
                self.parent.calls.append(dict(kwargs))
                script = self.parent._scripts.pop(0) if self.parent._scripts else []

                async def _stream():
                    for chunk in script:
                        yield chunk
                    await asyncio.Event().wait()
                    yield _llm_chunk(content="unreachable")

                return _stream()

        self.chat = SimpleNamespace(completions=_Completions(self))


@pytest.mark.asyncio
async def test_stalled_stream_continues_the_turn_instead_of_failing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half an answer on screen is real output: keep it and ask for the rest."""
    client = _StallingClient(
        [
            [_llm_chunk(content="First half ")],
            [_llm_chunk(content="second half.", finish_reason="stop")],
        ]
    )
    pipeline = _pipeline(monkeypatch, client)
    monkeypatch.setattr(agent_loop_mod, "_STREAM_IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(agent_loop_mod, "_PROVIDER_RETRY_DELAYS", (0, 0))

    events = await _run(pipeline, UnifiedContext(session_id="s1", user_message="Explain"))

    assert client.call_count == 2
    # The partial text is published exactly once: the continuation round asks
    # for what is missing instead of replaying the request.
    assert _answer_text(events) == "First half second half."
    assert any("stopped sending output" in text for text in _warnings(events))
    assert any(
        event.metadata.get("tool_call_id") is None
        and event.metadata.get("stream_stalled") is True
        for event in events
    )
    # The continuation carries the partial round in history so the model knows
    # what it already said.
    assert any(
        message.get("role") == "assistant" and message.get("content") == "First half "
        for message in client.calls[1]["messages"]
    )


@pytest.mark.asyncio
async def test_second_stall_in_one_turn_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """One graceful settlement, then the old behaviour: a visible retryable error."""
    client = _StallingClient([[_llm_chunk(content="First half ")], [_llm_chunk(content="Again ")]])
    pipeline = _pipeline(monkeypatch, client)
    monkeypatch.setattr(agent_loop_mod, "_STREAM_IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(agent_loop_mod, "_PROVIDER_RETRY_DELAYS", (0, 0))

    with pytest.raises(LLMProviderTransportError) as raised:
        await pipeline.run(UnifiedContext(session_id="s1", user_message="Explain"), StreamBus())

    assert client.call_count == 2
    assert raised.value.retryable is True
    assert raised.value.partial_response is True


@pytest.mark.asyncio
async def test_stall_after_finish_reason_keeps_the_finished_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the trailing frame was missing; the round itself is complete."""
    client = _StallingClient([[_llm_chunk(content="Complete answer.", finish_reason="stop")]])
    pipeline = _pipeline(monkeypatch, client)
    monkeypatch.setattr(agent_loop_mod, "_STREAM_IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(agent_loop_mod, "_PROVIDER_RETRY_DELAYS", (0, 0))

    events = await _run(pipeline, UnifiedContext(session_id="s1", user_message="Explain"))

    assert client.call_count == 1
    assert _answer_text(events) == "Complete answer."
    assert not any("stream_stalled" in str(event.metadata) for event in events)


# ---- C. the bound itself is shared and configurable ----------------------


def test_idle_bound_defaults_above_the_old_90_seconds() -> None:
    assert stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS > 90.0
    assert stream_bounds.stream_idle_timeout_seconds({}) == (
        stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    )


def test_idle_bound_is_read_from_main_yaml_and_clamped() -> None:
    def _config(seconds: Any) -> dict[str, Any]:
        return {"capabilities": {"chat": {"streaming": {"idle_timeout_seconds": seconds}}}}

    assert stream_bounds.stream_idle_timeout_seconds(_config(600)) == 600.0
    assert (
        stream_bounds.stream_idle_timeout_seconds(_config(1))
        == stream_bounds.MIN_STREAM_IDLE_TIMEOUT_SECONDS
    )
    assert (
        stream_bounds.stream_idle_timeout_seconds(_config(99_999))
        == stream_bounds.MAX_STREAM_IDLE_TIMEOUT_SECONDS
    )
    # Junk falls back to the shipped value instead of failing a turn.
    assert (
        stream_bounds.stream_idle_timeout_seconds(_config("soon"))
        == stream_bounds.DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    )


def test_connect_wait_never_inherits_a_raised_idle_bound() -> None:
    assert stream_bounds.connect_timeout_seconds(600) == (
        stream_bounds.MAX_STREAM_CONNECT_TIMEOUT_SECONDS
    )
    assert stream_bounds.connect_timeout_seconds(30) == 30.0


@pytest.mark.asyncio
async def test_bounded_stream_raises_its_own_transport_error() -> None:
    from deeptutor.services.llm.exceptions import LLMStreamIdleTimeout

    async def _stalled():
        await asyncio.Event().wait()
        yield _llm_chunk(content="never")

    with pytest.raises(LLMStreamIdleTimeout) as raised:
        async for _chunk in stream_bounds.bounded_stream_chunks(_stalled(), timeout_seconds=0.01):
            pass
    assert raised.value.retryable is True
    assert raised.value.idle_seconds == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_explorer_pre_pass_uses_the_shared_bound_without_its_own_copy() -> None:
    """The pre-pass and the answer loop must not drift apart again."""
    from deeptutor.capabilities.explore_context import explorer as explorer_mod

    assert not hasattr(explorer_mod, "_bounded_stream_chunks")
    assert explorer_mod._STREAM_IDLE_TIMEOUT_SECONDS is None
