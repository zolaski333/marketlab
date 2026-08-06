"""Tests for the decision orchestration loop (§10, §12, §14.5)."""

from __future__ import annotations

from datetime import UTC, datetime

from marketlab.agents.decision import ConditionContext, DecisionAgent
from marketlab.core.failures import AgentFailureKind, SnapshotStatus
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.instruments.types import (
    AssetClass,
    ExecutionModel,
    InstrumentStatus,
    InstrumentView,
)
from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.models.types import (
    ModelRequest,
    ModelResponse,
    RawDecision,
    RawForecast,
    RawTradeIntent,
    ToolCallRequest,
    TradeSide,
)
from marketlab.retrieval.budget import ToolBudget
from marketlab.retrieval.tools import RetrievalToolkit
from marketlab.retrieval.types import Evidence, EvidenceKind, RetrievalIndex

ALPHA_ID = "id-alpha"
PRICE_EVIDENCE_ID = "ev-price-alpha"


def _instant(hour: int) -> Instant:
    return instant_from_datetime(datetime(2026, 8, 1, hour, 0, tzinfo=UTC))


def _alpha_view() -> InstrumentView:
    return InstrumentView(
        instrument_id=ALPHA_ID,
        asset_class=AssetClass.EQUITY,
        version_number=1,
        ticker="EQ_US_ALPHA",
        name="Alpha Corp",
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="TEST_CAL",
        settlement_days=2,
        status=InstrumentStatus.ACTIVE,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        effective_from=_instant(0),
    )


def _price_evidence() -> Evidence:
    return Evidence(
        evidence_id=PRICE_EVIDENCE_ID,
        kind=EvidenceKind.PRICE_BAR,
        subject_ids=(ALPHA_ID,),
        as_of=_instant(10),
        first_seen_at=_instant(10),
        blob_hash="a" * 64,
        headline=f"{ALPHA_ID} close=150.00",
        fields={"bid": "149.95", "ask": "150.05", "close": "150.00", "volume": 1000},
    )


def _index() -> RetrievalIndex:
    return RetrievalIndex(
        snapshot_id="snap-1",
        cutoff=_instant(10),
        status=SnapshotStatus.COMPLETE,
        universe=(_alpha_view(),),
        evidence=(_price_evidence(),),
    )


def _toolkit(**budget_kwargs: int) -> RetrievalToolkit:
    return RetrievalToolkit(_index(), ToolBudget(**budget_kwargs))


