"""Daily root hashes (§24.2).

A daily root is a compact commitment to everything the platform recorded up to
the end of a given day. It is small enough to commit to Git, sign, or lodge with
a timestamping service, which is what converts "our database says so" into
evidence that the record existed in this exact form on that date.

Why the chain head is a sufficient root
---------------------------------------
Events already form a hash chain: event *n* commits to event *n-1*, which
commits to *n-2*, and so on to genesis. The head hash of a day therefore
*transitively commits to the entire history* up to that point. Publishing it
pins every earlier event: changing any one of them changes every subsequent
hash, including the published root.

A Merkle tree would additionally give compact inclusion proofs — proving one
event is in the set without revealing the rest. That is not needed here: the
full log is published with the release, so an auditor recomputes the chain
directly. The chain head is chosen for being simpler and having strictly fewer
ways to be implemented wrongly.

The root is *not* a hash of the day's events alone. It commits to the day's
boundaries and count as well, so truncating a day — dropping its final events
and recomputing — does not reproduce the published value.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.core.canonical import canonical_hash
from marketlab.core.clock import Clock
from marketlab.core.failures import IntegrityError
from marketlab.core.instants import Instant
from marketlab.storage.base import Base, HashStr, InstantStr
from marketlab.storage.events import EventRow

__all__ = ["DailyRoot", "DailyRootRow", "DailyRootService", "compute_root_hash"]


class DailyRootRow(Base):
    """A published commitment to the record as of the end of one UTC day."""

    __tablename__ = "daily_roots"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    """UTC calendar day, ``YYYY-MM-DD``."""

    first_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chain_head_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    """Hash of the last event of the day; commits transitively to all history."""

    root_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    computed_at: Mapped[str] = mapped_column(InstantStr, nullable=False)


@dataclass(frozen=True, slots=True)
class DailyRoot:
    """A daily root, independent of its storage row."""

    day: str
    first_seq: int
    last_seq: int
    event_count: int
    chain_head_hash: str
    root_hash: str


def compute_root_hash(
    *, day: str, first_seq: int, last_seq: int, event_count: int, chain_head_hash: str
) -> str:
    """Hash the day's boundary commitment."""
    return canonical_hash(
        {
            "day": day,
            "first_seq": first_seq,
            "last_seq": last_seq,
            "event_count": event_count,
            "chain_head_hash": chain_head_hash,
        }
    )


class DailyRootService:
    """Computes, records and verifies daily roots."""

    __slots__ = ("_clock", "_session")

    def __init__(self, session: Session, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    def _events_of_day(self, day: str, *, fresh: bool = False) -> Sequence[EventRow]:
        if fresh:
            # End any open read transaction so SQLite takes a new WAL snapshot.
            # An open transaction pins the database as of when it began, so a
            # verifier reusing it would re-check a stale view of history.
            self._session.rollback()
        return self._read_events_of_day(day)

    def _read_events_of_day(self, day: str) -> Sequence[EventRow]:
        # occurred_at is fixed-width canonical text, so a prefix match on the
        # date is exact and index-friendly.
        #
        # populate_existing forces a refresh from the database rather than
        # returning rows already held in the session's identity map. Without it,
        # verification would re-hash the values this process loaded earlier and
        # would therefore be blind to any change made behind it — a verifier
        # that reads its own cache verifies nothing.
        return (
            self._session.execute(
                select(EventRow)
                .where(EventRow.occurred_at.startswith(day))
                .order_by(EventRow.seq.asc())
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )

    def compute(self, day: str, *, fresh: bool = False) -> DailyRoot | None:
        """Compute the root for ``day`` without recording it.

        Returns ``None`` when the day contains no events — a day with nothing
        to commit to gets no root, rather than a root over emptiness that would
        be indistinguishable from a day whose events were deleted.

        Args:
            fresh: discard any open read transaction first, so the computation
                sees the latest committed state. Verification sets this;
                ordinary reads do not need it.
        """
        rows = self._events_of_day(day, fresh=fresh)
        if not rows:
            return None
        first, last = rows[0], rows[-1]
        return DailyRoot(
            day=day,
            first_seq=first.seq,
            last_seq=last.seq,
            event_count=len(rows),
            chain_head_hash=last.event_hash,
            root_hash=compute_root_hash(
                day=day,
                first_seq=first.seq,
                last_seq=last.seq,
                event_count=len(rows),
                chain_head_hash=last.event_hash,
            ),
        )

    def seal(self, day: str) -> DailyRoot | None:
        """Compute and persist the root for ``day``.

        Idempotent, and *conflict-detecting*: re-sealing a day whose recomputed
        root differs from the stored one raises rather than overwriting. That
        divergence means history changed after it was committed to, which is
        exactly the event this mechanism exists to surface.
        """
        computed = self.compute(day)
        if computed is None:
            return None

        stored = self._session.get(DailyRootRow, day)
        if stored is not None:
            if stored.root_hash != computed.root_hash:
                raise IntegrityError(
                    f"Daily root for {day} was already sealed as {stored.root_hash} "
                    f"but now recomputes to {computed.root_hash}: the recorded "
                    "history for that day changed after it was committed to.",
                    day=day,
                    sealed=stored.root_hash,
                    recomputed=computed.root_hash,
                )
            return computed

        self._session.add(
            DailyRootRow(
                day=computed.day,
                first_seq=computed.first_seq,
                last_seq=computed.last_seq,
                event_count=computed.event_count,
                chain_head_hash=computed.chain_head_hash,
                root_hash=computed.root_hash,
                computed_at=str(self._clock.now_instant()),
            )
        )
        self._session.commit()
        return computed

    def verify_all(self) -> int:
        """Recompute every sealed root. Returns the number verified.

        Raises:
            IntegrityError: if any sealed day no longer reproduces its root.
        """
        verified = 0
        # Read the sealed roots from a fresh snapshot, then recompute each day
        # from an equally fresh read (see ``compute(fresh=True)``).
        self._session.rollback()
        sealed = (
            self._session.execute(
                select(DailyRootRow)
                .order_by(DailyRootRow.day.asc())
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )
        for row in sealed:
            recomputed = self.compute(row.day, fresh=True)
            if recomputed is None:
                raise IntegrityError(
                    f"Daily root for {row.day} was sealed over {row.event_count} "
                    "events, but that day now has none: events were deleted.",
                    day=row.day,
                )
            if recomputed.root_hash != row.root_hash:
                raise IntegrityError(
                    f"Daily root mismatch for {row.day}: sealed {row.root_hash}, "
                    f"recomputed {recomputed.root_hash}.",
                    day=row.day,
                )
            verified += 1
        return verified

    def day_of(self, instant: Instant) -> str:
        """Return the UTC calendar day of an instant."""
        return str(instant)[:10]
