"""Tests for the typed, budgeted retrieval tools (§10.3, §10.6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from marketlab.core.failures import BudgetError, SnapshotStatus
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.instruments.types import (
    AssetClass,
    ExecutionModel,
    InstrumentStatus,
    InstrumentView,
)
from marketlab.retrieval.budget import ToolBudget
from marketlab.retrieval.tools import RetrievalToolkit
from marketlab.retrieval.types import Evidence, EvidenceKind, RetrievalIndex

ALPHA_ID = "id-alpha"
BETA_ID = "id-beta"


def _instant(hour: int) -> Instant:
    return instant_from_datetime(datetime(2026, 8, 1, hour, 0, tzinfo=UTC))


def _price(instrument_id: str, hour: int, close: str) -> Evidence:
    return Evidence(
        evidence_id=f"evid-price-{instrument_id}-{hour}",
        kind=EvidenceKind.PRICE_BAR,
        subject_ids=(instrument_id,),
        as_of=_instant(hour),
        first_seen_at=_instant(hour),
        blob_hash="a" * 64,
        headline=f"{instrument_id} close={close}",
        fields={"bid": "149.90", "ask": "150.10", "close": close, "volume": 1000},
    )


def _news(instrument_id: str, hour: int, title: str, body: str) -> Evidence:
    return Evidence(
        evidence_id=f"evid-news-{instrument_id}-{hour}-{title}",
        kind=EvidenceKind.NEWS_ITEM,
        subject_ids=(instrument_id,),
        as_of=_instant(hour),
        first_seen_at=_instant(hour),
        blob_hash="b" * 64,
        headline=title,
        fields={"title": title, "body": body},
    )


def _alpha_view() -> InstrumentView:
    return InstrumentView(
        instrument_id=ALPHA_ID,
        asset_class=AssetClass.EQUITY,
        version_number=1,
        ticker="EQ_US_ALPHA",
        name="Alpha Corp",
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="SYNTH_US_EQUITY",
        settlement_days=2,
        status=InstrumentStatus.ACTIVE,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        effective_from=_instant(0),
    )


@pytest.fixture
def index() -> RetrievalIndex:
    return RetrievalIndex(
        snapshot_id="snap-1",
        cutoff=_instant(20),
        status=SnapshotStatus.COMPLETE,
        universe=(_alpha_view(),),
        evidence=(
            _price(ALPHA_ID, 10, "150.00"),
            _price(ALPHA_ID, 20, "151.00"),
            _news(ALPHA_ID, 6, "Alpha strength", "Alpha Corp posted strong growth."),
        ),
    )


def test_get_price_quote_returns_the_latest_bar_and_charges_budget(index: RetrievalIndex) -> None:
    budget = ToolBudget()
    toolkit = RetrievalToolkit(index, budget)
    result = toolkit.get_price_quote(ALPHA_ID)
    assert result is not None
    assert result.fields["close"] == "151.00"
    assert budget.calls_used == 1
    assert budget.evidence_chars_used > 0


def test_get_price_quote_on_an_unknown_instrument_returns_none_but_still_charges_a_call(
    index: RetrievalIndex,
) -> None:
    budget = ToolBudget()
    toolkit = RetrievalToolkit(index, budget)
    result = toolkit.get_price_quote("nonexistent")
    assert result is None
    assert budget.calls_used == 1
    assert budget.evidence_chars_used == 0


def test_search_news_filters_by_query_and_instrument(index: RetrievalIndex) -> None:
    toolkit = RetrievalToolkit(index, ToolBudget())
    assert len(toolkit.search_news("growth")) == 1
    assert toolkit.search_news("nonexistent keyword") == ()
    assert len(toolkit.search_news(instrument_id=ALPHA_ID)) == 1
    assert toolkit.search_news(instrument_id=BETA_ID) == ()


def test_search_news_respects_the_limit(index: RetrievalIndex) -> None:
    toolkit = RetrievalToolkit(index, ToolBudget())
    assert toolkit.search_news(limit=0) == ()


def test_search_instruments_never_returns_more_than_the_frozen_universe(
    index: RetrievalIndex,
) -> None:
    toolkit = RetrievalToolkit(index, ToolBudget())
    results = toolkit.search_instruments("alpha")
    assert [v.instrument_id for v in results] == [ALPHA_ID]


def test_get_macro_indicator_and_get_fx_rate_return_none_when_absent(
    index: RetrievalIndex,
) -> None:
    toolkit = RetrievalToolkit(index, ToolBudget())
    assert toolkit.get_macro_indicator("SYNTH_US_INFLATION_RATE") is None
    assert toolkit.get_fx_rate("EUR_USD") is None


def test_get_corporate_actions_returns_empty_tuple_when_none_exist(index: RetrievalIndex) -> None:
    toolkit = RetrievalToolkit(index, ToolBudget())
    assert toolkit.get_corporate_actions(ALPHA_ID) == ()


def test_exhausting_the_call_budget_raises_on_the_next_tool_call(index: RetrievalIndex) -> None:
    toolkit = RetrievalToolkit(index, ToolBudget(max_calls=1))
    toolkit.get_price_quote(ALPHA_ID)
    with pytest.raises(BudgetError):
        toolkit.get_fx_rate("EUR_USD")


def test_exhausting_the_char_budget_raises(index: RetrievalIndex) -> None:
    toolkit = RetrievalToolkit(index, ToolBudget(max_calls=100, max_evidence_chars=1))
    with pytest.raises(BudgetError):
        toolkit.get_price_quote(ALPHA_ID)


def test_every_toolkit_call_shares_one_running_budget(index: RetrievalIndex) -> None:
    budget = ToolBudget(max_calls=10)
    toolkit = RetrievalToolkit(index, budget)
    toolkit.get_price_quote(ALPHA_ID)
    toolkit.search_news()
    toolkit.get_corporate_actions(ALPHA_ID)
    assert budget.calls_used == 3
