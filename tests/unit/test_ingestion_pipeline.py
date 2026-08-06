"""Tests for the ingestion pipeline (§8.2, §8.5, §30.6)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from marketlab.core.clock import FrozenClock
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.types import RawFxRate, RawNewsItem, RawPriceBar
from marketlab.storage.blobs import BlobMetadataRow, BlobStore
from marketlab.storage.events import EventStore

AS_OF = instant_from_datetime(datetime(2026, 8, 3, 16, 0, tzinfo=UTC))


@pytest.fixture
def pipeline(session: Session, clock: FrozenClock, blob_store: BlobStore) -> IngestionPipeline:
    return IngestionPipeline(
        blob_store,
        EventStore(session, clock),
        session,
        clock,
        source_id="SYNTHETIC",
        licence="internal-synthetic",
        redistributable=True,
    )


def make_bar(as_of: Instant = AS_OF, close: str = "150.00") -> RawPriceBar:
    return RawPriceBar(
        instrument_id="id-alpha",
        as_of=as_of,
        bid=Decimal("149.90"),
        ask=Decimal("150.10"),
        close=Decimal(close),
        volume=1000,
        first_seen_at=as_of,
    )


def test_ingesting_a_bar_stores_it_as_a_blob(
    pipeline: IngestionPipeline, blob_store: BlobStore
) -> None:
    record = pipeline.ingest_price_bar(make_bar())
    assert blob_store.exists(record.blob_hash)
    assert record.first_seen_at == AS_OF


def test_blob_metadata_records_provenance(pipeline: IngestionPipeline, session: Session) -> None:
    record = pipeline.ingest_price_bar(make_bar())
    meta = session.get(BlobMetadataRow, record.blob_hash)
    assert meta is not None
    assert meta.source_id == "SYNTHETIC"
    assert meta.licence == "internal-synthetic"
    assert meta.redistributable is True
    assert meta.first_seen_at == str(AS_OF)
    assert meta.media_type == "application/json"
    assert meta.size_bytes > 0


def test_ingestion_is_recorded_in_the_event_log(
    pipeline: IngestionPipeline, session: Session, clock: FrozenClock
) -> None:
    record = pipeline.ingest_price_bar(make_bar())
    events = list(EventStore(session, clock).iter_events(event_type="DATA_INGESTED"))
    assert len(events) == 1
    assert events[0].payload["blob_hash"] == record.blob_hash
    assert events[0].payload["record_type"] == "PRICE_BAR"


def test_ingesting_identical_content_twice_stores_one_blob(
    pipeline: IngestionPipeline, session: Session
) -> None:
    """Replaying an interrupted ingestion run must not duplicate anything (§30.6)."""
    first = pipeline.ingest_price_bar(make_bar())
    second = pipeline.ingest_price_bar(make_bar())
    assert first == second

    metas = session.query(BlobMetadataRow).all()
    assert len(metas) == 1


def test_ingesting_identical_content_twice_does_not_duplicate_events(
    pipeline: IngestionPipeline, session: Session, clock: FrozenClock
) -> None:
    pipeline.ingest_price_bar(make_bar())
    pipeline.ingest_price_bar(make_bar())
    events = list(EventStore(session, clock).iter_events(event_type="DATA_INGESTED"))
    assert len(events) == 1


def test_the_first_observed_first_seen_at_wins_on_replay(
    pipeline: IngestionPipeline,
) -> None:
    """Content addressing means "first seen" is a property of the bytes, not
    of any one call site — a later call claiming a different first_seen_at for
    identical content does not get to rewrite when it was actually first seen."""
    bar = make_bar()
    first = pipeline.ingest_price_bar(bar)

    later_claim = instant_from_datetime(datetime(2026, 8, 3, 18, 0, tzinfo=UTC))
    second = pipeline.ingest_price_bar(replace(bar, first_seen_at=later_claim))

    assert second.first_seen_at == first.first_seen_at == AS_OF


def test_distinct_content_produces_distinct_blobs(pipeline: IngestionPipeline) -> None:
    first = pipeline.ingest_price_bar(make_bar(close="150.00"))
    second = pipeline.ingest_price_bar(make_bar(close="151.00"))
    assert first.blob_hash != second.blob_hash


def test_news_item_content_round_trips_through_the_blob_store(
    pipeline: IngestionPipeline, blob_store: BlobStore
) -> None:
    """The raw document is recoverable byte-for-byte, unaltered (§8.6)."""
    item = RawNewsItem(
        news_id="NEWS_S001",
        instrument_ids=("id-alpha", "id-beta"),
        title="Routine update",
        body="Nothing notable happened.",
        source_published_at=AS_OF,
        first_seen_at=AS_OF,
    )
    record = pipeline.ingest_news_item(item)

    import json

    stored = json.loads(blob_store.get(record.blob_hash))
    assert stored["news_id"] == "NEWS_S001"
    assert stored["title"] == "Routine update"
    assert stored["instrument_ids"] == ["id-alpha", "id-beta"]


def test_fx_rate_is_ingested_with_its_own_first_seen_at(pipeline: IngestionPipeline) -> None:
    later = instant_from_datetime(datetime(2026, 8, 3, 16, 5, tzinfo=UTC))
    rate = RawFxRate(pair="EUR_USD", rate=Decimal("1.0850"), as_of=AS_OF, first_seen_at=later)
    record = pipeline.ingest_fx_rate(rate)
    assert record.first_seen_at == later
