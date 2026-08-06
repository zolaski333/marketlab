"""The ingestion pipeline: raw provider records into the append-only store (§8.2, §8.5).

Every record a provider returns is stored as a content-addressed blob, with a
:class:`~marketlab.storage.blobs.BlobMetadataRow` and a ``DATA_INGESTED`` event
recording its provenance. This is what lets later work — the snapshot builder,
the frozen retrieval tools — reconstruct exactly what was served, and when it
first became visible, without ever calling a provider again (§P2).

Idempotent by content: ingesting the same record twice (a resumed, replayed
ingestion run, §30.6) stores nothing a second time. When that happens, this
module returns the ``first_seen_at`` that was recorded the *first* time the
exact same content was stored, not whatever the current call happened to
claim — content addressing means "first seen" genuinely is a property of the
bytes, not of any one call site, and the earliest observation is the honest
one to keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from marketlab.core.canonical import canonical_bytes
from marketlab.core.clock import Clock
from marketlab.core.instants import Instant
from marketlab.ingestion.types import (
    RawCorporateAction,
    RawFxRate,
    RawMacroRecord,
    RawNewsItem,
    RawPriceBar,
)
from marketlab.storage.blobs import BlobMetadataRow, BlobStore
from marketlab.storage.events import EventStore

__all__ = ["IngestedRecord", "IngestionPipeline"]

_MEDIA_TYPE = "application/json"


@dataclass(frozen=True, slots=True)
class IngestedRecord:
    """A raw record after being durably stored.

    This, not the original dataclass, is what a snapshot builder references:
    ``blob_hash`` and ``first_seen_at`` are exactly the two fields a snapshot
    member needs (§9.1).
    """

    blob_hash: str
    first_seen_at: Instant


class IngestionPipeline:
    """Stores raw provider records durably, with provenance."""

    __slots__ = (
        "_blobs",
        "_clock",
        "_events",
        "_licence",
        "_redistributable",
        "_session",
        "_source_id",
    )

    def __init__(
        self,
        blob_store: BlobStore,
        events: EventStore,
        session: Session,
        clock: Clock,
        *,
        source_id: str,
        licence: str,
        redistributable: bool = False,
    ) -> None:
        self._blobs = blob_store
        self._events = events
        self._session = session
        self._clock = clock
        self._source_id = source_id
        self._licence = licence
        self._redistributable = redistributable

    def ingest_price_bar(self, bar: RawPriceBar) -> IngestedRecord:
        return self._ingest(
            "PRICE_BAR",
            {
                "instrument_id": bar.instrument_id,
                "as_of": str(bar.as_of),
                "bid": str(bar.bid),
                "ask": str(bar.ask),
                "close": str(bar.close),
                "volume": bar.volume,
            },
            first_seen_at=bar.first_seen_at,
        )

    def ingest_news_item(self, item: RawNewsItem) -> IngestedRecord:
        return self._ingest(
            "NEWS_ITEM",
            {
                "news_id": item.news_id,
                "instrument_ids": list(item.instrument_ids),
                "title": item.title,
                "body": item.body,
                "source_published_at": str(item.source_published_at),
            },
            first_seen_at=item.first_seen_at,
        )

    def ingest_macro_record(self, record: RawMacroRecord) -> IngestedRecord:
        return self._ingest(
            "MACRO_RECORD",
            {
                "indicator_id": record.indicator_id,
                "value": str(record.value),
                "revision": record.revision,
                "source_published_at": str(record.source_published_at),
            },
            first_seen_at=record.first_seen_at,
        )

    def ingest_fx_rate(self, rate: RawFxRate) -> IngestedRecord:
        return self._ingest(
            "FX_RATE",
            {"pair": rate.pair, "rate": str(rate.rate), "as_of": str(rate.as_of)},
            first_seen_at=rate.first_seen_at,
        )

    def ingest_corporate_action(self, action: RawCorporateAction) -> IngestedRecord:
        return self._ingest(
            "CORPORATE_ACTION",
            {
                "instrument_id": action.instrument_id,
                "action_type": action.action_type,
                "effective_at": str(action.effective_at),
                "details": dict(action.details),
            },
            first_seen_at=action.first_seen_at,
        )

    def _ingest(
        self, record_type: str, payload: dict[str, Any], *, first_seen_at: Instant
    ) -> IngestedRecord:
        content = canonical_bytes(payload)
        ref = self._blobs.put(content)

        existing = self._session.get(BlobMetadataRow, ref.digest)
        if existing is not None:
            return IngestedRecord(
                blob_hash=ref.digest, first_seen_at=Instant(existing.first_seen_at)
            )

        ingested_at = self._clock.now_instant()
        event = self._events.append(
            "DATA_INGESTED",
            {
                "record_type": record_type,
                "blob_hash": ref.digest,
                "source_id": self._source_id,
                "first_seen_at": str(first_seen_at),
            },
            occurred_at=first_seen_at,
        )
        self._session.add(
            BlobMetadataRow(
                digest=ref.digest,
                media_type=_MEDIA_TYPE,
                size_bytes=ref.size,
                source_id=self._source_id,
                licence=self._licence,
                redistributable=self._redistributable,
                first_seen_at=str(first_seen_at),
                ingested_at=str(ingested_at),
                ingestion_event_id=event.event_id,
            )
        )
        self._session.commit()
        return IngestedRecord(blob_hash=ref.digest, first_seen_at=first_seen_at)
