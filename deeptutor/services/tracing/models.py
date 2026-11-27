"""Response models for ``/api/traces``.

Declared so the OpenAPI export produces typed client contracts; the analyzer
itself works on plain dicts so it stays usable without FastAPI.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TraceBreakdown(BaseModel):
    queue_ms: float = 0.0
    context_ms: float | None = None
    model_ms: float = 0.0
    tool_ms: float = 0.0
    wait_ms: float = 0.0
    other_exec_ms: float | None = None
    persist_ms: float | None = None
    unattributed_ms: float = 0.0


class TraceRound(BaseModel):
    n: int
    ms: float
    status: str = ""
    ttft_ms: float | None = None
    gen_ms: float | None = None
    completion_tokens: int | None = None
    tools: int = 0


class TraceTool(BaseModel):
    name: str
    ms: float
    status: str = ""


class TraceStage(BaseModel):
    name: str = ""
    ms: float = 0.0


class TraceFinding(BaseModel):
    code: str
    severity: str = "info"
    text: str = ""


class TurnTrace(BaseModel):
    turn_id: str
    session_id: str = ""
    capability: str = ""
    status: str = ""
    created_at: float = 0.0
    total_ms: float = 0.0
    estimated: bool = False
    top_bottleneck: str = ""
    breakdown: TraceBreakdown = Field(default_factory=TraceBreakdown)
    # 本轮问题：turn_id 对人没有意义，问题文本才是「这是哪一轮」的标识。
    user_message_id: int | None = None
    user_message: str = ""
    user_message_truncated: bool = False
    # Subset of ``breakdown.tool_ms``: wall time spent on tool calls that
    # failed. Reported beside the phases because it must not be added to them.
    failed_tool_ms: float = 0.0
    tool_failure_rate: float | None = None
    rounds: list[TraceRound] = Field(default_factory=list)
    tools: list[TraceTool] = Field(default_factory=list)
    waits: list[dict[str, float]] = Field(default_factory=list)
    stages: list[TraceStage] = Field(default_factory=list)
    findings: list[TraceFinding] = Field(default_factory=list)


__all__ = [
    "TraceBreakdown",
    "TraceFinding",
    "TraceRound",
    "TraceStage",
    "TraceTool",
    "TurnTrace",
]
