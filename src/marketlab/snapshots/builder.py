"""Builds and loads the frozen exogenous snapshot (§9.1, §23.2).

A :class:`SnapshotBuilder` turns durably-ingested raw records (see
:mod:`marketlab.ingestion.pipeline`) into a **frozen, point-in-time-consistent
manifest** that every arm of one cycle reads from — A, B, C, D and the B'/C'
placebos alike. That sharing is the whole scientific point: if every
condition sees byte-identical evidence at the same cutoff, a difference in
their decisions cannot be attributed to a difference in what they were shown.

Two steps, two audiences
-------------------------
:meth:`SnapshotBuilder.build` runs *before* any decision path code executes.
It holds a database session, decodes raw blobs, resolves the instrument
universe, and persists a :class:`SnapshotRow` plus one :class:`SnapshotMemberRow`
per included fact. Its return value, :class:`SnapshotManifest`, is what a
cycle runner logs — not what an agent reads.

:meth:`SnapshotBuilder.load_index` reconstructs a
:class:`~marketlab.retrieval.types.RetrievalIndex` from what was persisted —
the plain, SQLAlchemy-free object that ``agents``/``retrieval``/``forecasting``
are actually allowed to hold (see ``tests/security/test_decision_path_isolation.py``).
Building and loading are deliberately separate methods rather than one
build-and-return-index call: a replay (§12.5) needs to reload an
already-frozen snapshot without re-deciding what belongs in it.

Why the caller supplies candidates instead of this module rediscovering them
------------------------------------------------------------------------------
:meth:`build` takes the sequence of :class:`SnapshotCandidate` the calling
cycle driver already has in hand — typically the accumulated
:class:`~marketlab.ingestion.pipeline.IngestedRecord` results of every
``pipeline.ingest_*`` call made so far in the run, session after session —
rather than rescanning the event log for ``DATA_INGESTED`` events itself. The
driver already holds this list at essentially no cost (it just produced every
record by calling the pipeline directly), and threading it through avoids a
second point-in-time filtering pass with its own chance to disagree with the
first. ``as_of``-based filtering (§6.2) is still re-applied defensively inside
:meth:`build` rather than trusted from the caller — cheap insurance, and
consistent with every other read on the decision path never taking a cutoff
on faith.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.core.canonical import canonical_bytes, canonical_hash
from marketlab.core.clock import Clock
from marketlab.core.cutoff import Cutoff
from marketlab.core.failures import (
    ConfigurationError,
    IntegrityError,
    SnapshotError,
    SnapshotStatus,
)
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant, parse_instant
from marketlab.ingestion.pipeline import IngestedRecord
from marketlab.instruments.repository import InstrumentRepository
from marketlab.instruments.types import AssetClass, ExecutionModel, InstrumentStatus, InstrumentView
from marketlab.retrieval.types import Evidence, EvidenceKind, RetrievalIndex
from marketlab.storage.base import Base, HashStr, InstantStr, ShortStr
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

__all__ = [
    "SnapshotBuilder",
    "SnapshotCandidate",
    "SnapshotManifest",
    "SnapshotMemberRow",
    "SnapshotRow",
]


class SnapshotRow(Base):
    """One frozen snapshot's identity and audit summary."""

    __tablename__ = "snapshots"

    snapshot_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    run_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    as_of: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    status: Mapped[str] = mapped_column(ShortStr, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    universe_blob_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    built_at: Mapped[str] = mapped_column(InstantStr, nullable=False)


class SnapshotMemberRow(Base):
    """One piece of evidence included in one snapshot.

    ``evidence_id`` is content-derived from ``(record_type, blob_hash)`` alone
    (see :mod:`marketlab.retrieval.types`), so the same fact reuses the same
    id across every snapshot that includes it; ``member_id`` is the join
    between that stable fact and this particular snapshot.
    """

    __tablename__ = "snapshot_members"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "evidence_id", name="uq_snapshot_members_evidence"),
    )

    member_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    evidence_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(ShortStr, nullable=False)
    blob_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    first_seen_at: Mapped[str] = mapped_column(InstantStr, nullable=False)


