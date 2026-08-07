"""Position lots and cost basis (§17.1, §17.4).

Why lots are folded, not mutated
--------------------------------
The obvious design is a ``position_lots`` table with a ``quantity_remaining``
column decremented on every sale. It is also the design §P4 forbids: that
column is an in-place edit of a past scientific fact, and the append-only
triggers would refuse it — correctly.

So the stored thing is the **event**, not the state. Every opening and closing
is one immutable :class:`PositionEventRow`; the open lots at any instant are
computed by folding the events up to it. This is the same move
:mod:`marketlab.instruments.repository` makes by refusing to store
``effective_to``, and it buys the same property: "what did the book look like
at instant *t*" is answerable for every *t*, not just for now.

Lot selection is FIFO
---------------------
Which lot a sale consumes determines the realised gain, so it is a
pre-registered choice, not an implementation detail. FIFO is used throughout:
deterministic, the most common real convention, and — unlike a
tax-optimising rule — it cannot be tuned after seeing results. Every arm uses
it, so it cannot advantage one condition over another.

Fractional quantities
---------------------
Quantities are :class:`~decimal.Decimal`, quantised to
``marketlab.core.money.QUANTITY_DECIMALS``. Crypto is fractional by nature; a
share-count integer would either forbid it or silently truncate it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.core.failures import AccountingError
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant
from marketlab.core.money import (
    Currency,
    Money,
    decimal_from_str,
    decimal_to_str,
    quantize_quantity,
)
from marketlab.storage.base import Base, DecimalStr, HashStr, InstantStr, ShortStr

__all__ = [
    "Lot",
    "LotConsumption",
    "PositionBook",
    "PositionEventRow",
]

_SEQUENCE_RANK: Final[Mapping[str, int]] = {
    "SPLIT_OUT": 1,
    "SPLIT_IN": 2,
    "CLOSE": 3,
    "OPEN": 4,
}
"""Order of position events *within one instant*.

Every event of one session carries that session's cutoff, so the timestamp
alone cannot order them and the fold would be free to replay them in any
order. That is not a cosmetic concern: a split closes a lot at the quantity it
had when the split ran, so replaying a same-session sale *before* the split
subtracts from that lot twice, drives it negative, and silently drops it —
losing its cost basis outright.

The ranks therefore encode the order the cycle actually performs:

1-2. Corporate actions, applied before the session's fills. Buying on a
     dividend's ex-date does not entitle you to it, and a split re-denominates
     the position you *held*, not the one you are about to buy.
3.   Sales, which consume lots that existed before this session.
4.   Purchases, whose lots are therefore not available to same-session sales —
     which matches reality, since T+N settlement means they are not yours to
     sell yet either.
