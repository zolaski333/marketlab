"""The append-only event log and its hash chain (§P4, §24.1).

This is the source of truth. Analytical tables, Parquet extracts and figures are
derivatives that can be dropped and rebuilt (§24.3); the event log cannot.

Chain design
------------
Each event carries ``previous_hash``, ``payload_hash`` and ``event_hash``, so
altering any historical event breaks every link after it.

The ordering the chain follows is a **monotonic sequence number**, not a
timestamp. That distinction is the whole point of this module:

* Several events routinely share one timestamp — the arms of a cycle are all
  stamped with the same cutoff. Ordering by timestamp leaves their relative
  order undefined, so the writer may link to an arbitrary "previous" event and
  the verifier may walk them in a different order, reporting corruption in an
  intact chain (or, worse, accepting a reordered one).
* ``seq`` is included *inside* the hashed material, so reordering rows is itself
  detectable rather than merely being prevented by convention.

Concurrency
-----------
Appending reads the current head and writes its successor. The chain cannot fork
under concurrency because ``seq`` is the primary key: two appenders that both
read head *N* would both attempt to insert *N+1*, and the database rejects the
second. The loser retries against the new head. Correctness rests on the
constraint rather than on a lock, which leaves reads free to run concurrently
under WAL (see :mod:`marketlab.storage.database`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Integer, String, UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError as SqlIntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.core.canonical import canonical_hash, canonical_json
from marketlab.core.clock import Clock
from marketlab.core.failures import IntegrityError
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant
from marketlab.storage.base import Base, HashStr, InstantStr, JsonStr, ShortStr

__all__ = [
    "GENESIS_HASH",
    "EventRecord",
    "EventRow",
    "EventStore",
    "compute_event_hash",
]

GENESIS_HASH: Final = "0" * 64
"""Parent of the first event. A fixed sentinel, distinguishable from any digest."""

_APPEND_ATTEMPTS: Final = 8
"""Retries when a concurrent appender wins the race for the next ``seq``."""


class EventRow(Base):
    """One immutable scientific event."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_events_event_id"),)

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """Monotonic chain position. Defines order; never reused."""

    event_id: Mapped[str] = mapped_column(HashStr, nullable=False)
    """Content-derived identity, used for idempotent re-append."""

    event_type: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)

    occurred_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    """When the described thing happened, in domain time."""

    recorded_at: Mapped[str] = mapped_column(InstantStr, nullable=False)
    """When the platform wrote the row, in wall-clock time."""

    payload_json: Mapped[str] = mapped_column(JsonStr, nullable=False)
    payload_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    previous_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    event_hash: Mapped[str] = mapped_column(HashStr, nullable=False)

    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    arm_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    repetition: Mapped[int | None] = mapped_column(Integer, nullable=True)


@dataclass(frozen=True, slots=True)
class EventRecord:
    """An event as read back from the log."""

    seq: int
    event_id: str
    event_type: str
    occurred_at: Instant
    recorded_at: Instant
    payload: Any
    payload_hash: str
    previous_hash: str
    event_hash: str


def compute_event_hash(
    *,
    seq: int,
    event_id: str,
    event_type: str,
    occurred_at: str,
    recorded_at: str,
    payload_hash: str,
    previous_hash: str,
) -> str:
    """Hash the material that binds an event to its position in the chain.

    ``seq`` is part of the material, so a row moved to a different position no
    longer validates.
    """
    return canonical_hash(
        {
            "seq": seq,
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
            "payload_hash": payload_hash,
            "previous_hash": previous_hash,
        }
    )


def _hash_stored_json(payload_json: str) -> str:
    """Hash a stored canonical JSON document.

    The stored text is already canonical, so it is hashed as bytes rather than
    parsed and re-serialised. Re-serialising would mask a payload edited into a
    *different but still canonical* form — exactly the tampering the chain
    exists to detect.
    """
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _to_record(row: EventRow) -> EventRecord:
    return EventRecord(
        seq=row.seq,
        event_id=row.event_id,
        event_type=row.event_type,
        occurred_at=Instant(row.occurred_at),
        recorded_at=Instant(row.recorded_at),
        payload=json.loads(row.payload_json),
        payload_hash=row.payload_hash,
        previous_hash=row.previous_hash,
        event_hash=row.event_hash,
    )