@dataclass(frozen=True, slots=True)
class SnapshotCandidate:
    """One raw record a cycle driver has already ingested and now offers to
    a snapshot. ``record_type`` matches :class:`~marketlab.retrieval.types.EvidenceKind`
    exactly (``"PRICE_BAR"``, ``"NEWS_ITEM"``, ...)."""

    record_type: str
    record: IngestedRecord


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """A built snapshot's identity and audit summary.

    Not what the decision path reads — see
    :class:`~marketlab.retrieval.types.RetrievalIndex` for that. This is what
    a cycle runner logs and what ``marketlab audit verify`` inspects.
    """

    snapshot_id: str
    run_id: str
    as_of: Instant
    status: SnapshotStatus
    member_count: int
    manifest_hash: str


class SnapshotBuilder:
    """Builds, persists, and reloads frozen exogenous snapshots."""

    __slots__ = ("_blobs", "_clock", "_events", "_instruments", "_session")

    def __init__(
        self,
        session: Session,
        clock: Clock,
        blob_store: BlobStore,
        instruments: InstrumentRepository,
        events: EventStore,
    ) -> None:
        self._session = session
        self._clock = clock
        self._blobs = blob_store
        self._instruments = instruments
        self._events = events

    # -- building ------------------------------------------------------------

    def build(
        self,
        candidates: Sequence[SnapshotCandidate],
        *,
        as_of: Instant,
        run_id: str,
    ) -> SnapshotManifest:
        """Freeze the point-in-time view as of ``as_of`` for ``run_id``.

        Idempotent and conflict-detecting, the same way
        :meth:`marketlab.audit.roots.DailyRootService.seal` is: calling this
        twice with the same ``(run_id, as_of)`` and an unchanged candidate set
        is a no-op that returns the original manifest (safe to retry after an
        interrupted cycle, §30.6); calling it with a *different* candidate set
        raises, because that means the visible evidence changed after the
        snapshot was already frozen and cited.

        Raises:
            IntegrityError: on a divergent rebuild.
        """
        cutoff = Cutoff(as_of=as_of)
        universe = self._instruments.search("", cutoff)

        visible_by_blob: dict[str, SnapshotCandidate] = {}
        for candidate in candidates:
            if candidate.record.first_seen_at <= as_of:
                visible_by_blob.setdefault(candidate.record.blob_hash, candidate)

        evidence = [
            _decode_evidence(
                candidate.record_type,
                candidate.record.blob_hash,
                candidate.record.first_seen_at,
                json.loads(self._blobs.get(candidate.record.blob_hash)),
            )
            for candidate in visible_by_blob.values()
        ]
        evidence.sort(key=lambda item: (item.as_of, item.evidence_id))

        snapshot_id = derive_id(IdKind.SNAPSHOT, run_id=run_id, as_of=str(as_of))
        status = _compute_status(universe, evidence, as_of)
        manifest_hash = _compute_manifest_hash(snapshot_id, as_of, universe, evidence)

        existing = self._session.get(SnapshotRow, snapshot_id)
        if existing is not None:
            if existing.manifest_hash != manifest_hash:
                raise IntegrityError(
                    f"Snapshot {snapshot_id} (run {run_id}, as_of {as_of}) was already "
                    f"built with manifest hash {existing.manifest_hash}, but recomputes "
                    f"to {manifest_hash}: the evidence visible at this cutoff changed "
                    "after the snapshot was frozen.",
                    snapshot_id=snapshot_id,
                )
            return SnapshotManifest(
                snapshot_id=snapshot_id,
                run_id=run_id,
                as_of=as_of,
                status=SnapshotStatus(existing.status),
                member_count=existing.member_count,
                manifest_hash=manifest_hash,
            )

        universe_blob = self._blobs.put(
            canonical_bytes([_instrument_view_to_payload(view) for view in universe])
        )

        self._session.add(
            SnapshotRow(
                snapshot_id=snapshot_id,
                run_id=run_id,
                as_of=str(as_of),
                status=str(status),
                member_count=len(evidence),
                manifest_hash=manifest_hash,
                universe_blob_hash=universe_blob.digest,
                built_at=str(self._clock.now_instant()),
            )
        )
        for item in evidence:
            member_id = derive_id(
                IdKind.SNAPSHOT_MEMBER, snapshot_id=snapshot_id, evidence_id=item.evidence_id
            )
            self._session.add(
                SnapshotMemberRow(
                    member_id=member_id,
                    snapshot_id=snapshot_id,
                    evidence_id=item.evidence_id,
                    record_type=str(item.kind),
                    blob_hash=item.blob_hash,
                    first_seen_at=str(item.first_seen_at),
                )
            )
        # EventStore.append commits internally; the SnapshotRow/SnapshotMemberRow
        # adds above ride along in that same commit rather than needing one of
        # their own, so the snapshot and its audit event land atomically.
        self._events.append(
            "SNAPSHOT_BUILT",
            {
                "snapshot_id": snapshot_id,
                "status": str(status),
                "member_count": len(evidence),
                "manifest_hash": manifest_hash,
            },
            occurred_at=as_of,
            run_id=run_id,
        )
        return SnapshotManifest(
            snapshot_id=snapshot_id,
            run_id=run_id,
            as_of=as_of,
            status=status,
            member_count=len(evidence),
            manifest_hash=manifest_hash,
        )

    # -- loading ---------------------------------------------------------

    def load_index(self, snapshot_id: str) -> RetrievalIndex:
        """Reconstruct the decision-path-safe :class:`RetrievalIndex` for a
        snapshot already built by :meth:`build`.

        Raises:
            SnapshotError: if no snapshot with this id was ever recorded.
        """
        row = self._session.get(SnapshotRow, snapshot_id)
        if row is None:
            raise SnapshotError(
                f"No snapshot recorded with id {snapshot_id}.", snapshot_id=snapshot_id
            )

        members = (
            self._session.execute(
                select(SnapshotMemberRow).where(SnapshotMemberRow.snapshot_id == snapshot_id)
            )
            .scalars()
            .all()
        )
        evidence = [
            _decode_evidence(
                member.record_type,
                member.blob_hash,
                Instant(member.first_seen_at),
                json.loads(self._blobs.get(member.blob_hash)),
            )
            for member in members
        ]
        evidence.sort(key=lambda item: (item.as_of, item.evidence_id))

        universe_payload = json.loads(self._blobs.get(row.universe_blob_hash))
        universe = tuple(_payload_to_instrument_view(entry) for entry in universe_payload)

        return RetrievalIndex(
            snapshot_id=snapshot_id,
            cutoff=Instant(row.as_of),
            status=SnapshotStatus(row.status),
            universe=universe,
            evidence=tuple(evidence),
        )


