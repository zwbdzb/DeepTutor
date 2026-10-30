"""Backlog accounting edge cases for ``TurnRecoveryService``.

The fix routes liveness-probed turns out of the backlog *before* recovery, so
these tests attack the paths the happy-path tests skip: a probe that raises, a
recovery step that raises, and mixed batches where only some turns are live.
"""

from __future__ import annotations

import asyncio

import pytest

from deeptutor.runtime.coordination import MemoryCoordinator
from deeptutor.runtime.coordination.recovery import TurnRecoveryService
from deeptutor.services.session.sqlite_store import SQLiteSessionStore


async def _expired_turn(repository, coordinator, turn_id, owner_id="worker-a"):
    session = await repository.ensure_session(None)
    lease = await coordinator.acquire_turn(turn_id, session["id"], owner_id)
    assert lease is not None
    await repository.begin_turn(
        session["id"],
        "chat",
        turn_id=lease.turn_id,
        owner_id=lease.owner_id,
        fencing_token=lease.fencing_token,
    )
    await asyncio.sleep(0.02)
    return lease


async def _always(value: bool):
    async def probe(turn_id: str) -> bool:
        return value

    return probe


# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_batch_recovers_only_the_dead_turns(tmp_path):
    repository = SQLiteSessionStore(tmp_path / "turns.db")
    coordinator = MemoryCoordinator(lease_ttl_seconds=0.01)
    live = await _expired_turn(repository, coordinator, "turn-live")
    dead = await _expired_turn(repository, coordinator, "turn-dead")
    recovery = TurnRecoveryService(coordinator, repository, is_live=await _always(True))
    seen: list[str] = []

    async def probe(turn_id: str) -> bool:
        seen.append(turn_id)
        return turn_id == "turn-live"

    recovery.is_live = probe

    assert await recovery.recover_once() == 1
    assert recovery.backlog == 0
    # Both expired turns were probed exactly once (order is not contractual).
    assert sorted(seen) == ["turn-dead", "turn-live"]
    assert (await repository.get_turn(live.turn_id))["status"] == "running"
    assert (await repository.get_turn(dead.turn_id))["status"] == "failed"
    # The live turn keeps its lease entry: nothing acknowledged it.
    assert await coordinator.list_expired_turn_ids() == [live.turn_id]


@pytest.mark.asyncio
async def test_a_live_turn_never_accumulates_backlog_across_cycles(tmp_path):
    """The reported bug: one slow turn looked like a permanent queue."""
    repository = SQLiteSessionStore(tmp_path / "turns.db")
    coordinator = MemoryCoordinator(lease_ttl_seconds=0.01)
    await _expired_turn(repository, coordinator, "turn-live")
    recovery = TurnRecoveryService(coordinator, repository, is_live=await _always(True))

    for _ in range(3):
        assert await recovery.recover_once() == 0
        assert recovery.backlog == 0


@pytest.mark.asyncio
async def test_a_failing_probe_never_hides_or_strands_a_turn(tmp_path):
    """An unanswerable probe must not abort the cycle nor lose the turn."""
    repository = SQLiteSessionStore(tmp_path / "turns.db")
    coordinator = MemoryCoordinator(lease_ttl_seconds=0.01)
    flaky = await _expired_turn(repository, coordinator, "turn-flaky")
    healthy = await _expired_turn(repository, coordinator, "turn-healthy")

    async def probe(turn_id: str) -> bool:
        if turn_id == "turn-flaky":
            raise RuntimeError("probe exploded")
        return False

    recovery = TurnRecoveryService(coordinator, repository, is_live=probe)

    # Both turns are still recovered in the same cycle: the exception is
    # contained to its own turn instead of unwinding ``recover_once``.
    assert await recovery.recover_once() == 2
    assert recovery.backlog == 0
    assert (await repository.get_turn(flaky.turn_id))["status"] == "failed"
    assert (await repository.get_turn(healthy.turn_id))["status"] == "failed"
    assert await coordinator.list_expired_turn_ids() == []


@pytest.mark.asyncio
async def test_a_turn_that_cannot_be_recovered_stays_outstanding(tmp_path):
    """A broken repository leaves the turn expired for the next cycle."""
    repository = SQLiteSessionStore(tmp_path / "turns.db")
    coordinator = MemoryCoordinator(lease_ttl_seconds=0.01)
    lease = await _expired_turn(repository, coordinator, "turn-stuck")

    class BrokenRepository:
        def __getattr__(self, name):
            return getattr(repository, name)

        async def get_turn(self, turn_id):
            raise RuntimeError("repository is down")

    recovery = TurnRecoveryService(coordinator, BrokenRepository())

    assert await recovery.recover_once() == 0
    assert recovery.backlog == 1
    assert await coordinator.list_expired_turn_ids() == [lease.turn_id]


@pytest.mark.asyncio
async def test_settled_counts_acknowledged_turns_not_recovered_ones(tmp_path):
    """Terminal turns are acknowledged without counting as recovered."""
    repository = SQLiteSessionStore(tmp_path / "turns.db")
    coordinator = MemoryCoordinator(lease_ttl_seconds=0.01)
    terminal = await _expired_turn(repository, coordinator, "turn-done")
    retryable = await _expired_turn(repository, coordinator, "turn-lost")
    assert await repository.transition_turn(terminal.turn_id, "completed")

    recovery = TurnRecoveryService(coordinator, repository)

    assert await recovery.recover_once() == 1  # only turn-lost was recovered
    assert recovery.backlog == 0  # both left the backlog
    assert await coordinator.list_expired_turn_ids() == []
    assert (await repository.get_turn(terminal.turn_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_backlog_survives_a_zero_or_negative_settle(tmp_path):
    """Nothing settled means the backlog keeps its full size."""
    repository = SQLiteSessionStore(tmp_path / "turns.db")
    coordinator = MemoryCoordinator(lease_ttl_seconds=0.01)
    await _expired_turn(repository, coordinator, "turn-a")
    await _expired_turn(repository, coordinator, "turn-b")

    class OwnerlessRepository:
        def __getattr__(self, name):
            return getattr(repository, name)

        async def get_turn(self, turn_id):
            return None  # an id from another user's repository

    recovery = TurnRecoveryService(coordinator, OwnerlessRepository())

    assert await recovery.recover_once() == 0
    assert recovery.backlog == 2
