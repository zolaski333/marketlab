"""Tests for assembling the market record and persisting verdicts (§20).

The pure arithmetic lives in ``test_resolution.py``. This is about the two
things that need a database: reconstructing a run's own price history out of
its frozen snapshots, and writing a verdict down exactly once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from marketlab.core.clock import FrozenClock
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.evaluation.resolution import (
    CASH_DIVIDEND,
    STOCK_SPLIT,
    ForecastSource,
    MarketRecord,
    MarketRecordBuilder,
    PendingForecast,
    ResolutionService,
    ResolutionStatus,
)
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.types import RawCorporateAction, RawPriceBar
from marketlab.instruments.repository import InstrumentRepository
from marketlab.instruments.types import AssetClass, ExecutionModel
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

RUN_ID = "RESOLUTION_RUN"
ADMITTED_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
CLOSES = ("100.00", "101.00", "99.00", "105.00", "104.00")


def session_at(index: int) -> Instant:
    return instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC) + timedelta(days=index))


class World:
    """Five sessions of one instrument, frozen snapshot by snapshot."""

    def __init__(self, session: Session, clock: FrozenClock, blobs: BlobStore) -> None:
        self.session = session
        self.events = EventStore(session, clock)
        repo = InstrumentRepository(session, clock)
        self.pipeline = IngestionPipeline(
            blobs,
            self.events,
            session,
            clock,
            source_id="TEST",
            licence="internal-test",
            redistributable=True,
        )
        self.builder = SnapshotBuilder(session, clock, blobs, repo, self.events)
        self.view = repo.admit(
            asset_class=AssetClass.EQUITY,
            ticker="ALPHA",
            name="Alpha Inc",
            quote_currency="USD",
            native_timezone="America/New_York",
            calendar_code="TEST_CAL",
            settlement_days=2,
            execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
            at=ADMITTED_AT,
        )
        self.instrument_id = self.view.instrument_id

    def build(self, *, actions: dict[int, RawCorporateAction] | None = None) -> None:
        candidates: list[SnapshotCandidate] = []
        for index, close in enumerate(CLOSES):
            cutoff = session_at(index)
            price = Decimal(close)
            candidates.append(
                SnapshotCandidate(
                    "PRICE_BAR",
                    self.pipeline.ingest_price_bar(
                        RawPriceBar(
                            instrument_id=self.instrument_id,
                            as_of=cutoff,
                            bid=price - Decimal("0.05"),
                            ask=price + Decimal("0.05"),
                            close=price,
                            volume=1000,
                            first_seen_at=cutoff,
                        )
                    ),
                )
            )
            action = (actions or {}).get(index)
            if action is not None:
                candidates.append(
                    SnapshotCandidate(
                        "CORPORATE_ACTION", self.pipeline.ingest_corporate_action(action)
                    )
                )
            self.builder.build(candidates, as_of=cutoff, run_id=RUN_ID)


@pytest.fixture
def world(session: Session, clock: FrozenClock, blob_store: BlobStore) -> World:
    return World(session, clock, blob_store)


def _record(world: World, *, through: int = len(CLOSES) - 1) -> MarketRecord:
    return MarketRecordBuilder(world.session, world.builder).build(
        RUN_ID, through=session_at(through)
    )


def _forecast(world: World, *, anchor: int, horizon: int, p: float = 0.6) -> PendingForecast:
    return PendingForecast(
        forecast_id=f"forecast-{anchor}-{horizon}".ljust(64, "0"),
        source=ForecastSource.PANEL,
        source_bundle_id="p" * 64,
        arm_id="C",
        repetition=0,
        instrument_id=world.instrument_id,
        horizon_sessions=horizon,
        probability_up=p,
        anchor_at=session_at(anchor),
    )


# ---------------------------------------------------------------------------
# Reconstructing the record
# ---------------------------------------------------------------------------


def test_the_grid_is_the_runs_own_decision_cadence(world: World) -> None:
    world.build()
    assert _record(world).grid.instants == tuple(session_at(i) for i in range(len(CLOSES)))


def test_each_close_is_filed_under_its_own_session(world: World) -> None:
    """A snapshot is cumulative — it still carries every earlier session's
    bars. Filing those under the later cutoff would resolve a forecast against
    a price from before it was made."""
    world.build()
    record = _record(world)
    for index, close in enumerate(CLOSES):
        assert record.close(world.instrument_id, session_at(index)) == Decimal(close)


def test_the_record_stops_where_it_is_told_to(world: World) -> None:
    """Resolution run mid-study must not see sessions that have not happened
    from its point of view."""
    world.build()
    record = _record(world, through=2)
    assert len(record.grid.instants) == 3
    assert record.close(world.instrument_id, session_at(4)) is None


def test_corporate_actions_are_collected_with_their_effective_instant(world: World) -> None:
    world.build(
        actions={
            2: RawCorporateAction(
                instrument_id=world.instrument_id,
                action_type=STOCK_SPLIT,
                effective_at=session_at(2),
                details={"split_ratio": "2.0"},
                first_seen_at=session_at(2),
            ),
            3: RawCorporateAction(
                instrument_id=world.instrument_id,
                action_type=CASH_DIVIDEND,
                effective_at=session_at(3),
                details={"amount_per_share": "1.50", "currency": "USD"},
                first_seen_at=session_at(3),
            ),
        }
    )
    events = _record(world).events_between(
        world.instrument_id, after=session_at(0), through=session_at(4)
    )
    assert [(event.at, event.kind, event.value) for event in events] == [
        (session_at(2), STOCK_SPLIT, Decimal("2.0")),
        (session_at(3), CASH_DIVIDEND, Decimal("1.50")),
    ]


def test_a_corporate_action_is_collected_once_not_once_per_later_snapshot(
    world: World,
) -> None:
    """The snapshot at session 4 still carries session 2's split. Collecting
    it again there would double the split factor and turn a flat return into a
    doubling."""
    world.build(
        actions={
            2: RawCorporateAction(
                instrument_id=world.instrument_id,
                action_type=STOCK_SPLIT,
                effective_at=session_at(2),
                details={"split_ratio": "2.0"},
                first_seen_at=session_at(2),
            )
        }
    )
    record = _record(world)
    assert len(record.corporate_events) == 1


# ---------------------------------------------------------------------------
# Persisting a verdict
# ---------------------------------------------------------------------------


@pytest.fixture
def service(session: Session, clock: FrozenClock) -> ResolutionService:
    return ResolutionService(session, clock, EventStore(session, clock))


def test_a_resolved_forecast_is_written_down_with_its_arithmetic(
    world: World, service: ResolutionService
) -> None:
    world.build()
    report = service.resolve([_forecast(world, anchor=0, horizon=3)], _record(world), run_id=RUN_ID)

    assert report.counts()[ResolutionStatus.RESOLVED] == 1
    rows = service.resolutions_for(RUN_ID)
    assert len(rows) == 1
    assert rows[0].status == str(ResolutionStatus.RESOLVED)
    assert rows[0].outcome_up is True
    assert Decimal(rows[0].anchor_close) == Decimal("100.00")
    assert Decimal(rows[0].target_close) == Decimal("105.00")
    assert Decimal(rows[0].total_return) == Decimal("0.05")


def test_a_pending_forecast_is_never_written_down(world: World, service: ResolutionService) -> None:
    """It is the absence of a verdict, not a verdict. Writing it would need a
    later UPDATE, which the append-only triggers refuse."""
    world.build()
    report = service.resolve([_forecast(world, anchor=3, horizon=4)], _record(world), run_id=RUN_ID)

    assert len(report.pending) == 1
    assert report.resolved == ()
    assert service.resolutions_for(RUN_ID) == ()


def test_resolving_twice_records_one_verdict(world: World, service: ResolutionService) -> None:
    world.build()
    forecast = _forecast(world, anchor=0, horizon=3)
    service.resolve([forecast], _record(world), run_id=RUN_ID)
    service.resolve([forecast], _record(world), run_id=RUN_ID)
    assert len(service.resolutions_for(RUN_ID)) == 1


def test_a_recorded_verdict_cannot_be_edited(
    world: World, service: ResolutionService, session: Session
) -> None:
    world.build()
    service.resolve([_forecast(world, anchor=0, horizon=3)], _record(world), run_id=RUN_ID)
    session.commit()
    with pytest.raises(Exception, match="append-only"):
        session.execute(text("UPDATE forecast_resolutions SET outcome_up = 0"))


def test_an_unresolvable_forecast_is_recorded_rather_than_silently_dropped(
    world: World, service: ResolutionService
) -> None:
    """How much data was lost is itself a result: an analysis that cannot say
    how many cells it dropped cannot be checked."""
    world.build()
    unknown = PendingForecast(
        forecast_id="ghost".ljust(64, "0"),
        source=ForecastSource.PANEL,
        source_bundle_id="p" * 64,
        arm_id="A",
        repetition=0,
        instrument_id="id-never-priced",
        horizon_sessions=2,
        probability_up=0.5,
        anchor_at=session_at(0),
    )
    report = service.resolve([unknown], _record(world), run_id=RUN_ID)
    assert report.counts()[ResolutionStatus.UNRESOLVABLE] == 1
    assert service.resolutions_for(RUN_ID)[0].status == str(ResolutionStatus.UNRESOLVABLE)


def test_resolution_announces_itself_in_the_event_log(
    world: World, service: ResolutionService, session: Session, clock: FrozenClock
) -> None:
    world.build()
    service.resolve([_forecast(world, anchor=0, horizon=3)], _record(world), run_id=RUN_ID)
    events = EventStore(session, clock)
    assert [event.event_type for event in events.iter_events(event_type="FORECASTS_RESOLVED")]
    assert events.verify_chain() > 0
