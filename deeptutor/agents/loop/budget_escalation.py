"""One bounded escalation of a round's output budget.

Why this exists: a reasoning model's hidden tokens and its visible answer are
paid for out of the same ``max_tokens``. The loop's budget model used to treat
reasoning as something *beside* the output, so a round could spend the entire
cap on chain-of-thought and return with ``content_chars=0`` — the reader sees
"thinking" and then nothing. The turn then retried at the same cap, which
fails the same way, and ended with a terminal error the user reads as "the
model just stopped".

Recovery from that shape is a *budget* problem, not a prompt problem, so it
must not require the operator to have opted into
``reasoning_budget_recovery.strategy: increase_output_budget`` in advance.
This module supplies the automatic half of that decision:

* it only fires for a round the provider cut off (or one whose budget
  reasoning demonstrably consumed), never for an ordinary round;
* it fires at most once per turn (the caller owns that latch), so a model that
  reasons past every bound cannot make the budget grow exponentially;
* it is capped absolutely (``ceiling``, and the same 131072 provider ceiling the
  recovery policy is validated against), so the cost of the one extra attempt
  is bounded and predictable;
* an explicit ``increase_output_budget`` policy still wins — an operator who
  named a number gets that number.

Cost note for the operator: one escalated round may emit up to ``ceiling``
output tokens instead of ``responding.max_tokens``. With the shipped defaults
(8000 -> 16000, ceiling 32768) that is at most 8000 extra output tokens on one
round of one turn, and it only happens when the turn would otherwise have
failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_ESCALATION_CEILING",
    "DEFAULT_ESCALATION_MULTIPLIER",
    "MAX_OUTPUT_TOKENS",
    "MIN_REASONING_CHARS_FOR_ESCALATION",
    "REASONING_DOMINANCE_RATIO",
    "REASONING_SHARE_TOOL_CALL_RATIO",
    "ReasoningBudgetEscalation",
    "reasoning_dominated_output",
]

#: The provider-side ceiling the recovery policy is validated against. The
#: escalation may not name a budget the request layer would reject.
MAX_OUTPUT_TOKENS = 131_072

#: Double the budget: enough headroom for a chain of thought that already
#: consumed a whole budget plus an answer, without inventing an open-ended one.
DEFAULT_ESCALATION_MULTIPLIER = 2.0

#: Absolute ceiling for an automatically escalated round.
DEFAULT_ESCALATION_CEILING = 32_768

#: Share of the round's output budget that reasoning must have taken before the
#: *budget* — rather than the model's verbosity — is the thing to fix. Half is
#: deliberately conservative: a round whose reasoning took less than half and
#: still ran out has a length problem that more thought will not solve.
REASONING_DOMINANCE_RATIO = 0.5

#: The same test for a round cut off *while writing a tool call*. Such a round
#: pays for reasoning and for a whole argument payload at once, so reasoning
#: does not have to be half the budget to have caused the cut: the payload is
#: regenerated from scratch on the continuation, and doing that at the same cap
#: is what repeats the failure.
REASONING_SHARE_TOOL_CALL_RATIO = 0.25

#: Floor for the character-based reading. A share of the streamed text is
#: meaningless when the round streamed almost nothing: seven characters of
#: thinking beside a cut-off tool call are 100% of the text and 0% of the
#: budget, and escalating for that would raise the cost of every round that
#: starts with a sentence of thought. ~500 tokens of reasoning is the smallest
#: amount that could plausibly matter against a real output cap.
MIN_REASONING_CHARS_FOR_ESCALATION = 2000


@dataclass(frozen=True, slots=True)
class ReasoningBudgetEscalation:
    """How much more room a reasoning-heavy truncated round may ask for."""

    enabled: bool = True
    multiplier: float = DEFAULT_ESCALATION_MULTIPLIER
    ceiling: int = DEFAULT_ESCALATION_CEILING

    @classmethod
    def from_config(cls, value: Any, *, base_max_tokens: int) -> "ReasoningBudgetEscalation":
        """Build from ``capabilities.chat.reasoning_budget_escalation``.

        Args:
            value: The mapping from agents.yaml, or ``None`` for the defaults.
            base_max_tokens: The round budget escalation starts from, used to
                reject a configuration that can never escalate.

        Returns:
            The escalation policy.

        Raises:
            ValueError: The mapping is malformed. A silently ignored budget
                knob would hide the very failure it exists to prevent. A
                ceiling that leaves no headroom is *not* malformed — see below.
        """
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("chat.reasoning_budget_escalation must be a mapping")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("chat.reasoning_budget_escalation.enabled must be a boolean")
        multiplier = value.get("multiplier", DEFAULT_ESCALATION_MULTIPLIER)
        if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
            raise ValueError("chat.reasoning_budget_escalation.multiplier must be a number")
        if not 1.0 < float(multiplier) <= 8.0:
            raise ValueError(
                "chat.reasoning_budget_escalation.multiplier must be greater than 1 "
                "and at most 8"
            )
        ceiling = value.get("ceiling", DEFAULT_ESCALATION_CEILING)
        if type(ceiling) is not int or not 0 < ceiling <= MAX_OUTPUT_TOKENS:
            raise ValueError(
                "chat.reasoning_budget_escalation.ceiling must be a positive integer "
                f"and be at most {MAX_OUTPUT_TOKENS}"
            )
        if ceiling <= base_max_tokens:
            # No headroom, not a misconfiguration: the round budget already sits
            # at or above the ceiling. ``responding.max_tokens`` above the
            # shipped ceiling is a legitimate setting for a long-output model,
            # and this block is deep-merged into every install, so rejecting the
            # pair here would fail the turn over a value the operator never
            # wrote. The policy says it instead: escalation is off because
            # there is nowhere to escalate to.
            return cls(enabled=False, multiplier=float(multiplier), ceiling=ceiling)
        return cls(enabled=enabled, multiplier=float(multiplier), ceiling=ceiling)

    def next_budget(self, current_max_tokens: int) -> int:
        """The escalated budget, or ``current_max_tokens`` when none is left.

        Args:
            current_max_tokens: The budget the previous round actually used.

        Returns:
            A strictly larger budget, capped; the input is returned unchanged
            when escalation is off or the current budget is already at the
            ceiling, so callers can compare the two.
        """
        if not self.enabled:
            return current_max_tokens
        ceiling = min(self.ceiling, MAX_OUTPUT_TOKENS)
        candidate = min(int(round(current_max_tokens * self.multiplier)), ceiling)
        return candidate if candidate > current_max_tokens else current_max_tokens


def reasoning_dominated_output(
    *,
    max_tokens: int,
    reasoning_tokens: int | None = None,
    completion_tokens: int | None = None,
    reasoning_chars: int = 0,
    content_chars: int = 0,
    tool_call_truncated: bool = False,
) -> bool:
    """Whether reasoning, not the visible answer, consumed this round's budget.

    Two readings, in order of precision:

    1. the provider's own token breakdown (``usage.reasoning_tokens``), which
       self-hosted OpenAI-compatible servers such as vLLM report exactly;
    2. the streamed character counts, for gateways that omit the breakdown —
       reasoning must dominate the text by a wide margin, so a long answer
       that merely made room for a little thinking is not misread as starved.

    Args:
        max_tokens: The budget the round requested.
        reasoning_tokens: ``usage.reasoning_tokens``, when the provider reports it.
        completion_tokens: ``usage.completion_tokens``, when reported.
        reasoning_chars: Characters streamed as reasoning.
        content_chars: Characters streamed as user-facing content.
        tool_call_truncated: The round was cut off while writing a tool call,
            which lowers the share reasoning must have taken to count.

    Returns:
        True when the budget was a reasoning budget rather than an answer
        budget, i.e. when raising it is the fix.
    """
    if max_tokens <= 0:
        return False
    ratio = REASONING_SHARE_TOOL_CALL_RATIO if tool_call_truncated else REASONING_DOMINANCE_RATIO
    threshold = max(1, int(round(int(max_tokens) * ratio)))
    if reasoning_tokens:
        return int(reasoning_tokens) >= threshold
    if reasoning_chars < MIN_REASONING_CHARS_FOR_ESCALATION:
        return False
    if completion_tokens and int(completion_tokens) < int(max_tokens):
        # The round did not actually reach the cap; verbosity is not the issue.
        return False
    streamed = max(1, int(reasoning_chars) + int(content_chars))
    # A floor on reasoning's *own* share of the streamed text — the same
    # direction as the token test above, so lowering ``ratio`` for a truncated
    # tool call lowers the bar in both readings. Using ``1.0 - ratio`` here made
    # the tool-call case stricter than an ordinary round, which is exactly
    # backwards: such a round pays for reasoning and a whole argument payload
    # at once.
    return int(reasoning_chars) >= max(1, int(streamed * ratio))