# -- decoding -----------------------------------------------------------------


def _decode_evidence(
    record_type: str, blob_hash: str, first_seen_at: Instant, payload: Mapping[str, Any]
) -> Evidence:
    try:
        kind = EvidenceKind(record_type)
    except ValueError as exc:
        raise ConfigurationError(f"Unknown snapshot record_type: {record_type!r}") from exc

    subject_ids: tuple[str, ...]
    if kind is EvidenceKind.PRICE_BAR:
        subject_ids = (str(payload["instrument_id"]),)
        as_of = parse_instant(str(payload["as_of"]))
        headline = f"{payload['instrument_id']} close={payload['close']}"
    elif kind is EvidenceKind.NEWS_ITEM:
        subject_ids = tuple(str(i) for i in payload["instrument_ids"])
        as_of = parse_instant(str(payload["source_published_at"]))
        headline = str(payload["title"])
    elif kind is EvidenceKind.MACRO_RECORD:
        subject_ids = (str(payload["indicator_id"]),)
        as_of = parse_instant(str(payload["source_published_at"]))
        headline = f"{payload['indicator_id']}={payload['value']} (revision {payload['revision']})"
    elif kind is EvidenceKind.FX_RATE:
        subject_ids = (str(payload["pair"]),)
        as_of = parse_instant(str(payload["as_of"]))
        headline = f"{payload['pair']}={payload['rate']}"
    else:
        subject_ids = (str(payload["instrument_id"]),)
        as_of = parse_instant(str(payload["effective_at"]))
        headline = f"{payload['instrument_id']} {payload['action_type']}"

    evidence_id = derive_id(IdKind.EVIDENCE, record_type=record_type, blob_hash=blob_hash)
    return Evidence(
        evidence_id=evidence_id,
        kind=kind,
        subject_ids=subject_ids,
        as_of=as_of,
        first_seen_at=first_seen_at,
        blob_hash=blob_hash,
        headline=headline,
        fields=payload,
    )


