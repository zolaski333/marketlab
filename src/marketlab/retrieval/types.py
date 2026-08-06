"""Frozen, decision-path-safe retrieval types (§9.1, §10.2).

Everything here is a plain dataclass — no SQLAlchemy import anywhere in this
module (enforced by ``tests/security/test_decision_path_isolation.py``). A
:class:`RetrievalIndex` is built once, outside the decision path, by
``marketlab.snapshots.builder.SnapshotBuilder.load_index`` — which does hold a
database session — and handed in as an already-frozen object. By the time any
agent/retrieval/forecasting code touches it, the point-in-time filtering has
already happened; there is no query left to write that could forget it.

``Evidence`` is the single, uniform currency for every kind of exogenous fact
(§8.1's five raw record kinds), so one frozen tuple can be searched, filtered
and cited uniformly rather than five parallel, kind-specific collections.
``subject_ids`` generalises "what this is about" across kinds that are
naturally singular (one instrument, one FX pair, one macro indicator) and one
that is not (a news item can name several instruments, or none).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from marketlab.core.failures import SnapshotStatus
from marketlab.core.instants import Instant
from marketlab.instruments.types import InstrumentView

__all__ = [
    "Evidence",
    "EvidenceKind",
    "PriceQuote",
    "RetrievalIndex",
    "price_quote_from_evidence",
]


class EvidenceKind(StrEnum):
    """Mirrors the ``record_type`` tags written by
    :class:`marketlab.ingestion.pipeline.IngestionPipeline` exactly, so a
    stored blob's provenance decodes back to the same kind without a lookup
    table."""

    PRICE_BAR = "PRICE_BAR"
    NEWS_ITEM = "NEWS_ITEM"
    MACRO_RECORD = "MACRO_RECORD"
    FX_RATE = "FX_RATE"
    CORPORATE_ACTION = "CORPORATE_ACTION"


@dataclass(frozen=True, slots=True)
class Evidence:
    """One exogenous fact, frozen into a snapshot and citable by id.

    ``evidence_id`` is derived from ``(kind, blob_hash)`` alone (§16.7), so the
    same underlying fact carries the same id in every snapshot that includes
    it — a claim citing it stays valid however many later cycles also see it.
    ``as_of`` is the fact's own domain time (when the price/news/rate is
    *for*); ``first_seen_at`` is when the platform first observed the bytes
    behind it. A macro revision is a *different* ``Evidence`` — later
    ``first_seen_at``, same ``subject_ids`` — never a mutation of the earlier
    one (§6.2, §P4).
    """

    evidence_id: str
    kind: EvidenceKind
    subject_ids: tuple[str, ...]
    as_of: Instant
    first_seen_at: Instant
    blob_hash: str
    headline: str
    fields: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """Typed numeric view of a ``PRICE_BAR`` evidence item's ``fields``."""

    bid: Decimal
    ask: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class RetrievalIndex:
    """The frozen, point-in-time view every arm of one cycle reads from.

    Immutable and shared by identity across all arms (§9.1): A/B/C/D and the
    B'/C' placebos are handed the exact same object for a given cycle, so a
    difference in their decisions cannot be attributed to seeing different
    evidence. Query methods are linear scans over a few hundred items at
    Phase 1 scale — deliberately not indexed behind a cache dict, since the
    correctness of a hand-rolled cache would itself need verifying against
    the same tests a plain scan already passes trivially.
    """

    snapshot_id: str
    cutoff: Instant
    status: SnapshotStatus
    universe: tuple[InstrumentView, ...]
    evidence: tuple[Evidence, ...]

    def resolve_instrument(self, instrument_id: str) -> InstrumentView | None:
        """Exact lookup within the frozen universe. Never fuzzy — mirrors
        :meth:`marketlab.instruments.repository.InstrumentRepository.resolve`."""
        for view in self.universe:
            if view.instrument_id == instrument_id:
                return view
        return None

    def search_instruments(self, query: str) -> tuple[InstrumentView, ...]:
        """Fuzzy ticker/name discovery, for the ``search_instruments`` tool only —
        never used to back an order."""
        needle = query.strip().lower()
        matches = [
            view
            for view in self.universe
            if needle in view.ticker.lower() or needle in view.name.lower()
        ]
        matches.sort(key=lambda v: v.instrument_id)
        return tuple(matches)

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        """Exact lookup by citation id — what claim verification (§14.5) checks
        a cited ``evidence_id`` against to catch ``NONEXISTENT_EVIDENCE``."""
        for item in self.evidence:
            if item.evidence_id == evidence_id:
                return item
        return None

    def evidence_of_kind(
        self, kind: EvidenceKind, *, subject_id: str | None = None
    ) -> tuple[Evidence, ...]:
        """Every evidence item of ``kind``, oldest first, optionally narrowed to
        one subject (an instrument id, an FX pair, a macro indicator id)."""
        matches = [item for item in self.evidence if item.kind is kind]
        if subject_id is not None:
            matches = [item for item in matches if subject_id in item.subject_ids]
        matches.sort(key=lambda item: item.as_of)
        return tuple(matches)

    def latest(self, kind: EvidenceKind, subject_id: str) -> Evidence | None:
        """The most recent evidence of ``kind`` about ``subject_id``, by its own
        domain time — e.g. the latest macro revision visible as of this index's
        cutoff, not necessarily the one most recently ingested."""
        matches = self.evidence_of_kind(kind, subject_id=subject_id)
        return matches[-1] if matches else None

    def search(self, query: str) -> tuple[Evidence, ...]:
        """Keyword search over every evidence item's headline and text fields,
        oldest first. An empty query returns everything (§10.2's frozen index
        must be browsable, not just searchable)."""
        needle = query.strip().lower()
        if not needle:
            return self.evidence
        matches = [item for item in self.evidence if _evidence_matches(item, needle)]
        matches.sort(key=lambda item: item.as_of)
        return tuple(matches)


def _evidence_matches(item: Evidence, needle: str) -> bool:
    if needle in item.headline.lower():
        return True
    return any(needle in value.lower() for value in item.fields.values() if isinstance(value, str))


def price_quote_from_evidence(evidence: Evidence) -> PriceQuote:
    """Decode a ``PRICE_BAR`` evidence item's ``fields`` into typed numbers.

    A free function rather than an ``Evidence`` method: most evidence kinds
    have no numeric decode, and a method that raises for four out of five
    kinds is a worse shape than a function callers only reach for when they
    already know they have a price bar.

    Raises:
        ValueError: if ``evidence`` is not a ``PRICE_BAR``.
    """
    if evidence.kind is not EvidenceKind.PRICE_BAR:
        raise ValueError(f"Not a PRICE_BAR: {evidence.evidence_id} is {evidence.kind}")
    fields = evidence.fields
    return PriceQuote(
        bid=Decimal(str(fields["bid"])),
        ask=Decimal(str(fields["ask"])),
        close=Decimal(str(fields["close"])),
        volume=int(fields["volume"]),
    )
