"""Bounds on a provider response stream, shared by every streaming caller.

Two callers stream a completion and both must survive a provider that accepts
the request and then stops producing chunks: the answer loop
(:mod:`deeptutor.agents.loop.agent_loop`) and the context-exploration pre-pass
(:mod:`deeptutor.capabilities.explore_context.explorer`). Each used to carry its
own copy of that bound — the same 90-second literal, twice — so the deployment
could not tune it and the two copies were free to drift. A third reader,
:mod:`deeptutor.runtime.agentic.labeled_step` (which ``deep_question`` runs),
carried the same literal and now resolves the same value.

Where the value comes from follows the project's own split: ``agents.yaml`` owns
LLM budgets, ``main.yaml`` owns runtime behaviour (iteration caps, timeouts,
retry policy). A read bound is runtime behaviour, so it is read from
``capabilities.chat.streaming.idle_timeout_seconds`` in main.yaml.

Why the default is well above the 90 seconds this replaced: a self-hosted
reasoning model behind a gateway prefills tens of thousands of prompt tokens
before it emits its first delta, and a long chain-of-thought can pause mid
stream too. 90 seconds turned every one of those pauses into a dead turn. The
bound must still exist — a stream that never yields again would park a durable
turn in ``running`` forever, and the turn lease would keep renewing — but it
should be long enough that only a genuinely stalled backend reaches it.

Raising the bound is only half of it: the failure is reported as
:class:`~deeptutor.services.llm.exceptions.LLMStreamIdleTimeout`, so a caller
that already streamed output can settle the round with what arrived instead of
reporting the whole turn as lost.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Mapping

from deeptutor.services.llm.exceptions import LLMStreamIdleTimeout

__all__ = [
    "CONNECT_TIMEOUT_KEY",
    "DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS",
    "IDLE_TIMEOUT_KEY",
    "MAX_STREAM_CONNECT_TIMEOUT_SECONDS",
    "MAX_STREAM_IDLE_TIMEOUT_SECONDS",
    "MIN_STREAM_IDLE_TIMEOUT_SECONDS",
    "bounded_stream_chunks",
    "configured_stream_idle_timeout_seconds",
    "connect_timeout_seconds",
    "stream_idle_timeout_seconds",
]

logger = logging.getLogger(__name__)

#: Shipped idle bound. Long enough for a cold prefill of a large prompt on a
#: self-hosted model, short enough that a wedged gateway still surfaces.
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 300.0

#: Shipped bound for the wait *before* the stream opens. A provider that has not
#: even produced response headers is a different bet from one mid-answer, so a
#: longer idle bound never extends this one past its own ceiling.
DEFAULT_STREAM_CONNECT_TIMEOUT_SECONDS = 120.0

MIN_STREAM_IDLE_TIMEOUT_SECONDS = 15.0
MAX_STREAM_IDLE_TIMEOUT_SECONDS = 3_600.0
MAX_STREAM_CONNECT_TIMEOUT_SECONDS = 120.0

#: Key names, spelled once: users edit these in ``data/user/settings/main.yaml``.
IDLE_TIMEOUT_KEY = "idle_timeout_seconds"
CONNECT_TIMEOUT_KEY = "connect_timeout_seconds"


def _coerce_timeout(raw: Any, default: float, *, upper: float) -> float:
    """Clamp one configured timeout into its usable range."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return max(MIN_STREAM_IDLE_TIMEOUT_SECONDS, min(upper, value))


def _streaming_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """``capabilities.chat.streaming`` from a loaded main.yaml mapping."""
    node: Any = config or {}
    for key in ("capabilities", "chat", "streaming"):
        if not isinstance(node, Mapping):
            return {}
        node = node.get(key) or {}
    return node if isinstance(node, Mapping) else {}


def stream_idle_timeout_seconds(config: Mapping[str, Any] | None = None) -> float:
    """Idle bound in seconds, from ``config`` or the shipped default.

    Args:
        config: A loaded main.yaml mapping. ``None`` reads the settings file.

    Returns:
        The inter-chunk idle bound, clamped to a usable range.
    """
    if config is None:
        return configured_stream_idle_timeout_seconds()
    streaming = _streaming_config(config)
    return _coerce_timeout(
        streaming.get(IDLE_TIMEOUT_KEY),
        DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
        upper=MAX_STREAM_IDLE_TIMEOUT_SECONDS,
    )


def connect_timeout_seconds(idle_timeout_seconds: float) -> float:
    """Bound for waiting on response headers, given the caller's idle bound.

    Args:
        idle_timeout_seconds: The already-resolved inter-chunk bound.

    Returns:
        ``min(idle_timeout_seconds, ceiling)``: raising the idle bound for a
        slow *answer* must not also make a dead endpoint wait minutes before
        the first retry.
    """
    try:
        idle = float(idle_timeout_seconds)
    except (TypeError, ValueError):
        return DEFAULT_STREAM_CONNECT_TIMEOUT_SECONDS
    if idle <= 0:
        return DEFAULT_STREAM_CONNECT_TIMEOUT_SECONDS
    return min(idle, MAX_STREAM_CONNECT_TIMEOUT_SECONDS)


def configured_stream_idle_timeout_seconds() -> float:
    """Idle bound as configured on disk, defaulting instead of raising.

    A malformed or unreadable settings file must not fail a turn: the bound is
    a safety net, and the shipped value is a working one.
    """
    try:
        # Imported lazily so this module stays importable from the runtime layer
        # without pulling the settings stack into module import time.
        from deeptutor.utils.config_manager import ConfigManager

        config = ConfigManager().load_config()
    except Exception:  # pragma: no cover - defensive, settings are local files
        logger.debug("Could not read streaming timeouts from main.yaml", exc_info=True)
        return DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    return stream_idle_timeout_seconds(config)


async def bounded_stream_chunks(
    stream: Any,
    *,
    timeout_seconds: float | None = None,
) -> AsyncIterator[Any]:
    """Yield a provider stream's chunks, failing once it stays idle too long.

    Args:
        stream: The provider's async chunk iterator.
        timeout_seconds: Inter-chunk bound. Callers resolve it once per request
            (configured value, or a module override in tests); ``None`` falls
            back to the configured value here.

    Yields:
        Each chunk the provider produces.

    Raises:
        LLMStreamIdleTimeout: The provider sent nothing for the whole bound.
            Raised as a transport error so existing retry paths still apply,
            and as *its own* type so a round that already streamed output can
            settle gracefully instead of failing the turn.
    """
    idle = (
        float(timeout_seconds)
        if timeout_seconds is not None and float(timeout_seconds) > 0
        else configured_stream_idle_timeout_seconds()
    )
    iterator = stream.__aiter__()
    while True:
        try:
            yield await asyncio.wait_for(iterator.__anext__(), idle)
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise LLMStreamIdleTimeout(idle) from None