class EventStore:
    """Appends to, and verifies, the scientific event log."""

    __slots__ = ("_clock", "_session")

    def __init__(self, session: Session, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    # -- writing -----------------------------------------------------------

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        occurred_at: Instant,
        *,
        run_id: str | None = None,
        cycle_id: str | None = None,
        arm_id: str | None = None,
        repetition: int | None = None,
    ) -> EventRecord:
        """Append one event and return it.

        Idempotent by ``event_id``: an event whose type, domain time, routing
        keys and payload are identical to one already recorded is *not* appended
        twice. This is what makes an interrupted cycle resumable without
        duplicating history (§30.6).
        """
        event_id = derive_id(
            IdKind.EVENT,
            event_type=event_type,
            occurred_at=str(occurred_at),
            run_id=run_id,
            cycle_id=cycle_id,
            arm_id=arm_id,
            repetition=repetition,
            payload=dict(payload),
        )
        payload_json = canonical_json(payload)
        payload_hash = canonical_hash(payload)

        for _ in range(_APPEND_ATTEMPTS):
            existing = self._session.execute(
                select(EventRow).where(EventRow.event_id == event_id)
            ).scalar_one_or_none()
            if existing is not None:
                return _to_record(existing)

            head = self._session.execute(
                select(EventRow).order_by(EventRow.seq.desc()).limit(1)
            ).scalar_one_or_none()
            previous_hash = head.event_hash if head is not None else GENESIS_HASH
            seq = (head.seq + 1) if head is not None else 1
            recorded_at = self._clock.now_instant()

            row = EventRow(
                seq=seq,
                event_id=event_id,
                event_type=event_type,
                occurred_at=str(occurred_at),
                recorded_at=str(recorded_at),
                payload_json=payload_json,
                payload_hash=payload_hash,
                previous_hash=previous_hash,
                event_hash=compute_event_hash(
                    seq=seq,
                    event_id=event_id,
                    event_type=event_type,
                    occurred_at=str(occurred_at),
                    recorded_at=str(recorded_at),
                    payload_hash=payload_hash,
                    previous_hash=previous_hash,
                ),
                run_id=run_id,
                cycle_id=cycle_id,
                arm_id=arm_id,
                repetition=repetition,
            )
            self._session.add(row)
            try:
                self._session.commit()
            except SqlIntegrityError:
                # Another appender claimed this seq (or this exact event) first.
                # Roll back and retry against the new head; the primary key on
                # ``seq`` is what makes a forked chain impossible.
                self._session.rollback()
                continue
            return _to_record(row)

        raise IntegrityError(
            f"Could not append event {event_type} after {_APPEND_ATTEMPTS} attempts: "
            "the chain head kept moving under concurrent writers.",
            event_type=event_type,
        )

    # -- reading -----------------------------------------------------------

    def head(self) -> EventRecord | None:
        """Return the most recent event, or ``None`` on an empty log."""
        row = self._session.execute(
            select(EventRow).order_by(EventRow.seq.desc()).limit(1)
        ).scalar_one_or_none()
        return _to_record(row) if row is not None else None

    def count(self) -> int:
        total = self._session.execute(select(func.count()).select_from(EventRow)).scalar_one()
        return int(total)

    def iter_events(
        self,
        *,
        event_type: str | None = None,
        from_seq: int = 1,
    ) -> Iterator[EventRecord]:
        """Yield events in chain order."""
        query = select(EventRow).where(EventRow.seq >= from_seq).order_by(EventRow.seq.asc())
        if event_type is not None:
            query = query.where(EventRow.event_type == event_type)
        for row in self._session.execute(query).scalars():
            yield _to_record(row)

    # -- verification ------------------------------------------------------

    def verify_chain(self) -> int:
        """Walk the whole chain, re-deriving every hash. Returns events checked.

        Raises:
            IntegrityError: on the first inconsistency, naming its position.

        Any transaction already open on the session is rolled back first. Under
        WAL, an open read transaction pins a consistent snapshot taken when it
        began, so verification would otherwise re-check the state as of some
        earlier moment and miss everything written since. Verification is about
        what is *committed on disk now*, so it deliberately discards pending
        session state to get a fresh snapshot.
        """
        self._session.rollback()

        expected_previous = GENESIS_HASH
        expected_seq = 1
        checked = 0

        # populate_existing forces a read from the database instead of the
        # session's identity map. Verifying cached copies of rows this process
        # already loaded would re-derive hashes from values held in memory and
        # miss any modification made behind it.
        rows = self._session.execute(
            select(EventRow).order_by(EventRow.seq.asc()).execution_options(populate_existing=True)
        ).scalars()
        for row in rows:
            if row.seq != expected_seq:
                raise IntegrityError(
                    f"Event sequence gap: expected seq {expected_seq}, found {row.seq}. "
                    "The log is append-only, so a gap means rows were removed.",
                    expected_seq=expected_seq,
                    found_seq=row.seq,
                )
            if row.previous_hash != expected_previous:
                raise IntegrityError(
                    f"Chain broken at seq {row.seq}: previous_hash {row.previous_hash} "
                    f"does not match the preceding event hash {expected_previous}.",
                    seq=row.seq,
                )

            actual_payload_hash = _hash_stored_json(row.payload_json)
            if actual_payload_hash != row.payload_hash:
                raise IntegrityError(
                    f"Payload altered at seq {row.seq}: stored hash {row.payload_hash}, "
                    f"actual {actual_payload_hash}.",
                    seq=row.seq,
                )

            actual_event_hash = compute_event_hash(
                seq=row.seq,
                event_id=row.event_id,
                event_type=row.event_type,
                occurred_at=row.occurred_at,
                recorded_at=row.recorded_at,
                payload_hash=row.payload_hash,
                previous_hash=row.previous_hash,
            )
            if actual_event_hash != row.event_hash:
                raise IntegrityError(
                    f"Event hash mismatch at seq {row.seq}: stored {row.event_hash}, "
                    f"actual {actual_event_hash}.",
                    seq=row.seq,
                )

            expected_previous = row.event_hash
            expected_seq += 1
            checked += 1

        return checked
