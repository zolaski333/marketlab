"""Tests for the frozen exogenous snapshot builder (§9.1, §23.2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from marketlab.core.clock import FrozenClock
from marketlab.core.failures import IntegrityError, SnapshotError, SnapshotStatus
from marketlab.core.instants import Instant, instant_from_datetime, to_datetime
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.types import RawNewsItem, RawPriceBar
from marketlab.instruments.repository import InstrumentRepository
from marketlab.instruments.types import AssetClass, ExecutionModel
from marketlab.retrieval.types import EvidenceKind
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

RUN_ID = "TEST_RUN"
ADMITTED_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
SESSION_1 = instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC))
SESSION_2 = instant_from_datetime(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))


@dataclass
class Rig:
    session: Session
    repo: InstrumentRepository
    events: EventStore
    pipeline: IngestionPipeline
    builder: SnapshotBuilder


@pytest.fixture
def rig(session: Session, clock: FrozenClock, blob_store: BlobStore) -> Rig:
    repo = InstrumentRepository(session, clock)
    events = EventStore(session, clock)
    pipeline = IngestionPipeline(
        blob_store,
        events,
        session,
        clock,
        source_id="TEST",
        licence="internal-test",
        redistributable=True,
    )
    builder = SnapshotBuilder(session, clock, blob_store, repo, events)
    return Rig(session=session, repo=repo, events=events, pipeline=pipeline, builder=builder)


def _admit(rig: Rig, ticker: str) -> str:
    view = rig.repo.admit(
        asset_class=AssetClass.EQUITY,
        ticker=ticker,
        name=f"{ticker} Inc",
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="TEST_CAL",
        settlement_days=2,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        at=ADMITTED_AT,
    )
    return view.instrument_id


def _bar(instrument_id: str, as_of: Instant, close: str = "100.00") -> RawPriceBar:
    price = Decimal(close)
    return RawPriceBar(
        instrument_id=instrument_id,
        as_of=as_of,
        bid=price - Decimal("0.05"),
        ask=price + Decimal("0.05"),
        close=price,
        volume=1000,
        first_seen_at=as_of,
    )


def test_a_complete_session_produces_a_complete_status(rig: Rig) -> None:
    alpha = _admit(rig, "ALPHA")
    beta = _admit(rig, "BETA")
    candidates = [
        SnapshotCandidate("PRICE_BAR", rig.pipeline.ingest_price_bar(_bar(alpha, SESSION_1))),
        SnapshotCandidate("PRICE_BAR", rig.pipeline.ingest_price_bar(_bar(beta, SESSION_1))),
    ]
    manifest = rig.builder.build(candidates, as_of=SESSION_1, run_id=RUN_ID)
    assert manifest.status is SnapshotStatus.COMPLETE
    assert manifest.member_count == 2


def test_a_missing_price_for_one_active_instrument_is_degraded(rig: Rig) -> None:
    alpha = _admit(rig, "ALPHA")
    _admit(rig, "BETA")  # beta gets no price bar this session
    candidates = [
        SnapshotCandidate("PRICE_BAR", rig.pipeline.ingest_price_bar(_bar(alpha, SESSION_1))),
    ]
    manifest = rig.builder.build(candidates, as_of=SESSION_1, run_id=RUN_ID)
    assert manifest.status is SnapshotStatus.DEGRADED


def test_no_price_for_any_active_instrument_is_invalid(rig: Rig) -> None:
    _admit(rig, "ALPHA")
    _admit(rig, "BETA")
    manifest = rig.builder.build([], as_of=SESSION_1, run_id=RUN_ID)
    assert manifest.status is SnapshotStatus.INVALID
    assert manifest.member_count == 0


def test_build_is_idempotent_for_an_unchanged_candidate_set(rig: Rig) -> None:
    alpha = _admit(rig, "ALPHA")
    candidates = [
        SnapshotCandidate("PRICE_BAR", rig.pipeline.ingest_price_bar(_bar(alpha, SESSION_1)))
    ]
    first = rig.builder.build(candidates, as_of=SESSION_1, run_id=RUN_ID)
    second = rig.builder.build(candidates, as_of=SESSION_1, run_id=RUN_ID)
    assert first == second


def test_build_raises_on_a_divergent_rebuild(rig: Rig) -> None:
    alpha = _admit(rig, "ALPHA")
    beta = _admit(rig, "BETA")
    first_candidates = [
        SnapshotCandidate("PRICE_BAR", rig.pipeline.ingest_price_bar(_bar(alpha, SESSION_1)))
    ]
    rig.builder.build(first_candidates, as_of=SESSION_1, run_id=RUN_ID)

    second_candidates = [
        *first_candidates,
        SnapshotCandidate("PRICE_BAR", rig.pipeline.ingest_price_bar(_bar(beta, SESSION_1))),
    ]
    with pytest.raises(IntegrityError, match="already"):
        rig.builder.build(second_candidates, as_of=SESSION_1, run_id=RUN_ID)


def test_snapshot_id_is_scoped_by_run_id(rig: Rig) -> None:
    alpha = _admit(rig, "ALPHA")
    candidates = [
        SnapshotCandidate("PRICE_BAR", rig.pipeline.ingest_price_bar(_bar(alpha, SESSION_1)))
    ]
    first = rig.builder.build(candidates, as_of=SESSION_1, run_id="RUN_A")
    second = rig.builder.build(candidates, as_of=SESSION_1, run_id="RUN_B")
    assert first.snapshot_id != second.snapshot_id


def test_load_index_round_trips_evidence_and_universe(rig: Rig) -> None:
    alpha = _admit(rig, "ALPHA")
    candidates = [
        SnapshotCandidate(
            "PRICE_BAR", rig.pipeline.ingest_price_bar(_bar(alpha, SESSION_1, close="123.45"))
        )
    ]
    manifest = rig.builder.build(candidates, as_of=SESSION_1, run_id=RUN_ID)

    index = rig.builder.load_index(manifest.snapshot_id)
    assert index.snapshot_id == manifest.snapshot_id
    assert index.cutoff == SESSION_1
    assert len(index.universe) == 1
    assert index.universe[0].instrument_id == alpha

    price = index.latest(EvidenceKind.PRICE_BAR, alpha)
    assert price is not None
    assert price.fields["close"] == "123.45"


def test_load_index_raises_for_an_unknown_snapshot_id(rig: Rig) -> None:
    with pytest.raises(SnapshotError):
        rig.builder.load_index("nonexistent")


def test_build_emits_a_snapshot_built_event(rig: Rig) -> None:
    alpha = _admit(rig, "ALPHA")
    candidates = [
        SnapshotCandidate("PRICE_BAR", rig.pipeline.ingest_price_bar(_bar(alpha, SESSION_1)))
    ]
    manifest = rig.builder.build(candidates, as_of=SESSION_1, run_id=RUN_ID)

    events = list(rig.events.iter_events(event_type="SNAPSHOT_BUILT"))
    assert len(events) == 1
    assert events[0].payload["snapshot_id"] == manifest.snapshot_id
    assert events[0].payload["status"] == "COMPLETE"


def test_late_arriving_evidence_is_excluded_from_the_session_it_arrived_too_late_for(
    rig: Rig,
) -> None:
    alpha = _admit(rig, "ALPHA")
    on_time = rig.pipeline.ingest_news_item(
        RawNewsItem(
            news_id="N1",
            instrument_ids=(alpha,),
            title="On time",
            body="...",
            source_published_at=SESSION_1,
            first_seen_at=SESSION_1,
        )
    )
    late_first_seen = instant_from_datetime(to_datetime(SESSION_1) + timedelta(minutes=30))
    late = rig.pipeline.ingest_news_item(
        RawNewsItem(
            news_id="N2",
            instrument_ids=(alpha,),
            title="Late",
            body="...",
            source_published_at=SESSION_1,
            first_seen_at=late_first_seen,
        )
    )
    price = rig.pipeline.ingest_price_bar(_bar(alpha, SESSION_1))
    candidates = [
        SnapshotCandidate("PRICE_BAR", price),
        SnapshotCandidate("NEWS_ITEM", on_time),
        SnapshotCandidate("NEWS_ITEM", late),
    ]

    same_session = rig.builder.build(candidates, as_of=SESSION_1, run_id=RUN_ID)
    index = rig.builder.load_index(same_session.snapshot_id)
    news = index.evidence_of_kind(EvidenceKind.NEWS_ITEM)
    assert [n.headline for n in news] == ["On time"]

    later = rig.builder.build(candidates, as_of=SESSION_2, run_id=RUN_ID)
    later_index = rig.builder.load_index(later.snapshot_id)
    later_news = later_index.evidence_of_kind(EvidenceKind.NEWS_ITEM)
    assert {n.headline for n in later_news} == {"On time", "Late"}
