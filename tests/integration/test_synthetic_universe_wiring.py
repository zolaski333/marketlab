"""End-to-end wiring test for task-6's deliverable: instrument admission,
calendars, the synthetic provider, and the ingestion pipeline all working
together over a realistic multi-session run.

This is deliberately not a unit test of any one component — each of those is
covered elsewhere. It exists to catch integration mistakes that unit tests
with hand-picked fake ids cannot: a mismatch between what
``admit_synthetic_universe`` admits and what ``SyntheticMarketDataProvider``
expects, a calendar code that does not round-trip through the registry, or an
ingested record whose instrument_id does not actually resolve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from marketlab.core.clock import FrozenClock
from marketlab.core.cutoff import Cutoff
from marketlab.core.instants import instant_from_datetime
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.synthetic import (
    SyntheticMarketDataProvider,
    SyntheticUniverse,
    admit_synthetic_universe,
)
from marketlab.instruments.calendars import CalendarRegistry
from marketlab.instruments.repository import InstrumentRepository, compute_tradability
from marketlab.instruments.types import TradabilityStatus
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

START_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
NUM_SESSIONS = 26  # enough to reach every scripted fixture including ticker change


@dataclass(frozen=True, slots=True)
class WiredWorld:
    repo: InstrumentRepository
    universe: SyntheticUniverse
    provider: SyntheticMarketDataProvider
    pipeline: IngestionPipeline


@pytest.fixture
def wired_world(session: Session, clock: FrozenClock, blob_store: BlobStore) -> WiredWorld:
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
    pipeline = IngestionPipeline(
        blob_store,
        EventStore(session, clock),
        session,
        clock,
        source_id="SYNTHETIC",
        licence="internal-synthetic",
        redistributable=True,
    )
    return WiredWorld(repo=repo, universe=universe, provider=provider, pipeline=pipeline)


def test_every_admitted_instrument_resolves_at_session_one(wired_world: WiredWorld) -> None:
    first_cutoff = Cutoff(as_of=wired_world.provider.session_cutoffs()[0])
    for view in (
        wired_world.universe.alpha,
        wired_world.universe.beta,
        wired_world.universe.gamma,
        wired_world.universe.delta,
    ):
        assert wired_world.repo.resolve(view.instrument_id, first_cutoff) is not None


def test_full_run_ingests_every_price_bar_without_error(wired_world: WiredWorld) -> None:
    total_bars = 0
    for cutoff in wired_world.provider.session_cutoffs():
        for bar in wired_world.provider.fetch_price_bars(cutoff):
            record = wired_world.pipeline.ingest_price_bar(bar)
            assert record.first_seen_at == cutoff
            total_bars += 1
    assert total_bars == NUM_SESSIONS * 4  # four instruments every session


def test_full_run_ingests_news_macro_fx_and_corporate_actions(wired_world: WiredWorld) -> None:
    news_count = 0
    corporate_action_types: set[str] = set()

    for cutoff in wired_world.provider.session_cutoffs():
        for item in wired_world.provider.fetch_news(cutoff):
            wired_world.pipeline.ingest_news_item(item)
            news_count += 1
        for record in wired_world.provider.fetch_macro_records(cutoff):
            wired_world.pipeline.ingest_macro_record(record)
        for rate in wired_world.provider.fetch_fx_rates(cutoff):
            wired_world.pipeline.ingest_fx_rate(rate)
        for action in wired_world.provider.fetch_corporate_actions(cutoff):
            wired_world.pipeline.ingest_corporate_action(action)
            corporate_action_types.add(action.action_type)

    # session 5 has none, session 12 has two -> at least NUM_SESSIONS - 1 items
    assert news_count >= NUM_SESSIONS - 1
    assert corporate_action_types == {"CASH_DIVIDEND", "STOCK_SPLIT", "TICKER_CHANGE"}


def test_ticker_change_can_be_applied_back_to_the_instrument_repository(
    wired_world: WiredWorld,
) -> None:
    """The raw corporate action this task emits is exactly what a later task's
    corporate-actions processor will feed back into the repository built
    earlier in this same task — verifying the two halves actually fit."""
    cutoffs = wired_world.provider.session_cutoffs()

    ticker_change_cutoff = cutoffs[23]  # session 24
    actions = wired_world.provider.fetch_corporate_actions(ticker_change_cutoff)
    assert len(actions) == 1
    action = actions[0]
    assert action.action_type == "TICKER_CHANGE"

    updated = wired_world.repo.record_ticker_change(
        action.instrument_id, action.details["new_ticker"], action.effective_at
    )
    assert updated.ticker == "EQ_US_ALPHA_2"

    # And the pre-change ticker is still resolvable at a cutoff before it.
    before = wired_world.repo.resolve(action.instrument_id, Cutoff(as_of=cutoffs[22]))
    assert before is not None
    assert before.ticker == "EQ_US_ALPHA"


def test_gamma_is_tradable_with_its_own_currency_and_calendar(wired_world: WiredWorld) -> None:
    cutoff = Cutoff(as_of=wired_world.provider.session_cutoffs()[0])
    gamma = wired_world.repo.search("gamma", cutoff)[0]
    assert gamma.quote_currency == "EUR"
    assert gamma.calendar_code == "SYNTH_EU_EQUITY"


def test_delta_trades_on_the_always_open_crypto_calendar(wired_world: WiredWorld) -> None:
    cutoff = Cutoff(as_of=wired_world.provider.session_cutoffs()[0])
    delta = wired_world.repo.search("delta", cutoff)[0]
    assert delta.calendar_code == "SYNTH_CRYPTO"
    assert delta.settlement_days == 0


def test_all_four_instruments_start_tradable(wired_world: WiredWorld) -> None:
    cutoff = Cutoff(as_of=wired_world.provider.session_cutoffs()[0])
    for view in wired_world.repo.search("", cutoff):
        status = compute_tradability(view, has_tradable_quote=True, data_is_stale=False)
        assert status == TradabilityStatus.TRADABLE
