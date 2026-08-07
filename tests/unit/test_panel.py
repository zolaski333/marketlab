"""Tests for the imposed, isolated forecast panel (§15)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from marketlab.agents.decision import ConditionContext
from marketlab.agents.panel import PanelAgent
from marketlab.core.failures import AgentFailureKind, SnapshotStatus
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.forecasting.panel import DEFAULT_HORIZONS, build_panel
from marketlab.instruments.types import (
    AssetClass,
    ExecutionModel,
    InstrumentStatus,
    InstrumentView,
)
from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.models.types import ModelRequest, ModelResponse, RawDecision, RawForecast
from marketlab.retrieval.budget import ToolBudget
from marketlab.retrieval.tools import RetrievalToolkit
from marketlab.retrieval.types import Evidence, EvidenceKind, RetrievalIndex

ALPHA = "id-alpha"
BETA = "id-beta"


def at(hour: int = 20) -> Instant:
    return instant_from_datetime(datetime(2026, 8, 3, hour, 0, tzinfo=UTC))


def _view(
    instrument_id: str, *, status: InstrumentStatus = InstrumentStatus.ACTIVE
) -> InstrumentView:
    return InstrumentView(
        instrument_id=instrument_id,
        asset_class=AssetClass.EQUITY,
        version_number=1,
        ticker=instrument_id.upper(),
        name=instrument_id,
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="TEST_CAL",
        settlement_days=2,
        status=status,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        effective_from=at(),
    )


def _price(instrument_id: str, close: str = "150.03", *, as_of: Instant | None = None) -> Evidence:
    moment = as_of or at()
    return Evidence(
        evidence_id=f"ev-price-{instrument_id}",
        kind=EvidenceKind.PRICE_BAR,
        subject_ids=(instrument_id,),
        as_of=moment,
        first_seen_at=moment,
        blob_hash="a" * 64,
        headline=f"{instrument_id} close",
        fields={
            "bid": str(Decimal(close) - Decimal("0.05")),
            "ask": str(Decimal(close) + Decimal("0.05")),
            "close": close,
            "volume": 1000,
        },
    )


def _index(
    *, views: tuple[InstrumentView, ...] | None = None, evidence: tuple[Evidence, ...] | None = None
) -> RetrievalIndex:
    return RetrievalIndex(
        snapshot_id="snap-1",
        cutoff=at(),
        status=SnapshotStatus.COMPLETE,
        universe=views if views is not None else (_view(ALPHA), _view(BETA)),
        evidence=evidence if evidence is not None else (_price(ALPHA), _price(BETA, "80.11")),
    )


# ---------------------------------------------------------------------------
# Building the panel
# ---------------------------------------------------------------------------


def test_the_panel_covers_every_active_instrument_at_every_horizon() -> None:
    panel = build_panel(_index())
    assert len(panel) == 2 * len(DEFAULT_HORIZONS)
    assert {item.instrument_id for item in panel} == {ALPHA, BETA}
    assert {item.horizon_sessions for item in panel} == set(DEFAULT_HORIZONS)


def test_the_panel_is_identical_for_every_arm_because_it_comes_from_the_snapshot() -> None:
    """The same guarantee, for the same reason, as the shared retrieval index:
    build it twice from one snapshot and it is the same panel."""
    index = _index()
    assert build_panel(index) == build_panel(index)


def test_a_suspended_instrument_is_not_asked_about() -> None:
    panel = build_panel(
        _index(views=(_view(ALPHA), _view(BETA, status=InstrumentStatus.SUSPENDED)))
    )
    assert {item.instrument_id for item in panel} == {ALPHA}


def test_an_instrument_with_no_fresh_price_is_not_asked_about() -> None:
    """A question nobody could answer from the snapshot would be scored as a
    failure to answer, measuring the data feed rather than the condition."""
    stale = _price(BETA, "80.11", as_of=at(hour=10))
    panel = build_panel(_index(evidence=(_price(ALPHA), stale)))
    assert {item.instrument_id for item in panel} == {ALPHA}


def test_panel_items_have_distinct_ids() -> None:
    panel = build_panel(_index())
    assert len({item.item_id for item in panel}) == len(panel)


def test_an_empty_universe_produces_an_empty_panel() -> None:
    assert build_panel(_index(views=(), evidence=())) == ()


# ---------------------------------------------------------------------------
# Answering it
# ---------------------------------------------------------------------------


def _toolkit() -> RetrievalToolkit:
    return RetrievalToolkit(_index(), ToolBudget())


def test_every_panel_item_gets_an_answer() -> None:
    panel = build_panel(_index())
    outcome = PanelAgent().elicit(
        _toolkit(), DeterministicPolicyModel(), ConditionContext(), panel, as_of=at()
    )
    assert len(outcome.answers) == len(panel)
    assert outcome.unanswered_count == 0
    for item in panel:
        answer = outcome.answer_for(item)
        assert answer is not None
        assert 0.0 <= answer.probability_up <= 1.0


def test_answers_are_returned_for_the_horizons_that_were_asked() -> None:
    """Not the model's preferred horizon: the panel imposes the question."""
    panel = build_panel(_index())
    outcome = PanelAgent().elicit(
        _toolkit(), DeterministicPolicyModel(), ConditionContext(), panel, as_of=at()
    )
    assert {answer.horizon_sessions for answer in outcome.answers} == set(DEFAULT_HORIZONS)


