"""Per-decision tool-call budget (§10.6).

Comparing conditions fairly requires bounding what every arm is *allowed* to
spend retrieving evidence, not just observing what each happened to use —
otherwise an arm could win purely by making more tool calls, a difference
having nothing to do with memory or reflection (the actual thing under test).

A budget is consumed across the tool calls one decision makes and then
discarded; it is not scientific data itself; nothing here is persisted.
Exceeding it raises :class:`~marketlab.core.failures.BudgetError`, whose
``scope`` is ``OBSERVED_AGENT_FAILURE`` — this module only enforces the
limit. Recording the failure as an experimental observation
(``AgentFailureKind.BUDGET_EXHAUSTED``) is the calling agent loop's job, the
same division of labour :mod:`marketlab.core.failures` already draws between
raised platform-facing exceptions and recorded data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from marketlab.core.failures import BudgetError, ConfigurationError

__all__ = ["DEFAULT_MAX_EVIDENCE_CHARS", "DEFAULT_MAX_TOOL_CALLS", "ToolBudget"]

DEFAULT_MAX_TOOL_CALLS: Final = 20
DEFAULT_MAX_EVIDENCE_CHARS: Final = 20_000
"""Placeholder magnitudes, not a pre-registered decision.

The real values depend on the still-open API cost model (task #4 in
``docs/ROADMAP.md``, deferred pending the power simulation). Picking them now
would be presenting a guess as a decision; a caller that cares should pass its
own ``max_calls``/``max_evidence_chars`` rather than lean on this default.
"""


@dataclass(slots=True)
class ToolBudget:
    """A mutable per-decision counter, charged by
    :class:`marketlab.retrieval.tools.RetrievalToolkit` on every tool call."""

    max_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_evidence_chars: int = DEFAULT_MAX_EVIDENCE_CHARS
    calls_used: int = 0
    evidence_chars_used: int = 0

    def __post_init__(self) -> None:
        if self.max_calls <= 0:
            raise ConfigurationError(f"max_calls must be positive, got {self.max_calls}")
        if self.max_evidence_chars <= 0:
            raise ConfigurationError(
                f"max_evidence_chars must be positive, got {self.max_evidence_chars}"
            )

    def charge(self, *, chars: int = 0) -> None:
        """Record one tool call and the evidence it returned.

        Checked before either counter is mutated, so a call that fails either
        cap leaves the budget exactly as it was — an agent that hits the wall
        does not get to keep spending against a corrupted balance.

        Raises:
            BudgetError: if this call would exceed either cap.
        """
        if self.calls_used + 1 > self.max_calls:
            raise BudgetError(
                f"Tool call budget exhausted: {self.calls_used}/{self.max_calls} calls "
                "already used.",
                calls_used=self.calls_used,
                max_calls=self.max_calls,
            )
        if self.evidence_chars_used + chars > self.max_evidence_chars:
            raise BudgetError(
                f"Evidence character budget exhausted: {self.evidence_chars_used}/"
                f"{self.max_evidence_chars} chars already used, this call would add {chars}.",
                evidence_chars_used=self.evidence_chars_used,
                max_evidence_chars=self.max_evidence_chars,
                requested_chars=chars,
            )
        self.calls_used += 1
        self.evidence_chars_used += chars

    @property
    def calls_remaining(self) -> int:
        return self.max_calls - self.calls_used
