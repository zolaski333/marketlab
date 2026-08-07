"""Double-entry ledger (§17.1, §17.2).

Balance is enforced, not assumed
--------------------------------
:meth:`Ledger.post` refuses any transaction whose signed amounts do not sum to
zero **within every currency involved**, raising
:class:`~marketlab.core.failures.AccountingError`. There is no path that writes
a half-transaction: the entries of one posting are constructed together,
validated together, and inserted together.

This is why the ledger is a service with one write method rather than a table
callers insert into. A table anyone can INSERT into is single-entry
bookkeeping with extra steps — the invariant that makes double-entry worth
having is precisely the one an ad-hoc insert would skip.

Amounts are exact
-----------------
Every stored amount is a quantised :class:`~marketlab.core.money.Money` in
plain positional notation (§17.2, §34.10). The balance check runs on the
*quantised* values, so a transaction that only balances before rounding is
rejected rather than silently leaving a sub-cent residue in the books.

Transactions are content-addressed
----------------------------------
``transaction_id`` derives from the portfolio, type, instant, reference and
postings. Re-posting an identical transaction is a no-op, which is what makes
an interrupted cycle resumable (§30.6) without double-charging a fee or
booking a fill twice.

Commit discipline
-----------------
:meth:`post` adds and **flushes**, so a subsequent balance query in the same
transaction sees it, but does not commit. The caller — always
:mod:`marketlab.execution.engine` — commits by appending the domain event that
narrates the posting, exactly as
:meth:`marketlab.snapshots.builder.SnapshotBuilder.build` does. The accounting
row and the event that explains it therefore land together or not at all.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.accounting.accounts import Account, AccountCode
from marketlab.core.canonical import canonical_json
from marketlab.core.clock import Clock
from marketlab.core.failures import AccountingError
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant
from marketlab.core.money import Currency, Money, decimal_from_str
from marketlab.storage.base import Base, DecimalStr, HashStr, InstantStr, JsonStr, ShortStr

__all__ = [
    "Ledger",
    "LedgerEntryRow",
    "LedgerTransaction",
    "LedgerTransactionRow",
    "Posting",
]


class LedgerTransactionRow(Base):
    """One balanced set of entries."""

    __tablename__ = "ledger_transactions"

    transaction_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    transaction_type: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    occurred_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    recorded_at: Mapped[str] = mapped_column(InstantStr, nullable=False)
    reference_json: Mapped[str] = mapped_column(JsonStr, nullable=False)
    """What this posting was caused by — a fill id, a settlement, a corporate
    action. Kept so a balance can always be traced back to the event that
    produced it."""

    entry_count: Mapped[int] = mapped_column(Integer, nullable=False)


class LedgerEntryRow(Base):
    """One signed amount against one account. Positive is a debit."""

    __tablename__ = "ledger_entries"

    entry_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    transaction_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    portfolio_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    account_code: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    currency: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    subject: Mapped[str] = mapped_column(ShortStr, nullable=False, default="")
    amount: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    """Signed debit, exact, plain positional notation — never a float (§17.2)."""

    occurred_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    memo: Mapped[str] = mapped_column(ShortStr, nullable=False, default="")


@dataclass(frozen=True, slots=True)
class Posting:
    """One leg of a transaction. ``amount`` positive debits, negative credits."""

    account: Account
    amount: Money
    memo: str = ""

    def __post_init__(self) -> None:
        if self.account.currency != self.amount.currency:
            raise AccountingError(
                f"Posting to {self.account} with {self.amount.currency}: an account "
                "holds exactly one currency, and posting another to it would make "
                "its balance meaningless.",
                account=str(self.account),
                amount_currency=self.amount.currency,
            )


@dataclass(frozen=True, slots=True)
class LedgerTransaction:
    """A posted transaction, as returned to the caller."""

    transaction_id: str
    portfolio_id: str
    transaction_type: str
    occurred_at: Instant
    postings: tuple[Posting, ...]


class Ledger:
    """Posts to, and reads from, the double-entry books."""

    __slots__ = ("_clock", "_session")

    def __init__(self, session: Session, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    # -- writing -------------------------------------------------------------

    def post(
        self,
        *,
        portfolio_id: str,
        transaction_type: str,
        occurred_at: Instant,
        postings: Sequence[Posting],
        reference: Mapping[str, Any],
    ) -> LedgerTransaction:
        """Record one balanced transaction.

        Idempotent by derived ``transaction_id``: posting the same transaction
        twice records it once.

        Raises:
            AccountingError: if there are no postings, or if the quantised
                amounts do not sum to zero in every currency involved.
        """
        quantised = tuple(
            Posting(account=p.account, amount=p.amount.quantized(), memo=p.memo) for p in postings
        )
        _require_balanced(quantised, transaction_type)

        transaction_id = derive_id(
            IdKind.LEDGER_TRANSACTION,
            portfolio_id=portfolio_id,
            transaction_type=transaction_type,
            occurred_at=str(occurred_at),
            reference=dict(reference),
            postings=[
                {
                    "code": str(p.account.code),
                    "currency": p.account.currency,
                    "subject": p.account.subject,
                    "amount": p.amount.to_str(),
                }
                for p in quantised
            ],
        )
        existing = self._session.get(LedgerTransactionRow, transaction_id)
        if existing is not None:
            return LedgerTransaction(
                transaction_id=transaction_id,
                portfolio_id=portfolio_id,
                transaction_type=transaction_type,
                occurred_at=occurred_at,
                postings=quantised,
            )

        self._session.add(
            LedgerTransactionRow(
                transaction_id=transaction_id,
                portfolio_id=portfolio_id,
                transaction_type=transaction_type,
                occurred_at=str(occurred_at),
                recorded_at=str(self._clock.now_instant()),
                reference_json=canonical_json(reference),
                entry_count=len(quantised),
            )
        )
        for index, posting in enumerate(quantised):
            self._session.add(
                LedgerEntryRow(
                    entry_id=derive_id(
                        IdKind.LEDGER_ENTRY, transaction_id=transaction_id, index=index
                    ),
                    transaction_id=transaction_id,
                    portfolio_id=portfolio_id,
                    account_code=str(posting.account.code),
                    currency=posting.account.currency,
                    subject=posting.account.subject,
                    amount=posting.amount.to_str(),
                    occurred_at=str(occurred_at),
                    memo=posting.memo,
                )
            )
        # Flush, not commit: a balance query later in this same transaction
        # must see these entries, but the commit belongs to the caller's
        # domain event (see the module docstring).
        self._session.flush()

        return LedgerTransaction(
            transaction_id=transaction_id,
            portfolio_id=portfolio_id,
            transaction_type=transaction_type,
            occurred_at=occurred_at,
            postings=quantised,
        )

    # -- reading -------------------------------------------------------------

    def balance(
        self, portfolio_id: str, account: Account, *, as_of: Instant | None = None
    ) -> Money:
        """Signed-debit balance of one account, optionally as of an instant."""
        query = (
            select(LedgerEntryRow.amount)
            .where(LedgerEntryRow.portfolio_id == portfolio_id)
            .where(LedgerEntryRow.account_code == str(account.code))
            .where(LedgerEntryRow.currency == account.currency)
            .where(LedgerEntryRow.subject == account.subject)
        )
        if as_of is not None:
            query = query.where(LedgerEntryRow.occurred_at <= str(as_of))
        total = sum(
            (decimal_from_str(amount) for amount in self._session.execute(query).scalars()),
            Decimal(0),
        )
        return Money(total, account.currency)

    def balances(self, portfolio_id: str, *, as_of: Instant | None = None) -> dict[Account, Money]:
        """Every non-empty account in one book."""
        query = select(LedgerEntryRow).where(LedgerEntryRow.portfolio_id == portfolio_id)
        if as_of is not None:
            query = query.where(LedgerEntryRow.occurred_at <= str(as_of))

        totals: dict[Account, Decimal] = defaultdict(Decimal)
        for row in self._session.execute(query).scalars():
            account = Account(
                code=AccountCode(row.account_code), currency=row.currency, subject=row.subject
            )
            totals[account] += decimal_from_str(row.amount)
        return {account: Money(amount, account.currency) for account, amount in totals.items()}

    def trial_balance(
        self, portfolio_id: str, *, as_of: Instant | None = None
    ) -> dict[Currency, Money]:
        """Sum of every signed amount, per currency. Must be zero everywhere.

        The property the whole module exists to guarantee, exposed so an audit
        can check it directly rather than trusting that every writer used
        :meth:`post`.
        """
        totals: dict[Currency, Decimal] = defaultdict(Decimal)
        for account, amount in self.balances(portfolio_id, as_of=as_of).items():
            totals[account.currency] += amount.amount
        return {currency: Money(total, currency) for currency, total in totals.items()}

    def assert_balanced(self, portfolio_id: str, *, as_of: Instant | None = None) -> None:
        """Raise if the books do not balance.

        Raises:
            AccountingError: naming the currency and the residual.
        """
        for currency, total in self.trial_balance(portfolio_id, as_of=as_of).items():
            if not total.is_zero():
                raise AccountingError(
                    f"Books for portfolio {portfolio_id} do not balance in {currency}: "
                    f"residual {total}.",
                    portfolio_id=portfolio_id,
                    currency=currency,
                    residual=total.to_str(),
                )

    def transactions(self, portfolio_id: str) -> list[LedgerTransactionRow]:
        """Every transaction in one book, in the order it occurred."""
        query = (
            select(LedgerTransactionRow)
            .where(LedgerTransactionRow.portfolio_id == portfolio_id)
            .order_by(LedgerTransactionRow.occurred_at.asc(), LedgerTransactionRow.transaction_id)
        )
        return list(self._session.execute(query).scalars())

    def entries_of(self, transaction_id: str) -> list[LedgerEntryRow]:
        query = (
            select(LedgerEntryRow)
            .where(LedgerEntryRow.transaction_id == transaction_id)
            .order_by(LedgerEntryRow.entry_id)
        )
        return list(self._session.execute(query).scalars())


def _require_balanced(postings: Sequence[Posting], transaction_type: str) -> None:
    if not postings:
        raise AccountingError(
            f"Transaction {transaction_type} has no postings: an empty transaction "
            "balances trivially and records nothing.",
            transaction_type=transaction_type,
        )

    totals: dict[Currency, Decimal] = defaultdict(Decimal)
    for posting in postings:
        totals[posting.amount.currency] += posting.amount.amount

    unbalanced = {currency: total for currency, total in totals.items() if total != 0}
    if unbalanced:
        detail = ", ".join(f"{currency} {total}" for currency, total in sorted(unbalanced.items()))
        raise AccountingError(
            f"Transaction {transaction_type} does not balance: {detail}. Every "
            "currency must sum to zero on its own — offsetting one currency "
            "against another would assume they are interchangeable (§17.3).",
            transaction_type=transaction_type,
            residuals={currency: str(total) for currency, total in unbalanced.items()},
        )
