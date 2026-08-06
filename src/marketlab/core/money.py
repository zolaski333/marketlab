"""Exact monetary arithmetic.

Scientific requirement §17.2: monetary amounts use ``Decimal``, never binary
floats, and JSON stores them as strings. §34.10 restates it as a hard rule.

Two failure modes this module exists to prevent
-----------------------------------------------

1. **Scientific-notation leakage.** ``Decimal.normalize()`` strips trailing
   zeros and switches to exponent form for round numbers::

       >>> str(Decimal("80000.00").normalize())
       '8E+4'

   A previous implementation called ``.normalize()`` on every ledger amount and
   persisted the result, so the general ledger literally contained ``'8E+4'``
   for $80,000.00. It round-trips numerically but destroys the scale, is
   unreadable in an audit, and sorts nonsensically as text. This module never
   calls ``normalize()`` and always serialises via ``format(d, "f")``, which is
   guaranteed to produce plain positional notation.

2. **Undeclared rounding.** Every currency has a minor unit; crypto has far
   more precision than fiat. Rounding must be declared once, applied
   consistently, and be bankers' rounding (``ROUND_HALF_EVEN``) so that repeated
   rounding does not introduce a directional bias into aggregate P&L.

Quantisation is applied at *storage and settlement boundaries*, not to every
intermediate term: quantising intermediates would compound rounding error. The
rule is: compute at full precision, quantise when an amount becomes a ledger
entry or a cash balance.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import Final

__all__ = [
    "DEFAULT_PRECISION",
    "Currency",
    "Money",
    "decimal_from_str",
    "decimal_to_str",
    "quantum_for",
    "register_currency",
]

Currency = str

# Working precision for intermediate arithmetic. Generous enough that no
# realistic position size loses significance before the quantisation step.
DEFAULT_PRECISION: Final = 34

# Number of decimal places per currency. Fiat uses its minor unit; crypto keeps
# enough places to represent the smallest tradable fraction exactly.
_CURRENCY_DECIMALS: dict[Currency, int] = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CHF": 2,
    "JPY": 0,
    "BTC": 8,
    "ETH": 18,
    "XAU": 6,
}

# Decimal places used for instrument quantities (share/unit counts). Kept
# separate from currency precision: a fractional-share policy is an execution
# concern, not a monetary one.
QUANTITY_DECIMALS: Final = 8


def register_currency(code: Currency, decimals: int) -> None:
    """Declare the minor-unit precision of a currency.

    Registration is required before a currency can be used in a ledger entry;
    §16.4 refuses execution when honest valuation is impossible, and an unknown
    rounding rule is exactly that.
    """
    if decimals < 0:
        raise ValueError(f"Currency {code} cannot have negative precision")
    _CURRENCY_DECIMALS[code] = decimals


def quantum_for(currency: Currency) -> Decimal:
    """Return the smallest representable amount in ``currency``."""
    try:
        decimals = _CURRENCY_DECIMALS[currency]
    except KeyError:
        raise ValueError(
            f"Unregistered currency {currency!r}: its rounding rule is undeclared, "
            "so no honest amount can be stored (§16.4)."
        ) from None
    return Decimal(1).scaleb(-decimals)


def decimal_to_str(value: Decimal) -> str:
    """Serialise a Decimal to a plain positional string.

    Never produces scientific notation, so the stored text is exactly what an
    auditor reads.
    """
    if value.is_nan() or value.is_infinite():
        raise ValueError(f"Non-finite monetary value rejected: {value!r}")
    return format(value, "f")


def decimal_from_str(text: str) -> Decimal:
    """Parse a Decimal from its stored string form."""
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Malformed decimal string: {text!r}") from exc
    if value.is_nan() or value.is_infinite():
        raise ValueError(f"Non-finite monetary value rejected: {text!r}")
    return value


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An exact amount in a declared currency.

    Immutable, and arithmetic between different currencies raises rather than
    silently coercing — cross-currency conversion must go through an explicit,
    logged FX rate so that §17.3 attribution (local price move vs FX move vs
    fees) remains decomposable.
    """

    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):  # pragma: no cover - type guard
            raise TypeError(
                f"Money.amount must be Decimal, got {type(self.amount).__name__}. "
                "Binary floats are forbidden in accounting (§34.10)."
            )
        if self.amount.is_nan() or self.amount.is_infinite():
            raise ValueError(f"Non-finite Money rejected: {self.amount!r}")
        # Touch the registry so an unknown currency fails at construction time
        # rather than at persistence time.
        quantum_for(self.currency)

    # -- constructors ------------------------------------------------------

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        return cls(Decimal(0), currency)

    @classmethod
    def parse(cls, text: str, currency: Currency) -> Money:
        return cls(decimal_from_str(text), currency)

    # -- arithmetic --------------------------------------------------------

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(
                f"Refusing implicit conversion {self.currency}->{other.currency}. "
                "Cross-currency arithmetic requires an explicit logged FX rate (§17.3)."
            )

    def __add__(self, other: Money) -> Money:
        self._check(other)
        with localcontext() as ctx:
            ctx.prec = DEFAULT_PRECISION
            return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        with localcontext() as ctx:
            ctx.prec = DEFAULT_PRECISION
            return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def scaled_by(self, factor: Decimal) -> Money:
        """Multiply by a dimensionless quantity (share count, ratio)."""
        with localcontext() as ctx:
            ctx.prec = DEFAULT_PRECISION
            return Money(self.amount * factor, self.currency)

    def convert(self, rate: Decimal, to_currency: Currency) -> Money:
        """Convert using an explicit rate expressed as ``to`` per unit of ``from``."""
        with localcontext() as ctx:
            ctx.prec = DEFAULT_PRECISION
            return Money(self.amount * rate, to_currency)

    # -- comparison --------------------------------------------------------

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.amount >= other.amount

    def is_zero(self) -> bool:
        return self.amount == 0

    def is_negative(self) -> bool:
        return self.amount < 0

    # -- storage -----------------------------------------------------------

    def quantized(self) -> Money:
        """Round to the currency's minor unit using bankers' rounding."""
        return Money(
            self.amount.quantize(quantum_for(self.currency), rounding=ROUND_HALF_EVEN),
            self.currency,
        )

    def to_str(self) -> str:
        """Serialise the *quantised* amount for persistence."""
        return decimal_to_str(self.quantized().amount)

    def __str__(self) -> str:
        return f"{self.to_str()} {self.currency}"


def quantize_quantity(value: Decimal) -> Decimal:
    """Round an instrument quantity to the platform's unit precision."""
    return value.quantize(Decimal(1).scaleb(-QUANTITY_DECIMALS), rounding=ROUND_HALF_EVEN)