"""


class PositionEventRow(Base):
    """One immutable change to a position: an opening or a closing.

    ``quantity_delta`` is positive when opening and negative when closing.
    ``lot_id`` ties a closing back to the opening it consumes, so cost basis
    stays attributable rather than averaged.
    """

    __tablename__ = "position_events"

    event_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    instrument_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    lot_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    occurred_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    sequence: Mapped[str] = mapped_column(ShortStr, nullable=False)
    """Order within one instant. See :data:`_SEQUENCE_RANK`."""

    quantity_delta: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    unit_cost: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    currency: Mapped[str] = mapped_column(ShortStr, nullable=False)
    reason: Mapped[str] = mapped_column(ShortStr, nullable=False)
    """``FILL``, ``SPLIT_OUT``, ``SPLIT_IN`` — why the position moved."""

    reference_id: Mapped[str] = mapped_column(ShortStr, nullable=False, default="")


@dataclass(frozen=True, slots=True)
class Lot:
    """One open parcel of an instrument, carried at the cost it was opened at."""

    lot_id: str
    instrument_id: str
    opened_at: Instant
    quantity: Decimal
    unit_cost: Decimal
    currency: Currency

    @property
    def cost_basis(self) -> Money:
        return Money(self.quantity * self.unit_cost, self.currency)


@dataclass(frozen=True, slots=True)
class LotConsumption:
    """How much of one lot a sale consumed, and at what cost."""

    lot_id: str
    quantity: Decimal
    unit_cost: Decimal
    currency: Currency

    @property
    def cost_basis(self) -> Money:
        return Money(self.quantity * self.unit_cost, self.currency)


class PositionBook:
    """Records and folds position events for one study database."""

    __slots__ = ("_session",)

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- writing -------------------------------------------------------------

    def open_lot(
        self,
        *,
        portfolio_id: str,
        instrument_id: str,
        quantity: Decimal,
        unit_cost: Decimal,
        currency: Currency,
        occurred_at: Instant,
        reason: str,
        reference_id: str,
    ) -> str:
        """Record an opening and return the new lot id.

        Raises:
            AccountingError: on a non-positive quantity. An "opening" of zero
                or less is a closing wearing the wrong name, and would corrupt
                the FIFO order it inserts itself into.
        """
        quantity = quantize_quantity(quantity)
        if quantity <= 0:
            raise AccountingError(
                f"Cannot open a lot of {quantity} in {instrument_id}: an opening "
                "must be strictly positive.",
                instrument_id=instrument_id,
                quantity=str(quantity),
            )
        lot_id = derive_id(
            IdKind.POSITION_EVENT,
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            occurred_at=str(occurred_at),
            reference_id=reference_id,
            reason=reason,
        )
        self._append(
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            lot_id=lot_id,
            occurred_at=occurred_at,
            sequence=f"{_SEQUENCE_RANK['OPEN']}-{reason}",
            quantity_delta=quantity,
            unit_cost=unit_cost,
            currency=currency,
            reason=reason,
            reference_id=reference_id,
        )
        return lot_id

    def close_quantity(
        self,
        *,
        portfolio_id: str,
        instrument_id: str,
        quantity: Decimal,
        occurred_at: Instant,
        reason: str,
        reference_id: str,
    ) -> tuple[LotConsumption, ...]:
        """Consume ``quantity`` from the oldest open lots first.

        Returns what each lot contributed, so the caller can compute realised
        P&L against the actual cost basis rather than an average.

        Raises:
            AccountingError: if the open position is smaller than ``quantity``.
                Short selling is not modelled (see ``docs/ROADMAP.md``), so an
                oversized close is a bug, not a short.
        """
        quantity = quantize_quantity(quantity)
        if quantity <= 0:
            raise AccountingError(
                f"Cannot close {quantity} of {instrument_id}: a closing must be strictly positive.",
                instrument_id=instrument_id,
                quantity=str(quantity),
            )

        open_lots = self.open_lots(portfolio_id, instrument_id, as_of=occurred_at)
        available = sum((lot.quantity for lot in open_lots), Decimal(0))
        if quantity > available:
            raise AccountingError(
                f"Cannot close {quantity} of {instrument_id}: only {available} is "
                "open. Short positions are not modelled, so this is an "
                "over-close rather than a short sale.",
                instrument_id=instrument_id,
                requested=str(quantity),
                available=str(available),
            )

        consumptions: list[LotConsumption] = []
        remaining = quantity
        for index, lot in enumerate(open_lots):
            if remaining <= 0:
                break
            taken = min(remaining, lot.quantity)
            remaining -= taken
            consumptions.append(
                LotConsumption(
                    lot_id=lot.lot_id,
                    quantity=taken,
                    unit_cost=lot.unit_cost,
                    currency=lot.currency,
                )
            )
            self._append(
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                lot_id=lot.lot_id,
                occurred_at=occurred_at,
                sequence=f"{_SEQUENCE_RANK['CLOSE']}-{reason}-{index}",
                quantity_delta=-taken,
                unit_cost=lot.unit_cost,
                currency=lot.currency,
                reason=reason,
                reference_id=reference_id,
            )
        return tuple(consumptions)

    def apply_split(
        self,
        *,
        portfolio_id: str,
        instrument_id: str,
        ratio: Decimal,
        occurred_at: Instant,
        reference_id: str,
    ) -> tuple[Lot, ...]:
        """Re-denominate every open lot by ``ratio``, preserving cost basis.

        A split changes how many units a holding is expressed in, not what it
        is worth: a 2-for-1 doubles the count and halves the unit cost, so the
        position account never moves and no ledger entry is produced. Booking
        one would invent a gain out of a renaming.

        Each lot is closed and reopened *individually* rather than the position
        being collapsed and reissued as one parcel. Merging them would preserve
        the total cost basis but destroy the FIFO ordering, quietly changing
        the realised gain of every future sale.

        Raises:
            AccountingError: on a non-positive ratio.
        """
        if ratio <= 0:
            raise AccountingError(
                f"Split ratio must be positive, got {ratio}.",
                instrument_id=instrument_id,
                ratio=str(ratio),
            )
        lots = self.open_lots(portfolio_id, instrument_id, as_of=occurred_at)
        for index, lot in enumerate(lots):
            self._append(
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                lot_id=lot.lot_id,
                occurred_at=occurred_at,
                sequence=f"{_SEQUENCE_RANK['SPLIT_OUT']}-SPLIT_OUT-{index}",
                quantity_delta=-lot.quantity,
                unit_cost=lot.unit_cost,
                currency=lot.currency,
                reason="SPLIT_OUT",
                reference_id=reference_id,
            )
            self._append(
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                lot_id=derive_id(
                    IdKind.POSITION_EVENT,
                    portfolio_id=portfolio_id,
                    predecessor_lot_id=lot.lot_id,
                    reference_id=reference_id,
                ),
                occurred_at=occurred_at,
                sequence=f"{_SEQUENCE_RANK['SPLIT_IN']}-SPLIT_IN-{index}",
                quantity_delta=lot.quantity * ratio,
                unit_cost=lot.unit_cost / ratio,
                currency=lot.currency,
                reason="SPLIT_IN",
                reference_id=reference_id,
            )
        return self.open_lots(portfolio_id, instrument_id, as_of=occurred_at)

    def _append(
        self,
        *,
        portfolio_id: str,
        instrument_id: str,
        lot_id: str,
        occurred_at: Instant,
        sequence: str,
        quantity_delta: Decimal,
        unit_cost: Decimal,
        currency: Currency,
        reason: str,
        reference_id: str,
    ) -> None:
        event_id = derive_id(
            IdKind.POSITION_EVENT,
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            lot_id=lot_id,
            occurred_at=str(occurred_at),
            sequence=sequence,
            quantity_delta=str(quantity_delta),
            reference_id=reference_id,
        )
        if self._session.get(PositionEventRow, event_id) is not None:
            return
        self._session.add(
            PositionEventRow(
                event_id=event_id,
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                lot_id=lot_id,
                occurred_at=str(occurred_at),
                sequence=sequence,
                # decimal_to_str, never str(): quantising a tiny crypto
                # quantity yields e.g. Decimal("0E-8"), whose str() is
                # scientific notation — the exact form
                # marketlab.core.money exists to keep out of storage.
                quantity_delta=decimal_to_str(quantize_quantity(quantity_delta)),
                unit_cost=decimal_to_str(unit_cost),
                currency=currency,
                reason=reason,
                reference_id=reference_id,
            )
        )
        self._session.flush()

    # -- reading -------------------------------------------------------------

    def open_lots(
        self, portfolio_id: str, instrument_id: str, *, as_of: Instant | None = None
    ) -> tuple[Lot, ...]:
        """Open lots in FIFO order — oldest first — as of an instant."""
        query = (
            select(PositionEventRow)
            .where(PositionEventRow.portfolio_id == portfolio_id)
            .where(PositionEventRow.instrument_id == instrument_id)
            .order_by(PositionEventRow.occurred_at.asc(), PositionEventRow.sequence.asc())
        )
        if as_of is not None:
            query = query.where(PositionEventRow.occurred_at <= str(as_of))
        return _fold(list(self._session.execute(query).scalars()))

    def quantity_of(
        self, portfolio_id: str, instrument_id: str, *, as_of: Instant | None = None
    ) -> Decimal:
        """Total open quantity of one instrument."""
        return sum(
            (lot.quantity for lot in self.open_lots(portfolio_id, instrument_id, as_of=as_of)),
            Decimal(0),
        )

    def held_instruments(
        self, portfolio_id: str, *, as_of: Instant | None = None
    ) -> tuple[str, ...]:
        """Every instrument with a non-zero open position, sorted."""
        query = select(PositionEventRow.instrument_id).where(
            PositionEventRow.portfolio_id == portfolio_id
        )
        if as_of is not None:
            query = query.where(PositionEventRow.occurred_at <= str(as_of))
        candidates = sorted(set(self._session.execute(query).scalars()))
        return tuple(
            instrument_id
            for instrument_id in candidates
            if self.quantity_of(portfolio_id, instrument_id, as_of=as_of) > 0
        )


def _fold(events: Sequence[PositionEventRow]) -> tuple[Lot, ...]:
    """Replay events into the open lots they leave behind.

    Closings are matched to their lot by ``lot_id`` rather than by order, so a
    fold over a partially-consumed history reconstructs exactly the cost basis
    the closing was computed against.
    """
    quantities: dict[str, Decimal] = {}
    opened: dict[str, Lot] = {}
    order: list[str] = []

    for event in events:
        delta = decimal_from_str(event.quantity_delta)
        if event.lot_id not in quantities:
            quantities[event.lot_id] = Decimal(0)
            order.append(event.lot_id)
            opened[event.lot_id] = Lot(
                lot_id=event.lot_id,
                instrument_id=event.instrument_id,
                opened_at=Instant(event.occurred_at),
                quantity=Decimal(0),
                unit_cost=decimal_from_str(event.unit_cost),
                currency=event.currency,
            )
        quantities[event.lot_id] += delta

    return tuple(
        Lot(
            lot_id=lot_id,
            instrument_id=opened[lot_id].instrument_id,
            opened_at=opened[lot_id].opened_at,
            quantity=quantities[lot_id],
            unit_cost=opened[lot_id].unit_cost,
            currency=opened[lot_id].currency,
        )
        for lot_id in order
        if quantities[lot_id] > 0
    )