def _instrument_view_to_payload(view: InstrumentView) -> dict[str, Any]:
    return {
        "instrument_id": view.instrument_id,
        "asset_class": str(view.asset_class),
        "version_number": view.version_number,
        "ticker": view.ticker,
        "name": view.name,
        "quote_currency": view.quote_currency,
        "native_timezone": view.native_timezone,
        "calendar_code": view.calendar_code,
        "settlement_days": view.settlement_days,
        "status": str(view.status),
        "execution_model": str(view.execution_model),
        "effective_from": str(view.effective_from),
    }


def _payload_to_instrument_view(payload: Mapping[str, Any]) -> InstrumentView:
    return InstrumentView(
        instrument_id=str(payload["instrument_id"]),
        asset_class=AssetClass(payload["asset_class"]),
        version_number=int(payload["version_number"]),
        ticker=str(payload["ticker"]),
        name=str(payload["name"]),
        quote_currency=str(payload["quote_currency"]),
        native_timezone=str(payload["native_timezone"]),
        calendar_code=str(payload["calendar_code"]),
        settlement_days=int(payload["settlement_days"]),
        status=InstrumentStatus(payload["status"]),
        execution_model=ExecutionModel(payload["execution_model"]),
        effective_from=parse_instant(str(payload["effective_from"])),
    )


def _compute_manifest_hash(
    snapshot_id: str,
    as_of: Instant,
    universe: Sequence[InstrumentView],
    evidence: Sequence[Evidence],
) -> str:
    universe_payload = sorted(
        (_instrument_view_to_payload(view) for view in universe),
        key=lambda payload: str(payload["instrument_id"]),
    )
    return canonical_hash(
        {
            "snapshot_id": snapshot_id,
            "as_of": str(as_of),
            "universe": universe_payload,
            "evidence_ids": sorted(item.evidence_id for item in evidence),
        }
    )


def _compute_status(
    universe: Sequence[InstrumentView], evidence: Sequence[Evidence], as_of: Instant
) -> SnapshotStatus:
    """Daily completeness, in the same spirit as
    :func:`marketlab.instruments.repository.compute_tradability`: pure,
    deterministic, precedence-ordered.

    This is an interpretation of §23.2, not a quotation of it: no fresh price
    for *any* actively-tradable instrument is treated as ``INVALID`` (there is
    nothing to decide on), a partial gap as ``DEGRADED`` (usable, flagged),
    and news/macro/FX absence as neither — real markets have news-free
    sessions, and the synthetic world scripts one deliberately (session 5).
    Recorded as an open interpretation in ``docs/ROADMAP.md`` pending
    confirmation from the study owner.
    """
    active_ids = {view.instrument_id for view in universe if view.status is InstrumentStatus.ACTIVE}
    if not active_ids:
        return SnapshotStatus.INVALID

    fresh_priced_ids = {
        subject_id
        for item in evidence
        if item.kind is EvidenceKind.PRICE_BAR and item.as_of == as_of
        for subject_id in item.subject_ids
    }
    missing = active_ids - fresh_priced_ids
    if not missing:
        return SnapshotStatus.COMPLETE
    if missing == active_ids:
        return SnapshotStatus.INVALID
    return SnapshotStatus.DEGRADED
