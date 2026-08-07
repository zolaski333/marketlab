"""Value types for virtual execution (§16).

Kept apart from :mod:`marketlab.execution.engine` so that tests, reports and
later analysis can talk about orders and fills without importing SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from marketlab.core.instants import Instant
from marketlab.core.money import Money
from marketlab.execution.policy import OrderSide

__all__ = [
    "ExecutionReport",
    "Fill",
    "Order",
    "Rejection",
    "RejectionReason",
]


class RejectionReason(StrEnum):
    """Why an order did not fill.

    A rejection is an *execution outcome*, not necessarily an agent error. An
    agent that says SELL on something it does not hold has not malfunctioned —
    short positions simply are not modelled — whereas an agent that orders a
    suspended instrument has (§7.5). :mod:`marketlab.execution.engine` maps
    only the latter kind onto
    :class:`~marketlab.core.failures.ObservedAgentFailure`.
    """

    NOT_TRADABLE = "NOT_TRADABLE"
    """Suspended, delisted, expired, stale or unvaluable that session (§7.5)."""

    UNSUPPORTED_EXECUTION = "UNSUPPORTED_EXECUTION"
    """No honest fill can be constructed for this instrument (§16.4)."""

    NO_EXECUTION_QUOTE = "NO_EXECUTION_QUOTE"
    """The market printed no usable bar in the execution window."""

    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    NOTHING_TO_SELL = "NOTHING_TO_SELL"
    """A sell with no open position. Not a short — shorts are not modelled."""

    BELOW_MINIMUM_SIZE = "BELOW_MINIMUM_SIZE"
    LIQUIDITY_EXHAUSTED = "LIQUIDITY_EXHAUSTED"
    """The participation cap left nothing to fill (§16.3)."""


@dataclass(frozen=True, slots=True)
class Order:
    """An intent, sized and scheduled, awaiting its execution window."""

    order_id: str
    portfolio_id: str
    bundle_id: str
    instrument_id: str
    side: OrderSide
    quantity: Decimal
    currency: str
    decided_at: Instant
    execute_after: Instant
    """First instant this may fill — strictly after the decision (§16.2)."""


@dataclass(frozen=True, slots=True)
class Fill:
    """What an order actually got."""

    fill_id: str
    order_id: str
    instrument_id: str
    side: OrderSide
    quantity: Decimal
    requested_quantity: Decimal
    price: Money
    gross: Money
    fee: Money
    slippage: Money
    """Cost of crossing the spread, already inside ``price`` — reported for
    §17.3 attribution, never posted (that would charge it twice)."""

    realized_pnl: Money
    """Non-zero only on a sell, against the FIFO cost basis consumed."""

    executed_at: Instant
    settles_at: Instant
    transaction_id: str

    @property
    def is_partial(self) -> bool:
        """Whether the liquidity cap held the fill below what was asked."""
        return self.quantity < self.requested_quantity


@dataclass(frozen=True, slots=True)
class Rejection:
    """An order that could not fill, and why."""

    order_id: str
    instrument_id: str
    reason: RejectionReason
    detail: str
    occurred_at: Instant


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Everything one execution pass did."""

    fills: tuple[Fill, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    settled_fill_ids: tuple[str, ...] = ()

    @property
    def filled_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.fills), Decimal(0))
