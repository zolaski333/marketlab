"""Portfolio valuation across currencies (§17.3).

Equity has to be one number before anything can be sized as a fraction of it,
and this portfolio holds USD cash, EUR cash, a USD equity and a EUR equity.
Collapsing those into one figure requires an exchange rate, and §17.3 requires
that rate to be **explicit and logged** rather than assumed — which here means
it comes out of the same frozen snapshot the decision was made from, never
from a live lookup at valuation time.

Refusing rather than guessing
-----------------------------
:class:`FxTable` knows only the pairs the snapshot actually carries. Asked for
one it does not have, it raises. The tempting alternative — falling back to
1.0, or to the last rate seen — would silently value a foreign holding at the
wrong number and quietly corrupt every downstream figure. §16.4's rule applies
to valuation as much as to execution: when no honest number exists, refuse.

Positions are valued at the mid
-------------------------------
Holdings are marked at the mid of bid and ask, not at the price they could be
sold into. Marking to the bid would book the round-trip spread as a loss the
moment a position is opened, making every arm's equity dip on entry for
reasons having nothing to do with its decision. The spread is charged where it
is actually paid — inside the fill price (see
:mod:`marketlab.execution.policy`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from marketlab.accounting.accounts import AccountCode
from marketlab.accounting.ledger import Ledger
from marketlab.accounting.positions import PositionBook
from marketlab.core.failures import ConfigurationError
from marketlab.core.instants import Instant
from marketlab.core.money import Currency, Money
from marketlab.retrieval.types import EvidenceKind, RetrievalIndex, price_quote_from_evidence

__all__ = ["FxTable", "PortfolioValuation", "value_portfolio"]


class FxTable:
    """Exchange rates as of one frozen snapshot."""

    __slots__ = ("_rates",)

    def __init__(self, rates: Mapping[str, Decimal]) -> None:
        self._rates = dict(rates)

    @classmethod
    def from_index(cls, index: RetrievalIndex) -> FxTable:
        """Read every FX rate the snapshot carries.

        Pair codes follow the snapshot's own convention (``EUR_USD``), read as
        "units of the second currency per unit of the first".
        """
        rates: dict[str, Decimal] = {}
        for evidence in index.evidence_of_kind(EvidenceKind.FX_RATE):
            for pair in evidence.subject_ids:
                rates[pair] = Decimal(str(evidence.fields["rate"]))
        return cls(rates)

    def rate(self, from_currency: Currency, to_currency: Currency) -> Decimal:
        """Units of ``to_currency`` per unit of ``from_currency``.

        Raises:
            ConfigurationError: if neither direction of the pair is in the
                snapshot. Guessing here would silently misvalue a holding.
        """
        if from_currency == to_currency:
            return Decimal(1)
        direct = self._rates.get(f"{from_currency}_{to_currency}")
        if direct is not None:
            return direct
        inverse = self._rates.get(f"{to_currency}_{from_currency}")
        if inverse is not None and inverse != 0:
            return Decimal(1) / inverse
        raise ConfigurationError(
            f"No {from_currency}->{to_currency} rate in this snapshot. Valuing a "
            "holding without an explicit logged rate is exactly what §17.3 "
            "forbids.",
            from_currency=from_currency,
            to_currency=to_currency,
            available=sorted(self._rates),
        )

    def convert(self, amount: Money, to_currency: Currency) -> Money:
        return amount.convert(self.rate(amount.currency, to_currency), to_currency)


@dataclass(frozen=True, slots=True)
class PortfolioValuation:
    """One portfolio's worth, in one currency, as of one snapshot."""

    portfolio_id: str
    base_currency: Currency
    as_of: Instant
    cash: Money
    """Settled cash only. Unsettled legs sit in receivables and payables."""

    receivables: Money
    payables: Money
    positions: Money
    """Open holdings marked at mid."""

    equity: Money
    by_instrument: Mapping[str, Money]

    def in_currency(self, currency: Currency, fx: FxTable) -> Money:
        """This portfolio's equity expressed in another currency."""
        return fx.convert(self.equity, currency)


def value_portfolio(
    ledger: Ledger,
    positions: PositionBook,
    index: RetrievalIndex,
    *,
    portfolio_id: str,
    base_currency: Currency,
) -> PortfolioValuation:
    """Mark one portfolio to the frozen snapshot ``index``.

    Raises:
        ConfigurationError: if a held instrument has no price in the snapshot,
            or no rate exists to convert it. An unpriceable holding makes
            equity a fiction, and a fictional equity would size every
            subsequent order (§16.4).
    """
    fx = FxTable.from_index(index)
    as_of = index.cutoff

    cash = Money.zero(base_currency)
    receivables = Money.zero(base_currency)
    payables = Money.zero(base_currency)
    for account, balance in ledger.balances(portfolio_id, as_of=as_of).items():
        converted = fx.convert(balance, base_currency)
        if account.code is AccountCode.CASH:
            cash = cash + converted
        elif account.code is AccountCode.RECEIVABLE:
            receivables = receivables + converted
        elif account.code is AccountCode.PAYABLE:
            # Stored as a credit-normal (negative) balance; report it positive.
            payables = payables + Money(-converted.amount, base_currency)

    by_instrument: dict[str, Money] = {}
    holdings = Money.zero(base_currency)
    for instrument_id in positions.held_instruments(portfolio_id, as_of=as_of):
        quantity = positions.quantity_of(portfolio_id, instrument_id, as_of=as_of)
        view = index.resolve_instrument(instrument_id)
        evidence = index.latest(EvidenceKind.PRICE_BAR, instrument_id)
        if view is None or evidence is None:
            raise ConfigurationError(
                f"Holding {instrument_id} has no price in snapshot {index.snapshot_id}: "
                "equity cannot be computed honestly, and every order sized "
                "against it would inherit the fiction (§16.4).",
                instrument_id=instrument_id,
                snapshot_id=index.snapshot_id,
            )
        quote = price_quote_from_evidence(evidence)
        mid = (quote.bid + quote.ask) / Decimal(2)
        local = Money(mid * quantity, view.quote_currency)
        converted = fx.convert(local, base_currency)
        by_instrument[instrument_id] = converted
        holdings = holdings + converted

    equity = cash + receivables + holdings - payables
    return PortfolioValuation(
        portfolio_id=portfolio_id,
        base_currency=base_currency,
        as_of=as_of,
        cash=cash.quantized(),
        receivables=receivables.quantized(),
        payables=payables.quantized(),
        positions=holdings.quantized(),
        equity=equity.quantized(),
        by_instrument=by_instrument,
    )