def test_every_answer_cites_evidence() -> None:
    panel = build_panel(_index())
    outcome = PanelAgent().elicit(
        _toolkit(), DeterministicPolicyModel(), ConditionContext(), panel, as_of=at()
    )
    assert all(answer.cited_evidence_ids for answer in outcome.answers)


def test_an_unanswered_item_is_recorded_rather_than_dropped() -> None:
    """§15.5. Silence is never a shorter answer set: how often a condition
    fails to answer is one of the things the study counts."""

    class _PartialModel:
        @property
        def model_id(self) -> str:
            return "partial-test-model"

        def generate(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(
                decision=RawDecision(
                    forecasts=(RawForecast(ALPHA, 5, 0.55, ()),), trade_intents=(), narrative=""
                )
            )

    panel = build_panel(_index())
    outcome = PanelAgent().elicit(
        _toolkit(), _PartialModel(), ConditionContext(), panel, as_of=at()
    )
    assert len(outcome.answers) == 1
    assert outcome.unanswered_count == len(panel) - 1
    assert all(
        failure.kind is AgentFailureKind.MISSING_PANEL_ITEM
        for failure in outcome.failures
        if failure.kind is AgentFailureKind.MISSING_PANEL_ITEM
    )


def test_a_refusal_leaves_every_item_unanswered_and_recorded() -> None:
    class _RefusingModel:
        @property
        def model_id(self) -> str:
            return "refusing-test-model"

        def generate(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(refused=True, refusal_reason="not enough information")

    panel = build_panel(_index())
    outcome = PanelAgent().elicit(
        _toolkit(), _RefusingModel(), ConditionContext(), panel, as_of=at()
    )
    assert outcome.answers == ()
    assert outcome.unanswered_count == len(panel)
    assert any(failure.kind is AgentFailureKind.REFUSAL for failure in outcome.failures)


def test_an_empty_panel_is_a_no_op() -> None:
    outcome = PanelAgent().elicit(
        _toolkit(), DeterministicPolicyModel(), ConditionContext(), (), as_of=at()
    )
    assert outcome.answers == ()
    assert outcome.model_turns == 0


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_the_panel_produces_no_trade_intents() -> None:
    """An assessment must not be able to move the portfolio - that is the
    contamination §15's isolation exists to prevent."""
    panel = build_panel(_index())
    response = DeterministicPolicyModel().generate(
        ModelRequest(
            system_prompt="x",
            injected_context=None,
            tool_catalogue=(),
            tool_results=(),
            required_forecasts=tuple(item.key for item in panel),
        )
    )
    assert response.decision is None or response.decision.trade_intents == ()


def test_the_panel_spends_its_own_budget_not_the_decisions() -> None:
    panel = build_panel(_index())
    decision_toolkit = _toolkit()
    panel_toolkit = _toolkit()

    PanelAgent().elicit(
        panel_toolkit, DeterministicPolicyModel(), ConditionContext(), panel, as_of=at()
    )
    assert panel_toolkit.budget.calls_used > 0
    assert decision_toolkit.budget.calls_used == 0


def test_the_panel_receives_the_conditions_granted_material() -> None:
    """Isolated from the *decision*, not from the condition: withholding the
    treatment here would measure an arm that does not exist."""
    seen: list[str | None] = []

    class _RecordingModel:
        def __init__(self) -> None:
            self._inner = DeterministicPolicyModel()

        @property
        def model_id(self) -> str:
            return self._inner.model_id

        def generate(self, request: ModelRequest) -> ModelResponse:
            seen.append(request.injected_context)
            return self._inner.generate(request)

    panel = build_panel(_index())
    PanelAgent().elicit(
        _toolkit(),
        _RecordingModel(),
        ConditionContext(injected_context="remembered material"),
        panel,
        as_of=at(),
    )
    assert "remembered material" in set(filter(None, seen))
