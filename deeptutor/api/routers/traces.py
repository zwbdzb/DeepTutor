"""Read-only turn latency endpoints.

Everything here is derived from data the runtime already persists, executed off
the event loop. No endpoint writes, re-runs a turn, or touches a model, so
polling them can never perturb the thing being measured.
"""

from __future__ import annotations

import asyncio
from functools import partial

from fastapi import APIRouter, HTTPException, Query

from deeptutor.services.tracing import derive
from deeptutor.services.tracing.models import TurnTrace

router = APIRouter()


@router.get("/turns", response_model=list[TurnTrace])
async def recent_turn_traces(
    session_id: str = Query("", description="Restrict to one conversation."),
    limit: int = Query(20, ge=1, le=200),
) -> list[TurnTrace]:
    """Newest-first breakdown for recent turns, newest conversation first.

    One request returns everything a row needs to expand inline, so the page
    never has to round-trip per row while the reader is looking at it.
    """
    traces = await asyncio.to_thread(
        partial(derive.derive_recent, limit=limit, session_id=session_id)
    )
    return [TurnTrace.model_validate(trace) for trace in traces]


@router.get("/sessions/{session_id}/turns", response_model=list[TurnTrace])
async def session_turn_traces(
    session_id: str,
    limit: int = Query(20, ge=1, le=200),
) -> list[TurnTrace]:
    """Newest-first time breakdown for every turn in a conversation."""
    traces = await asyncio.to_thread(
        partial(derive.derive_session, session_id, limit=limit)
    )
    return [TurnTrace.model_validate(trace) for trace in traces]


@router.get("/turns/{turn_id}", response_model=TurnTrace)
async def turn_trace(turn_id: str) -> TurnTrace:
    """Full breakdown for one turn: phases, rounds, tools and findings."""
    trace = await asyncio.to_thread(partial(derive.derive_turn_by_id, turn_id))
    if trace is None:
        raise HTTPException(status_code=404, detail="Turn not found")
    return TurnTrace.model_validate(trace)
