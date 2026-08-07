"""Tests for the microstructure every arm trades under (§16.3, §16.4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from marketlab.core.failures import ConfigurationError
from marketlab.core.money import Money
from marketlab.execution.policy import (
    ExecutionPolicy,
    FeeSchedule,
    OrderSide,
    fill_price,
    slippage_against_mid,
)
from marketlab.instruments.types import AssetClass
from marketlab.retrieval.types import PriceQuote


def usd(amount: str) -> Money:
    return Money(Decimal(amount), "USD")


def _quote(bid: str, ask: str, volume: int = 1_000_000) -> PriceQuote:
    return PriceQuote(
        bid=Decimal(bid), ask=Decimal(ask), close=(Decimal(bid) + Decimal(ask)) / 2, volume=volume
    )


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------


def test_a_proportional_fee_is_charged_on_notional() -> None:
    schedule = FeeSchedule(basis_points=Decimal("5"), minimum=Decimal("1.00"))
    assert schedule.fee_for(usd("10000.00")) == usd("5.00")


def test_the_per_order_minimum_dominates_a_small_order() -> None:
    """What makes dust genuinely uneconomic rather than merely cheap."""
    schedule = FeeSchedule(basis_points=Decimal("5"), minimum=Decimal("1.00"))
    assert schedule.fee_for(usd("100.00")) == usd("1.00")


def test_a_negative_fee_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="rebate"):
        FeeSchedule(basis_points=Decimal("-1"), minimum=Decimal("0"))


def test_an_asset_class_without_a_declared_fee_is_refused() -> None:
    """§16.4: an order whose cost is undeclared cannot be filled honestly."""
    policy = ExecutionPolicy(fees={AssetClass.EQUITY: FeeSchedule(Decimal("5"), Decimal("1"))})
    with pytest.raises(ConfigurationError, match="No fee schedule"):
        policy.fee_schedule(AssetClass.CRYPTO_SPOT)


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------


def test_a_buy_pays_the_ask_and_a_sell_receives_the_bid() -> None:
    quote = _quote("99.95", "100.05")
    assert fill_price(quote, OrderSide.BUY) == Decimal("100.05")
    assert fill_price(quote, OrderSide.SELL) == Decimal("99.95")


def test_slippage_against_mid_is_positive_on_both_sides() -> None:
    """Crossing the spread costs money whichever way you go — a signed
    convention would let the two directions cancel in aggregate."""
    quote = _quote("99.95", "100.05")
    assert slippage_against_mid(quote, OrderSide.BUY, Decimal("10")) == Decimal("0.50")
    assert slippage_against_mid(quote, OrderSide.SELL, Decimal("10")) == Decimal("0.50")


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def _size(
    policy: ExecutionPolicy,
    equity: str,
    price: str,
    cash: str,
    asset: AssetClass = AssetClass.EQUITY,
) -> Decimal:
    return policy.buy_quantity(
        equity=usd(equity), price=usd(price), available_cash=usd(cash), asset_class=asset
    )


def test_a_buy_targets_the_configured_fraction_of_equity() -> None:
    policy = ExecutionPolicy(target_weight=Decimal("0.10"), minimum_notional=Decimal("0"))
    # 10% of 100,000 = 10,000; at 100.00 that is 100 units.
    assert _size(policy, "100000.00", "100.00", "1000000.00") == Decimal("100")


def test_sizing_is_capped_by_the_cash_actually_on_hand() -> None:
    policy = ExecutionPolicy(target_weight=Decimal("0.50"), minimum_notional=Decimal("0"))
    quantity = _size(policy, "100000.00", "100.00", "1000.00")
    # Never more than the cash can pay for, fee included.
    assert quantity * Decimal("100.00") <= Decimal("1000.00")


def test_the_fee_is_reserved_so_a_buy_cannot_overdraw() -> None:
    """The subtle failure this guards: sizing to exactly the cash balance and
    then discovering the commission does not fit."""
    schedule = FeeSchedule(basis_points=Decimal("5"), minimum=Decimal("1.00"))
    policy = ExecutionPolicy(
        target_weight=Decimal("1"),
        minimum_notional=Decimal("0"),
        fees={AssetClass.EQUITY: schedule},
    )
    cash = Decimal("1000.00")
    quantity = _size(policy, "1000000.00", "100.00", "1000.00")
    notional = usd(str(quantity * Decimal("100.00")))
    assert notional.amount + schedule.fee_for(notional).amount <= cash


def test_an_order_below_the_minimum_notional_is_sized_to_zero() -> None:
    policy = ExecutionPolicy(target_weight=Decimal("0.10"), minimum_notional=Decimal("100"))
    assert _size(policy, "500.00", "100.00", "1000000.00") == 0


def test_an_empty_wallet_sizes_to_zero() -> None:
    policy = ExecutionPolicy(minimum_notional=Decimal("0"))
    assert _size(policy, "100000.00", "100.00", "0.00") == 0


def test_a_non_positive_price_sizes_to_zero_rather_than_dividing_by_it() -> None:
    policy = ExecutionPolicy(minimum_notional=Decimal("0"))
    assert _size(policy, "100000.00", "0.00", "1000.00") == 0


def test_quantities_round_down_never_up() -> None:
    """Rounding up would buy a sliver the cash cannot cover, turning a sizing
    decision into an overdraft."""
    policy = ExecutionPolicy(target_weight=Decimal("1"), minimum_notional=Decimal("0"))
    quantity = _size(policy, "1000.00", "300.00", "1000000.00")
    assert quantity == Decimal("3.33333333")


# ---------------------------------------------------------------------------
# Liquidity
# ---------------------------------------------------------------------------


def test_the_participation_cap_limits_an_order_to_a_share_of_volume() -> None:
    policy = ExecutionPolicy(max_participation=Decimal("0.05"))
    assert policy.liquidity_cap(1000) == Decimal("50")


def test_a_market_with_no_volume_absorbs_nothing() -> None:
    assert ExecutionPolicy().liquidity_cap(0) == 0


# ---------------------------------------------------------------------------
# Configuration guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("weight", ["0", "-0.1", "1.5"])
def test_an_impossible_target_weight_is_refused(weight: str) -> None:
    with pytest.raises(ConfigurationError, match="target_weight"):
        ExecutionPolicy(target_weight=Decimal(weight))


@pytest.mark.parametrize("participation", ["0", "-0.1", "1.5"])
def test_an_impossible_participation_cap_is_refused(participation: str) -> None:
    with pytest.raises(ConfigurationError, match="max_participation"):
        ExecutionPolicy(max_participation=Decimal(participation))
