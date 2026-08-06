"""Property tests for exact monetary arithmetic (§17.2, §34.10)."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from marketlab.core import money as money_module
from marketlab.core.money import (
    Money,
    decimal_from_str,
    decimal_to_str,
    quantize_quantity,
    quantum_for,
    register_currency,
)

# Amounts spanning the range a portfolio actually reaches, including the round
# magnitudes where scientific notation appears.
amounts = st.decimals(
    min_value=Decimal("-1e9"),
    max_value=Decimal("1e9"),
    allow_nan=False,
    allow_infinity=False,
    places=8,
)

PLAIN_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")


@given(amounts)
def test_serialisation_never_uses_scientific_notation(value: Decimal) -> None:
    """The stored text is always plain positional notation.

    This is the exact defect that put ``'8E+4'`` into a previous ledger: the
    value round-trips numerically but is unreadable in an audit and sorts
    nonsensically as text.
    """
    text = decimal_to_str(value)
    assert PLAIN_DECIMAL.match(text), f"non-positional serialisation: {text!r}"
    assert "E" not in text.upper()


@given(amounts)
def test_decimal_round_trip_is_exact(value: Decimal) -> None:
    assert decimal_from_str(decimal_to_str(value)) == value


def test_normalize_is_the_trap_this_module_avoids() -> None:
    """Regression guard: document the behaviour that caused the defect."""
    assert str(Decimal("80000.00").normalize()) == "8E+4"
    assert decimal_to_str(Decimal("80000.00")) == "80000.00"


@given(amounts, amounts)
def test_addition_is_exact_and_commutative(left: Decimal, right: Decimal) -> None:
    a = Money(left, "USD")
    b = Money(right, "USD")
    assert (a + b).amount == left + right
    assert (a + b) == (b + a)


@given(amounts, amounts, amounts)
def test_addition_is_associative(x: Decimal, y: Decimal, z: Decimal) -> None:
    """Exactness means no reassociation error — unlike binary floats."""
    a, b, c = Money(x, "USD"), Money(y, "USD"), Money(z, "USD")
    assert ((a + b) + c).amount == (a + (b + c)).amount


@given(amounts)
def test_subtracting_self_is_zero(value: Decimal) -> None:
    m = Money(value, "USD")
    assert (m - m).is_zero()


@given(amounts)
def test_quantisation_stays_within_one_minor_unit(value: Decimal) -> None:
    """Rounding to the minor unit never moves an amount by more than a quantum."""
    money = Money(value, "USD")
    delta = abs(money.quantized().amount - value)
    assert delta <= quantum_for("USD")


@given(amounts)
def test_quantisation_is_idempotent(value: Decimal) -> None:
    once = Money(value, "USD").quantized()
    assert once.quantized() == once


def test_quantisation_uses_bankers_rounding() -> None:
    """Half-even avoids the upward drift that half-up injects into aggregates."""
    assert Money(Decimal("0.005"), "USD").to_str() == "0.00"
    assert Money(Decimal("0.015"), "USD").to_str() == "0.02"
    assert Money(Decimal("0.025"), "USD").to_str() == "0.02"


def test_currency_precision_differs_by_currency() -> None:
    assert Money(Decimal("1.005"), "USD").to_str() == "1.00"
    assert Money(Decimal("1.005"), "JPY").to_str() == "1"
    assert Money(Decimal("1.000000005"), "BTC").to_str() == "1.00000000"


@given(amounts, amounts)
def test_cross_currency_arithmetic_is_refused(left: Decimal, right: Decimal) -> None:
    """Implicit conversion would destroy the FX attribution required by §17.3."""
    usd = Money(left, "USD")
    eur = Money(right, "EUR")
    with pytest.raises(ValueError, match="Refusing implicit conversion"):
        _ = usd + eur


@given(amounts, st.decimals(min_value=Decimal("0.1"), max_value=Decimal("10"), places=6))
def test_conversion_requires_an_explicit_rate(value: Decimal, rate: Decimal) -> None:
    converted = Money(value, "EUR").convert(rate, "USD")
    assert converted.currency == "USD"
    assert converted.amount == value * rate


@pytest.fixture
def isolated_currency_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap in a copy of the currency registry for the duration of one test.

    ``register_currency`` mutates module-level state. Without this fixture, a
    registration made by one test would leak into every test that runs
    afterward in the same process — including tests elsewhere in the suite
    that specifically assert a given code is *not* registered. Swapping the
    dict object itself (rather than mutating it) means ``monkeypatch``
    restores the original registry unconditionally at teardown.
    """
    monkeypatch.setattr(money_module, "_CURRENCY_DECIMALS", dict(money_module._CURRENCY_DECIMALS))


def test_unregistered_currency_is_refused() -> None:
    """An undeclared rounding rule means no honest amount can be stored."""
    with pytest.raises(ValueError, match="Unregistered currency"):
        Money(Decimal("1"), "ZZZ")


def test_register_currency_makes_it_usable(isolated_currency_registry: None) -> None:
    register_currency("ZZZ", 3)
    assert Money(Decimal("1.0005"), "ZZZ").to_str() == "1.000"


def test_register_currency_rejects_negative_precision(isolated_currency_registry: None) -> None:
    with pytest.raises(ValueError, match="cannot have negative precision"):
        register_currency("ZZZ", -1)


def test_float_amounts_are_refused() -> None:
    with pytest.raises(TypeError, match="must be Decimal"):
        Money(1.5, "USD")  # type: ignore[arg-type]


@given(amounts)
def test_quantity_precision_is_independent_of_currency(value: Decimal) -> None:
    """Share counts follow the platform's unit precision, not a currency's."""
    assume(value.is_finite())
    quantised = quantize_quantity(value)
    assert abs(quantised - value) <= Decimal("1e-8")
