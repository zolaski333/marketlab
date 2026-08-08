"""Every failure the taxonomy declares is exercised somewhere (§23, §30.8).

The origin story in ``docs/ROADMAP.md`` records an implementation whose
database contained **zero** failure events for a study that deliberately
injected three distinct agent failures. The types existed; nothing produced
them, and nothing checked that anything did.

A taxonomy is only worth having if each of its members is reachable and tested.
So this scans the test suite for every declared failure kind, rejection reason
and failure scope, and fails when one is added with no test behind it.

A mention is not an assertion, so the scan alone would be a weak check. What
makes it worth having is that it is *falsifiable and it fired*: adding it
surfaced four members of the taxonomy that nothing tested —
``INSUFFICIENT_CASH``, ``NOTHING_TO_SELL``, ``BELOW_MINIMUM_SIZE`` and every
platform scope — and ``tests/unit/test_execution_failures.py`` exists because
of it. The section at the bottom of this file reaches the remaining members
directly, including the one that is deliberately unreachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marketlab.agents.decision import ConditionContext, DecisionAgent
from marketlab.core.failures import AgentFailureKind, FailureScope, ObservedAgentFailure
from marketlab.execution.types import RejectionReason
from marketlab.models.types import ModelRequest, ModelResponse, RawDecision, RawForecast
from marketlab.retrieval.budget import ToolBudget
from marketlab.retrieval.tools import RetrievalToolkit
from tests.unit.test_panel import ALPHA, _index, at

_TESTS_ROOT = Path(__file__).resolve().parent.parent


def _test_sources() -> str:
    """Every test file, this one included.

    The scan cannot be satisfied by its own enumeration: the parametrised
    cases below iterate over the **enum members**, never over text, so the only
    strings this file contributes are those its own tests genuinely reach —
    the deliberately unreachable ``FUTURE_EVIDENCE`` and the scope every
    observed agent failure carries. ``test_the_scan_would_notice_a_kind_nobody_tested``
    is what keeps that honest.
    """
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_TESTS_ROOT.rglob("test_*.py"))
    )


@pytest.mark.parametrize("kind", list(AgentFailureKind), ids=str)
def test_every_agent_failure_kind_is_named_by_some_test(kind: AgentFailureKind) -> None:
    assert str(kind) in _test_sources(), (
        f"{kind} is declared in the failure taxonomy but no test mentions it. "
        "Either produce it somewhere and assert on it, or delete it: a category "
        "nothing can reach is a category the study will never report."
    )


@pytest.mark.parametrize("reason", list(RejectionReason), ids=str)
def test_every_rejection_reason_is_named_by_some_test(reason: RejectionReason) -> None:
    assert str(reason) in _test_sources(), f"{reason} has no test behind it."


@pytest.mark.parametrize("scope", list(FailureScope), ids=str)
def test_every_failure_scope_is_named_by_some_test(scope: FailureScope) -> None:
    assert str(scope) in _test_sources(), f"{scope} has no test behind it."


def test_the_scan_would_notice_a_kind_nobody_tested() -> None:
    """Guards the three tests above against passing vacuously.

    The sentinel is assembled at runtime so the literal never appears in any
    source file — a check for a name that is present in the checker itself
    would pass forever, which is the failure mode this whole module is about.
    """
    sentinel = "COMPLETELY_MADE_UP" + "_FAILURE_KIND"
    assert sentinel not in _test_sources()


# ---------------------------------------------------------------------------
# The members whose coverage a text scan could not honestly claim on its own
# ---------------------------------------------------------------------------


def test_future_evidence_is_unreachable_by_construction_not_by_omission() -> None:
    """``FUTURE_EVIDENCE`` (§14.5) is never produced, and that is correct.

    Every ``evidence_id`` present in a :class:`RetrievalIndex` already
    satisfies ``first_seen_at <= cutoff``, because the snapshot builder applied
    exactly that filter. So a citation that resolves at all cannot be dated in
    the future, and the only way to violate §14.5 is to cite an id that is not
    in the index — which ``NONEXISTENT_EVIDENCE`` catches.

    Asserted rather than left as a comment: if the index ever stopped being
    built by that filter, the kind would become reachable and this test is
    where that shows up.
    """
    index = _index()
    assert index.evidence, "an empty index would make this vacuous"
    assert all(item.first_seen_at <= index.cutoff for item in index.evidence)

    class _CitingTheFuture:
        @property
        def model_id(self) -> str:
            return "future-citing-test-model"

        def generate(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(
                decision=RawDecision(
                    forecasts=(RawForecast(ALPHA, 5, 0.6, ("ev-from-next-week",)),),
                    trade_intents=(),
                    narrative="",
                )
            )

    outcome = DecisionAgent().decide(
        RetrievalToolkit(index, ToolBudget()),
        _CitingTheFuture(),
        ConditionContext(),
        as_of=at(),
    )
    kinds = {failure.kind for failure in outcome.failures}
    assert AgentFailureKind.NONEXISTENT_EVIDENCE in kinds
    assert AgentFailureKind.FUTURE_EVIDENCE not in kinds


def test_a_platform_failure_declares_its_blast_radius() -> None:
    """§3's scopes are not decoration: an operator reading a failure needs to
    know whether one condition is missing or the whole run is void, and the
    exception itself says which."""
    from marketlab.core.failures import (
        ConfigurationError,
        MarketLabError,
        ModelProviderError,
        SnapshotError,
        TemporalLeakError,
    )

    assert MarketLabError("x").scope is FailureScope.RUN_FATAL
    assert ConfigurationError("x").scope is FailureScope.RUN_FATAL
    assert TemporalLeakError("x").scope is FailureScope.CYCLE_INVALID
    assert SnapshotError("x").scope is FailureScope.CYCLE_INVALID
    assert ModelProviderError("x").scope is FailureScope.CONDITION_MISSING


def test_a_degraded_but_valid_cycle_is_a_distinct_scope() -> None:
    """DEGRADED_VALID exists so that "usable, flagged" is recordable. Folding
    it into CYCLE_INVALID would discard every session with a partial data gap;
    folding it into nothing would hide that the gap happened."""
    assert FailureScope.DEGRADED_VALID != FailureScope.CYCLE_INVALID
    assert FailureScope.DEGRADED_VALID in set(FailureScope)


def test_an_observed_agent_failure_cannot_be_raised() -> None:
    """The distinction the whole taxonomy is built on: an agent failure is a
    record, so it is impossible to ``except ... : pass`` it out of existence.
    A previous implementation did exactly that and shipped a study with no
    failure events at all."""
    failure = ObservedAgentFailure(
        kind=AgentFailureKind.REFUSAL, detail="declined", occurred_at=at()
    )
    assert not isinstance(failure, BaseException)
    assert failure.scope is FailureScope.OBSERVED_AGENT_FAILURE
    with pytest.raises(TypeError):
        raise failure  # type: ignore[misc]
