"""Sizing, fees and liquidity: the microstructure every arm trades under (§16).

Why sizing lives here and not in the agent
------------------------------------------
:class:`~marketlab.agents.decision.TradeIntent` carries a side and no size, and
that is deliberate. If the agent chose position sizes, a condition could
"outperform" by betting bigger rather than by being better calibrated — a
difference in aggression, not in judgement, and one that memory or reflection
could plausibly induce as a side effect. Sizing is therefore a fixed,
pre-registered function of the portfolio and the price, identical for every
arm, applied after the decision is sealed.

The consequence worth stating: this study measures **direction and timing
quality**, not portfolio construction. An arm cannot win by sizing, and
equally cannot demonstrate skill at sizing.

Everything here is pure
-----------------------
No database, no clock, no I/O. A fill price is a function of a quote and a
side; a fee is a function of a notional; a cap is a function of a bar's
volume. That makes the whole microstructure testable without a portfolio, and
makes it obvious by inspection that it cannot vary between conditions.

Spread is a cost, not a fee
---------------------------
A buy fills at the ask and a sell at the bid, so the spread is paid *inside*
the fill price. It is therefore not posted to the fee account — doing so would
charge it twice. :func:`slippage_against_mid` exposes it as a measurable
quantity for §17.3's attribution without double-counting it in the books.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from marketlab.core.failures import ConfigurationError
from marketlab.core.money import Money, quantize_quantity
from marketlab.instruments.types import AssetClass
from marketlab.retrieval.types import PriceQuote

__all__ = [
    "DEFAULT_POLICY",
    "ExecutionPolicy",
    "FeeSchedule",
    "OrderSide",
    "fill_price",
    "slippage_against_mid",
]


class OrderSide(StrEnum):
    """Which way an order goes. ``HOLD`` is not an order and never reaches here."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Explicit transaction costs for one asset class.

    ``basis_points`` of notional, floored at ``minimum``. A per-order minimum
    is what makes small orders genuinely uneconomic, which is the effect that
    stops an agent from being rewarded for slicing a position into dust.
    """

    basis_points: Decimal
    minimum: Decimal

    def __post_init__(self) -> None:
        if self.basis_points < 0 or self.minimum < 0:
            raise ConfigurationError(
                f"Negative fees are a rebate, not a cost: {self.basis_points}bp, "
                f"minimum {self.minimum}."
            )

    def fee_for(self, notional: Money) -> Money:
        proportional = notional.scaled_by(self.basis_points / Decimal(10_000))
        floor = Money(self.minimum, notional.currency)
        return (floor if proportional < floor else proportional).quantized()


_DEFAULT_FEES: Final[dict[AssetClass, FeeSchedule]] = {
    # Round placeholder magnitudes in the spirit of retail brokerage, not a
    # calibrated cost model — see docs/ROADMAP.md.
    AssetClass.EQUITY: FeeSchedule(basis_points=Decimal("5"), minimum=Decimal("1.00")),
    AssetClass.ETF: FeeSchedule(basis_points=Decimal("5"), minimum=Decimal("1.00")),
    AssetClass.CRYPTO_SPOT: FeeSchedule(basis_points=Decimal("10"), minimum=Decimal("0.50")),
    AssetClass.FX_SPOT: FeeSchedule(basis_points=Decimal("2"), minimum=Decimal("0.50")),
    AssetClass.PRECIOUS_METAL_SPOT: FeeSchedule(basis_points=Decimal("8"), minimum=Decimal("1.00")),
}


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """The pre-registered rules every arm trades under."""

    target_weight: Decimal = Decimal("0.10")
    """Fraction of portfolio equity a BUY aims to hold in one instrument."""

    max_participation: Decimal = Decimal("0.05")
    """Ceiling on an order's share of the execution bar's volume (§16.3). A
    virtual order that swallowed a day's whole volume would be a fill nobody
    could have obtained."""

    minimum_notional: Decimal = Decimal("100")
    """Orders below this are dropped as dust rather than filled at a size where
    the per-order fee minimum dominates the result."""

    fees: Mapping[AssetClass, FeeSchedule] = field(
        default_factory=lambda: MappingProxyType(dict(_DEFAULT_FEES))
    )

    def __post_init__(self) -> None:
        if not 0 < self.target_weight <= 1:
            raise ConfigurationError(
                f"target_weight must be in (0, 1], got {self.target_weight}: a "
                "non-positive weight never trades and a weight above 1 is leverage, "
                "which is not modelled."
            )
        if not 0 < self.max_participation <= 1:
            raise ConfigurationError(
                f"max_participation must be in (0, 1], got {self.max_participation}."
            )
        if self.minimum_notional < 0:
            raise ConfigurationError(f"minimum_notional must be >= 0, got {self.minimum_notional}")

    def fee_schedule(self, asset_class: AssetClass) -> FeeSchedule:
        try:
            return self.fees[asset_class]
        except KeyError:
            raise ConfigurationError(
                f"No fee schedule for {asset_class}: an order whose cost is "
                "undeclared cannot be filled honestly (§16.4).",
                asset_class=str(asset_class),
            ) from None

    def buy_quantity(
        self,
        *,
        equity: Money,
        price: Money,
        available_cash: Money,
        asset_class: AssetClass,
    ) -> Decimal:
        """How many units a BUY should ask for.

        The target notional is ``target_weight`` of equity, reduced to what the
        cash on hand can actually pay for *including* the fee — an order that
        overdraws is not a smaller order, it is a rejected one, and rejecting
        for a rounding error would make the policy look flakier than it is.

        Returns ``0`` when nothing can honestly be bought.
        """
        if price.amount <= 0:
            return Decimal(0)

        target = equity.amount * self.target_weight
        affordable = _affordable_notional(available_cash.amount, self.fee_schedule(asset_class))
        notional = min(target, affordable)
        if notional < self.minimum_notional:
            return Decimal(0)
        return _floor_quantity(notional / price.amount)

    def liquidity_cap(self, bar_volume: int) -> Decimal:
        """Largest quantity the execution bar could plausibly absorb."""
        return _floor_quantity(Decimal(bar_volume) * self.max_participation)


DEFAULT_POLICY: Final = ExecutionPolicy()


def fill_price(quote: PriceQuote, side: OrderSide) -> Decimal:
    """The price an order actually gets: the ask for a buy, the bid for a sell.

    Falling back to ``close`` when a side of the book is missing is deliberate
    and conservative in the wrong direction — it would understate cost — so it
    is *not* done here. A quote without the relevant side is an unfillable
    quote, and the engine rejects the order rather than inventing a price
    (§16.4).
    """
    price = quote.ask if side is OrderSide.BUY else quote.bid
    return price


def slippage_against_mid(quote: PriceQuote, side: OrderSide, quantity: Decimal) -> Decimal:
    """Cost of crossing the spread, as a positive amount.

    Not posted to the ledger — it is already inside the fill price. Exposed so
    §17.3 can decompose a result into price move, FX move and trading cost
    without double-charging.
    """
    mid = (quote.bid + quote.ask) / Decimal(2)
    executed = fill_price(quote, side)
    difference = executed - mid if side is OrderSide.BUY else mid - executed
    return difference * quantity


def _affordable_notional(cash: Decimal, schedule: FeeSchedule) -> Decimal:
    """Notional whose value plus its proportional fee fits inside ``cash``."""
    if cash <= schedule.minimum:
        return Decimal(0)
    return (cash - schedule.minimum) / (Decimal(1) + schedule.basis_points / Decimal(10_000))


def _floor_quantity(value: Decimal) -> Decimal:
    """Round a quantity *down* to the platform's unit precision.

    Down, never nearest: rounding up would buy a sliver the cash on hand
    cannot cover, turning a sizing decision into an overdraft.
    """
    if value <= 0:
        return Decimal(0)
    return quantize_quantity(value.quantize(Decimal(1).scaleb(-8), rounding=ROUND_DOWN))