class _ScriptedModel:
    """Returns a fixed sequence of responses; repeats the last one past the end."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self._calls = 0

    @property
    def model_id(self) -> str:
        return "scripted-test-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        index = min(self._calls, len(self._responses) - 1)
        self._calls += 1
        return self._responses[index]


def test_the_deterministic_policy_produces_a_valid_decision_end_to_end() -> None:
    agent = DecisionAgent()
    outcome = agent.decide(
        _toolkit(), DeterministicPolicyModel(), ConditionContext(), as_of=_instant(10)
    )
    assert outcome.failures == ()
    assert len(outcome.forecasts) == 1
    assert outcome.forecasts[0].instrument_id == ALPHA_ID
    assert len(outcome.trade_intents) == 1
    assert outcome.tool_calls_made >= 3  # search_instruments + get_price_quote + search_news


def test_a_refusal_is_recorded_and_produces_no_forecasts() -> None:
    model = _ScriptedModel([ModelResponse(refused=True, refusal_reason="not enough information")])
    agent = DecisionAgent()
    outcome = agent.decide(_toolkit(), model, ConditionContext(), as_of=_instant(10))
    assert outcome.forecasts == ()
    assert [f.kind for f in outcome.failures] == [AgentFailureKind.REFUSAL]


def test_a_response_with_neither_tool_calls_nor_decision_nor_refusal_is_malformed() -> None:
    model = _ScriptedModel([ModelResponse()])
    agent = DecisionAgent()
    outcome = agent.decide(_toolkit(), model, ConditionContext(), as_of=_instant(10))
    assert [f.kind for f in outcome.failures] == [AgentFailureKind.MALFORMED_JSON]


def test_a_response_setting_both_tool_calls_and_decision_is_a_schema_violation() -> None:
    ambiguous = ModelResponse(
        tool_calls=(ToolCallRequest("get_price_quote", {"instrument_id": ALPHA_ID}),),
        decision=RawDecision((), (), ""),
    )
    model = _ScriptedModel([ambiguous])
    agent = DecisionAgent()
    outcome = agent.decide(_toolkit(), model, ConditionContext(), as_of=_instant(10))
    assert [f.kind for f in outcome.failures] == [AgentFailureKind.SCHEMA_VIOLATION]


def test_an_unknown_tool_name_is_a_schema_violation_and_the_loop_continues() -> None:
    model = _ScriptedModel(
        [
            ModelResponse(tool_calls=(ToolCallRequest("delete_everything", {}),)),
            ModelResponse(decision=RawDecision((), (), "")),
        ]
    )
    agent = DecisionAgent()
    outcome = agent.decide(_toolkit(), model, ConditionContext(), as_of=_instant(10))
    assert AgentFailureKind.SCHEMA_VIOLATION in [f.kind for f in outcome.failures]
    assert outcome.forecasts == ()


def test_exhausting_the_tool_budget_mid_turn_stops_the_loop_and_records_it() -> None:
    model = _ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCallRequest("search_instruments", {"query": ""}),
                    ToolCallRequest("get_price_quote", {"instrument_id": ALPHA_ID}),
                )
            ),
        ]
    )
    agent = DecisionAgent()
    outcome = agent.decide(_toolkit(max_calls=1), model, ConditionContext(), as_of=_instant(10))
    assert [f.kind for f in outcome.failures] == [AgentFailureKind.BUDGET_EXHAUSTED]
    assert outcome.forecasts == ()


def test_a_forecast_citing_nonexistent_evidence_is_dropped_and_recorded() -> None:
    decision = RawDecision(
        forecasts=(RawForecast(ALPHA_ID, 5, 0.6, ("nonexistent-evidence",)),),
        trade_intents=(),
        narrative="",
    )
    model = _ScriptedModel([ModelResponse(decision=decision)])
    agent = DecisionAgent()
    outcome = agent.decide(_toolkit(), model, ConditionContext(), as_of=_instant(10))
    assert outcome.forecasts == ()
    assert AgentFailureKind.NONEXISTENT_EVIDENCE in [f.kind for f in outcome.failures]


def test_a_forecast_on_an_unresolved_instrument_is_dropped_and_recorded() -> None:
    decision = RawDecision(
        forecasts=(RawForecast("hallucinated-instrument", 5, 0.6, ()),),
        trade_intents=(),
        narrative="",
    )
    model = _ScriptedModel([ModelResponse(decision=decision)])
    agent = DecisionAgent()
    outcome = agent.decide(_toolkit(), model, ConditionContext(), as_of=_instant(10))
    assert outcome.forecasts == ()
    assert AgentFailureKind.UNRESOLVED_INSTRUMENT in [f.kind for f in outcome.failures]


def test_a_forecast_with_an_out_of_range_probability_is_dropped_and_recorded() -> None:
    decision = RawDecision(
        forecasts=(RawForecast(ALPHA_ID, 5, 1.5, (PRICE_EVIDENCE_ID,)),),
        trade_intents=(),
        narrative="",
    )
    model = _ScriptedModel([ModelResponse(decision=decision)])
    agent = DecisionAgent()
    outcome = agent.decide(_toolkit(), model, ConditionContext(), as_of=_instant(10))
    assert outcome.forecasts == ()
    assert AgentFailureKind.PROBABILITY_OUT_OF_RANGE in [f.kind for f in outcome.failures]


def test_a_valid_forecast_and_trade_intent_survive_validation() -> None:
    decision = RawDecision(
        forecasts=(RawForecast(ALPHA_ID, 5, 0.6, (PRICE_EVIDENCE_ID,)),),
        trade_intents=(RawTradeIntent(ALPHA_ID, TradeSide.BUY, "test", (PRICE_EVIDENCE_ID,)),),
        narrative="",
    )
    model = _ScriptedModel([ModelResponse(decision=decision)])
    agent = DecisionAgent()
    outcome = agent.decide(_toolkit(), model, ConditionContext(), as_of=_instant(10))
    assert outcome.failures == ()
    assert len(outcome.forecasts) == 1
    assert len(outcome.trade_intents) == 1


def test_exhausting_the_turn_limit_without_a_decision_is_recorded_as_truncated() -> None:
    model = _ScriptedModel(
        [ModelResponse(tool_calls=(ToolCallRequest("search_instruments", {"query": ""}),))]
    )
    agent = DecisionAgent()
    outcome = agent.decide(
        _toolkit(max_calls=100), model, ConditionContext(max_model_turns=2), as_of=_instant(10)
    )
    assert [f.kind for f in outcome.failures] == [AgentFailureKind.TRUNCATED_OUTPUT]
    assert outcome.model_turns == 2
