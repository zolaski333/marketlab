"""End-to-end wiring test for task 7's deliverable: turning the durably
ingested synthetic world (task 6) into frozen snapshots, retrieval indexes,
and budgeted tool calls, session by session.

Like ``test_synthetic_universe_wiring.py``, this is deliberately not a unit
test of any one component — it exists to catch the mistakes unit tests with
hand-picked fixtures cannot: a snapshot that silently drops evidence a tool
should see, a status computation that disagrees with what the provider
actually scripted for a given session, or budget accounting that behaves
differently once real (larger, messier) evidence flows through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from marketlab.core.clock import FrozenClock
from marketlab.core.failures import BudgetError, SnapshotStatus
from marketlab.core.instants import instant_from_datetime
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.synthetic import SyntheticMarketDataProvider, admit_synthetic_universe
from marketlab.instruments.calendars import CalendarRegistry
from marketlab.instruments.repository import InstrumentRepository
from marketlab.retrieval.budget import ToolBudget
from marketlab.retrieval.tools import RetrievalToolkit
from marketlab.retrieval.types import price_quote_from_evidence
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

START_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
NUM_SESSIONS = 26  # enough to reach every scripted fixture including ticker change
RUN_ID = "SNAPSHOT_WIRING_RUN"


@dataclass(frozen=True, slots=True)
class RunResult:
    builder: SnapshotBuilder
    snapshot_ids: tuple[str, ...]  # session N is snapshot_ids[N - 1]
    alpha_id: str
    beta_id: str


@pytest.fixture
def run(session: Session, clock: FrozenClock, blob_store: BlobStore) -> RunResult:
    repo = InstrumentRepository(session, clock)
    calendars = CalendarRegistry()
    universe = admit_synthetic_universe(repo, calendars, START_AT)
    provider = SyntheticMarketDataProvider(
        equity_calendar=universe.equity_calendar,
        start_at=START_AT,
        num_sessions=NUM_SESSIONS,
        alpha_id=universe.alpha.instrument_id,
        beta_id=universe.beta.instrument_id,
        gamma_id=universe.gamma.instrument_id,
        delta_id=universe.delta.instrument_id,
    )
    events = EventStore(session, clock)
    pipeline = IngestionPipeline(
        blob_store,
        events,
        session,
        clock,
        source_id="SYNTHETIC",
        licence="internal-synthetic",
        redistributable=True,
    )
    builder = SnapshotBuilder(session, clock, blob_store, repo, events)

    candidates: list[SnapshotCandidate] = []
    snapshot_ids: list[str] = []
    for cutoff in provider.session_cutoffs():
        for bar in provider.fetch_price_bars(cutoff):
            candidates.append(SnapshotCandidate("PRICE_BAR", pipeline.ingest_price_bar(bar)))
        for item in provider.fetch_news(cutoff):
            candidates.append(SnapshotCandidate("NEWS_ITEM", pipeline.ingest_news_item(item)))
        for record in provider.fetch_macro_records(cutoff):
            candidates.append(
                SnapshotCandidate("MACRO_RECORD", pipeline.ingest_macro_record(record))
            )
        for rate in provider.fetch_fx_rates(cutoff):
            candidates.append(SnapshotCandidate("FX_RATE", pipeline.ingest_fx_rate(rate)))
        for action in provider.fetch_corporate_actions(cutoff):
            candidates.append(
                SnapshotCandidate("CORPORATE_ACTION", pipeline.ingest_corporate_action(action))
            )
            # Applying a corporate action back to the instrument repository is
            # not this task's job (dividends/splits reaching the ledger and
            # position lots is task 10's, per docs/ROADMAP.md) — but a ticker
            # change is cheap to apply here and for it, the interesting
            # property is exactly what this fixture wants to exercise: a
            # snapshot built before the change must keep showing the old
            # ticker, and one built after must show the new one.
            if action.action_type == "TICKER_CHANGE":
                repo.record_ticker_change(
                    action.instrument_id, action.details["new_ticker"], action.effective_at
                )

        manifest = builder.build(candidates, as_of=cutoff, run_id=RUN_ID)
        snapshot_ids.append(manifest.snapshot_id)

    return RunResult(
        builder=builder,
        snapshot_ids=tuple(snapshot_ids),
        alpha_id=universe.alpha.instrument_id,
        beta_id=universe.beta.instrument_id,
    )


def test_every_session_produces_a_complete_snapshot(run: RunResult) -> None:
    # Every one of the four synthetic instruments gets a fresh price bar every
    # session regardless of news/macro/fx/corporate-action content, so
    # completeness should hold throughout — the fixture-specific tests below
    # confirm that news/macro variability does *not* degrade this.
    for snapshot_id in run.snapshot_ids:
        index = run.builder.load_index(snapshot_id)
        assert index.status is SnapshotStatus.COMPLETE


def test_session_five_has_no_news_evidence(run: RunResult) -> None:
    # A snapshot is cumulative — it still legitimately carries sessions 1-4's
    # news — so what session 5 being scripted as news-free actually means is
    # no news item *dated this session*, not an empty result overall.
    index = run.builder.load_index(run.snapshot_ids[4])  # session 5
    toolkit = RetrievalToolkit(index, ToolBudget())
    news_this_session = [item for item in toolkit.search_news() if item.as_of == index.cutoff]
    assert news_this_session == []


def test_late_arriving_news_is_invisible_the_session_it_arrives_late_for(run: RunResult) -> None:
    at_session_12 = run.builder.load_index(run.snapshot_ids[11])
    at_session_13 = run.builder.load_index(run.snapshot_ids[12])

    news_at_12 = RetrievalToolkit(at_session_12, ToolBudget()).search_news(
        instrument_id=run.alpha_id
    )
    news_at_13 = RetrievalToolkit(at_session_13, ToolBudget()).search_news(
        instrument_id=run.alpha_id
    )

    assert not any("Late-arriving" in item.headline for item in news_at_12)
    assert any("Late-arriving" in item.headline for item in news_at_13)


def test_macro_revision_takes_over_from_its_session_onward(run: RunResult) -> None:
    before = run.builder.load_index(run.snapshot_ids[12])  # session 13
    after = run.builder.load_index(run.snapshot_ids[13])  # session 14, the revision session

    before_indicator = RetrievalToolkit(before, ToolBudget()).get_macro_indicator(
        "SYNTH_US_INFLATION_RATE"
    )
    after_indicator = RetrievalToolkit(after, ToolBudget()).get_macro_indicator(
        "SYNTH_US_INFLATION_RATE"
    )
    assert before_indicator is not None
    assert after_indicator is not None
    assert before_indicator.fields["revision"] == 1
    assert after_indicator.fields["revision"] == 2


def test_split_adjusted_price_flows_through_the_snapshot(run: RunResult) -> None:
    before_split = run.builder.load_index(run.snapshot_ids[16])  # session 17
    at_split = run.builder.load_index(run.snapshot_ids[17])  # session 18, the split session

    before_evidence = RetrievalToolkit(before_split, ToolBudget()).get_price_quote(run.alpha_id)
    at_evidence = RetrievalToolkit(at_split, ToolBudget()).get_price_quote(run.alpha_id)
    assert before_evidence is not None
    assert at_evidence is not None

    before_quote = price_quote_from_evidence(before_evidence)
    at_quote = price_quote_from_evidence(at_evidence)
    assert at_quote.close < before_quote.close
    assert abs(at_quote.close * Decimal("2") - before_quote.close) < Decimal("10")


def test_a_realistic_sequence_of_tool_calls_stays_within_a_generous_budget(run: RunResult) -> None:
    index = run.builder.load_index(run.snapshot_ids[-1])
    toolkit = RetrievalToolkit(index, ToolBudget())
    toolkit.get_price_quote(run.alpha_id)
    toolkit.get_price_quote(run.beta_id)
    toolkit.search_news(instrument_id=run.alpha_id)
    toolkit.get_macro_indicator("SYNTH_US_INFLATION_RATE")
    toolkit.get_fx_rate("EUR_USD")
    toolkit.get_corporate_actions(run.alpha_id)
    toolkit.search_instruments("alpha")
    assert toolkit.budget.calls_used == 7


def test_a_tight_call_budget_is_exhausted_by_the_same_sequence(run: RunResult) -> None:
    index = run.builder.load_index(run.snapshot_ids[-1])
    toolkit = RetrievalToolkit(index, ToolBudget(max_calls=2))
    toolkit.get_price_quote(run.alpha_id)
    toolkit.get_price_quote(run.beta_id)
    with pytest.raises(BudgetError):
        toolkit.search_news(instrument_id=run.alpha_id)


def test_ticker_change_is_visible_through_search_instruments_from_its_session_onward(
    run: RunResult,
) -> None:
    before = run.builder.load_index(run.snapshot_ids[22])  # session 23, before the change
    after = run.builder.load_index(run.snapshot_ids[23])  # session 24, the change session

    before_view = RetrievalToolkit(before, ToolBudget()).search_instruments("EQ_US_ALPHA")
    after_view = RetrievalToolkit(after, ToolBudget()).search_instruments("EQ_US_ALPHA_2")

    assert [v.ticker for v in before_view] == ["EQ_US_ALPHA"]
    assert [v.ticker for v in after_view] == ["EQ_US_ALPHA_2"]
