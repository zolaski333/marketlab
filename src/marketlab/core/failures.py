"""Failure taxonomy (§3, §23).

Every abnormal outcome in the platform must be classifiable, because the
analysis plan treats the classes differently: one invalidates a cycle, another
*is* the experimental result.

The distinction that matters most
---------------------------------
An **agent failure** is data. When the model emits malformed JSON, cites an
evidence id that does not exist, hallucinates a ticker, or refuses to answer,
that is an observation about the model — precisely the kind of observation this
study exists to collect (§23.3, §34.8). It must be recorded and must not
propagate as an exception that some ``except`` clause swallows.

A **platform failure** is a bug or an outage. It is an exception.

Encoding this in the type system means the two cannot be confused:
:class:`ObservedAgentFailure` is a *record*, not an exception — it is impossible
to ``raise`` it, and therefore impossible to accidentally ``except ... : pass``
it out of existence. A previous implementation returned ``None`` on hallucinated
tickers and wrapped execution in ``except ExecutionRefusedError: pass``; the
resulting database contained zero failure events for a study that deliberately
injected three distinct agent failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from marketlab.core.instants import Instant

__all__ = [
    "AgentFailureKind",
    "FailureScope",
    "MarketLabError",
    "ObservedAgentFailure",
    "RequirementLevel",
    "SnapshotStatus",
]


class RequirementLevel(StrEnum):
    """Normative level of a requirement (§3)."""

    INV = "INV"
    """Scientific invariant. Violation invalidates the affected unit."""

    REQ = "REQ"
    """Required for the main study; failure follows a pre-registered policy."""

    FID = "FID"
    """Simulation fidelity. Absence is documented, not disqualifying."""

    OPS = "OPS"
    """Operational robustness, ergonomics, maintenance, observability."""


class FailureScope(StrEnum):
    """Blast radius of a failure (§3)."""

    RUN_FATAL = "RUN_FATAL"
    """The whole execution must stop."""

    CYCLE_INVALID = "CYCLE_INVALID"
    """The entire cycle is unusable for the primary analysis."""

    CONDITION_MISSING = "CONDITION_MISSING"
    """One arm or repetition is missing; the paired policy applies (§23.4)."""

    DEGRADED_VALID = "DEGRADED_VALID"
    """Usable cycle, flagged as degraded."""

    OBSERVED_AGENT_FAILURE = "OBSERVED_AGENT_FAILURE"
    """An agent error retained as an experimental result (§23.3)."""


class SnapshotStatus(StrEnum):
    """Completeness of an exogenous snapshot (§23.2)."""

    COMPLETE = "COMPLETE"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"


class AgentFailureKind(StrEnum):
    """Categories of agent failure retained as results (§30.8)."""

    MALFORMED_JSON = "MALFORMED_JSON"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    UNRESOLVED_INSTRUMENT = "UNRESOLVED_INSTRUMENT"
    """A ticker or name that resolves to no instrument — a hallucination (§7.2)."""

    NONEXISTENT_EVIDENCE = "NONEXISTENT_EVIDENCE"
    """A claim citing an evidence id absent from the snapshot (§14.5)."""

    FUTURE_EVIDENCE = "FUTURE_EVIDENCE"
    """A claim citing evidence dated after the cutoff (§14.5)."""

    PROBABILITY_OUT_OF_RANGE = "PROBABILITY_OUT_OF_RANGE"
    TRUNCATED_OUTPUT = "TRUNCATED_OUTPUT"
    REFUSAL = "REFUSAL"
    MISSING_PANEL_ITEM = "MISSING_PANEL_ITEM"
    """No probability produced for a mandated panel item (§15.5)."""

    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    NON_TRADABLE_INSTRUMENT = "NON_TRADABLE_INSTRUMENT"
    """An order on a RESEARCH_ONLY / SUSPENDED / UNVALUABLE instrument (§7.5)."""

    UNSUPPORTED_EXECUTION = "UNSUPPORTED_EXECUTION"
    """No honest execution model exists for the requested instrument (§16.4)."""


@dataclass(frozen=True, slots=True)
class ObservedAgentFailure:
    """An agent error, retained as an experimental observation.

    Deliberately *not* an exception. It is constructed, recorded, and carried in
    the result of the operation that produced it.
    """

    kind: AgentFailureKind
    detail: str
    occurred_at: Instant
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def scope(self) -> FailureScope:
        return FailureScope.OBSERVED_AGENT_FAILURE


# ---------------------------------------------------------------------------
# Platform exceptions
# ---------------------------------------------------------------------------


class MarketLabError(Exception):
    """Base class for platform failures.

    Carries a scope so that the caller — and the failure log — records the
    pre-registered blast radius rather than inferring it.
    """

    scope: FailureScope = FailureScope.RUN_FATAL

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = context


class ConfigurationError(MarketLabError):
    """Malformed or internally inconsistent configuration."""


class TemporalLeakError(MarketLabError):
    """An attempt to read data dated after the active cutoff (§6.3, INV-P5)."""

    scope = FailureScope.CYCLE_INVALID


class AccountingError(MarketLabError):
    """The ledger would be left unbalanced or inconsistent (§17.1)."""

    scope = FailureScope.RUN_FATAL


class IntegrityError(MarketLabError):
    """A hash, chain link, or reference failed verification (§24)."""

    scope = FailureScope.RUN_FATAL


class ImmutabilityError(MarketLabError):
    """An attempt to mutate append-only scientific data (§P4)."""

    scope = FailureScope.RUN_FATAL


class SnapshotError(MarketLabError):
    """The exogenous snapshot could not be built or is unusable (§23.2)."""

    scope = FailureScope.CYCLE_INVALID


class ModelProviderError(MarketLabError):
    """The model provider was unreachable after the allowed retries (§23.4)."""

    scope = FailureScope.CONDITION_MISSING


class BudgetError(MarketLabError):
    """A per-cycle budget was exceeded (§10.6)."""

    scope = FailureScope.OBSERVED_AGENT_FAILURE
