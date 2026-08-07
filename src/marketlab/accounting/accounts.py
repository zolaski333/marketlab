"""The chart of accounts (§17.1).

A deliberately small, closed set. Every posting the platform makes lands in
one of these; there is no "miscellaneous" account, because an amount nobody
could name is an amount nobody can audit.

Signed debits
-------------
An entry carries one signed amount: **positive is a debit, negative is a
credit**. The alternative — a `direction` enum beside an unsigned amount —
makes the balance check (`sum == 0`) into a conditional sum, and a conditional
sum is a place to get the sign wrong. With signed amounts, "this transaction
balances" is literally addition.

The cost is that a credit-normal account (liability, equity, income) carries a
negative internal balance. :meth:`Account.natural_sign` exists so reports can
present those the way an accountant expects without any call site doing sign
arithmetic by hand.

Per-currency balance
--------------------
A transaction must balance **within each currency**, not merely in total.
Summing 100 EUR against -100 USD would "balance" only if the two were
interchangeable, which is exactly what :class:`~marketlab.core.money.Money`
refuses to assume (§17.3). Cross-currency movement therefore cannot be
expressed as a single transaction here — see ``docs/ROADMAP.md`` on why
Phase 1 funds each currency directly instead of converting between them.

Portfolios
----------
An account is a *position in the chart*, not a book. Which book it is posted
in is the ``portfolio_id`` on the transaction — one per (run, arm,
repetition), so arm B's trading cannot move arm A's cash. That isolation is
§30.3's requirement and is asserted directly in the execution tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from marketlab.core.failures import ConfigurationError
from marketlab.core.money import Currency

__all__ = [
    "KIND_OF_CODE",
    "Account",
    "AccountCode",
    "AccountKind",
    "cash",
    "fees",
    "opening_equity",
    "payable",
    "position",
    "realized_pnl",
    "receivable",
]


class AccountKind(StrEnum):
    """Where an account sits in the accounting equation."""

    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"

    @property
    def is_debit_normal(self) -> bool:
        """Assets and expenses grow by debit; everything else by credit."""
        return self in (AccountKind.ASSET, AccountKind.EXPENSE)


class AccountCode(StrEnum):
    """Every account this platform can post to."""

    CASH = "CASH"
    """Settled cash, per currency."""

    POSITION = "POSITION"
    """Instrument holdings carried at cost. Subject: the instrument id."""

    RECEIVABLE = "RECEIVABLE"
    """Sale proceeds sold but not yet settled (T+N)."""

    PAYABLE = "PAYABLE"
    """Purchase obligations incurred but not yet settled (T+N)."""

    OPENING_EQUITY = "OPENING_EQUITY"
    """The study's initial virtual funding. Never touched again."""

    FEES = "FEES"
    """Explicit transaction costs. Spread is *not* here — see the note in
    :mod:`marketlab.execution.policy`: spread is paid inside the fill price,
    and recording it separately would double-count it."""

    REALIZED_PNL = "REALIZED_PNL"
    """Gain or loss crystallised when a lot is closed."""

    DIVIDEND_INCOME = "DIVIDEND_INCOME"


_KIND_OF_CODE: Final[dict[AccountCode, AccountKind]] = {
    AccountCode.CASH: AccountKind.ASSET,
    AccountCode.POSITION: AccountKind.ASSET,
    AccountCode.RECEIVABLE: AccountKind.ASSET,
    AccountCode.PAYABLE: AccountKind.LIABILITY,
    AccountCode.OPENING_EQUITY: AccountKind.EQUITY,
    AccountCode.FEES: AccountKind.EXPENSE,
    AccountCode.REALIZED_PNL: AccountKind.INCOME,
    AccountCode.DIVIDEND_INCOME: AccountKind.INCOME,
}

KIND_OF_CODE: Final[Mapping[AccountCode, AccountKind]] = MappingProxyType(_KIND_OF_CODE)

_SUBJECT_REQUIRED: Final[frozenset[AccountCode]] = frozenset({AccountCode.POSITION})
"""Codes that are meaningless without naming what they are about.

A ``POSITION`` account with no instrument would silently pool every holding
into one balance, making cost basis — and therefore realised P&L —
unrecoverable.
"""


@dataclass(frozen=True, slots=True)
class Account:
    """One position in the chart of accounts."""

    code: AccountCode
    currency: Currency
    subject: str = ""
    """What the account is about — an instrument id for ``POSITION``, empty
    otherwise."""

    def __post_init__(self) -> None:
        if self.code in _SUBJECT_REQUIRED and not self.subject:
            raise ConfigurationError(
                f"Account {self.code} requires a subject; without one every "
                "holding would pool into a single balance and cost basis would "
                "be unrecoverable.",
                code=str(self.code),
            )
        if self.code not in _SUBJECT_REQUIRED and self.subject:
            raise ConfigurationError(
                f"Account {self.code} takes no subject, got {self.subject!r}. "
                "Two spellings of the same account do not offset each other.",
                code=str(self.code),
                subject=self.subject,
            )

    @property
    def kind(self) -> AccountKind:
        return KIND_OF_CODE[self.code]

    @property
    def natural_sign(self) -> int:
        """``+1`` if this account's balance reads positively as a debit.

        Multiply an internal (signed-debit) balance by this to get the figure
        an accountant expects: cash of 100, a payable of 100, income of 100 —
        rather than a payable of -100.
        """
        return 1 if self.kind.is_debit_normal else -1

    def __str__(self) -> str:
        return f"{self.code}:{self.currency}" + (f":{self.subject}" if self.subject else "")


# -- constructors, so call sites never spell a code by hand --------------------


def cash(currency: Currency) -> Account:
    return Account(AccountCode.CASH, currency)


def position(instrument_id: str, currency: Currency) -> Account:
    return Account(AccountCode.POSITION, currency, instrument_id)


def receivable(currency: Currency) -> Account:
    return Account(AccountCode.RECEIVABLE, currency)


def payable(currency: Currency) -> Account:
    return Account(AccountCode.PAYABLE, currency)


def opening_equity(currency: Currency) -> Account:
    return Account(AccountCode.OPENING_EQUITY, currency)


def fees(currency: Currency) -> Account:
    return Account(AccountCode.FEES, currency)


def realized_pnl(currency: Currency) -> Account:
    return Account(AccountCode.REALIZED_PNL, currency)


def dividend_income(currency: Currency) -> Account:
    return Account(AccountCode.DIVIDEND_INCOME, currency)
