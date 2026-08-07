"""Tests for the chart of accounts and the double-entry ledger (§17.1, §17.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SqlIntegrityError
from sqlalchemy.orm import Session

from marketlab.accounting.accounts import (
    Account,
    AccountCode,
    AccountKind,
    cash,
    dividend_income,
    fees,
    opening_equity,
    payable,
    position,
    realized_pnl,
    receivable,
)
from marketlab.accounting.ledger import Ledger, LedgerEntryRow, Posting
from marketlab.core.clock import FrozenClock
from marketlab.core.failures import AccountingError, ConfigurationError
from marketlab.core.instants import instant_from_datetime
from marketlab.core.money import Money
from marketlab.storage.database import Database

PORTFOLIO = "portfolio-under-test"
OTHER_PORTFOLIO = "another-portfolio"
T1 = instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC))
T2 = instant_from_datetime(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))


def usd(amount: str) -> Money:
    return Money(Decimal(amount), "USD")


def eur(amount: str) -> Money:
    return Money(Decimal(amount), "EUR")


@pytest.fixture
def ledger(session: Session, clock: FrozenClock) -> Ledger:
    return Ledger(session, clock)


def _fund(ledger: Ledger, amount: Money, *, portfolio: str = PORTFOLIO) -> None:
    ledger.post(
        portfolio_id=portfolio,
        transaction_type="OPENING_BALANCE",
        occurred_at=T1,
        postings=[
            Posting(cash(amount.currency), amount),
            Posting(opening_equity(amount.currency), -amount),
        ],
        reference={"reason": "test funding"},
    )


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------


def test_assets_and_expenses_are_debit_normal_and_nothing_else_is() -> None:
    debit_normal = {kind for kind in AccountKind if kind.is_debit_normal}
    assert debit_normal == {AccountKind.ASSET, AccountKind.EXPENSE}


def test_every_account_code_has_a_declared_kind() -> None:
    # A code without a kind would have an undefined natural sign, so every
    # report of it would be a coin flip.
    for code in AccountCode:
        assert Account(code, "USD", "x" if code is AccountCode.POSITION else "").kind


def test_a_position_account_without_an_instrument_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="requires a subject"):
        Account(AccountCode.POSITION, "USD")


def test_a_non_position_account_with_a_subject_is_refused() -> None:
    """Two spellings of the same account would not offset each other."""
    with pytest.raises(ConfigurationError, match="takes no subject"):
        Account(AccountCode.CASH, "USD", "id-alpha")


def test_natural_sign_flips_only_for_credit_normal_accounts() -> None:
    assert cash("USD").natural_sign == 1
    assert fees("USD").natural_sign == 1
    assert payable("USD").natural_sign == -1
    assert realized_pnl("USD").natural_sign == -1
    assert opening_equity("USD").natural_sign == -1


# ---------------------------------------------------------------------------
# Balance enforcement
# ---------------------------------------------------------------------------


def test_a_balanced_transaction_posts(ledger: Ledger) -> None:
    _fund(ledger, usd("100000.00"))
    assert ledger.balance(PORTFOLIO, cash("USD")) == usd("100000.00")
    assert ledger.balance(PORTFOLIO, opening_equity("USD")) == usd("-100000.00")


def test_an_unbalanced_transaction_is_refused(ledger: Ledger) -> None:
    with pytest.raises(AccountingError, match="does not balance"):
        ledger.post(
            portfolio_id=PORTFOLIO,
            transaction_type="BROKEN",
            occurred_at=T1,
            postings=[
                Posting(cash("USD"), usd("100.00")),
                Posting(opening_equity("USD"), usd("-99.00")),
            ],
            reference={},
        )


def test_an_empty_transaction_is_refused(ledger: Ledger) -> None:
    """An empty transaction balances trivially and records nothing."""
    with pytest.raises(AccountingError, match="no postings"):
        ledger.post(
            portfolio_id=PORTFOLIO,
            transaction_type="EMPTY",
            occurred_at=T1,
            postings=[],
            reference={},
        )


def test_currencies_must_balance_individually_not_merely_in_total(ledger: Ledger) -> None:
    """100 EUR against -100 USD sums to zero only if the two are
    interchangeable, which is exactly what Money refuses to assume (§17.3)."""
    with pytest.raises(AccountingError, match="does not balance"):
        ledger.post(
            portfolio_id=PORTFOLIO,
            transaction_type="CROSS_CURRENCY",
            occurred_at=T1,
            postings=[Posting(cash("EUR"), eur("100.00")), Posting(cash("USD"), usd("-100.00"))],
            reference={},
        )


def test_a_transaction_may_carry_two_currencies_if_each_balances(ledger: Ledger) -> None:
    ledger.post(
        portfolio_id=PORTFOLIO,
        transaction_type="DUAL_FUNDING",
        occurred_at=T1,
        postings=[
            Posting(cash("USD"), usd("100.00")),
            Posting(opening_equity("USD"), usd("-100.00")),
            Posting(cash("EUR"), eur("50.00")),
            Posting(opening_equity("EUR"), eur("-50.00")),
        ],
        reference={},
    )
    assert ledger.balance(PORTFOLIO, cash("USD")) == usd("100.00")
    assert ledger.balance(PORTFOLIO, cash("EUR")) == eur("50.00")


def test_balance_is_checked_after_rounding_not_before(ledger: Ledger) -> None:
    """A transaction that only balances at full precision would leave a
    sub-cent residue in the books once stored."""
    with pytest.raises(AccountingError, match="does not balance"):
        ledger.post(
            portfolio_id=PORTFOLIO,
            transaction_type="SUB_CENT",
            occurred_at=T1,
            postings=[
                Posting(cash("USD"), usd("0.005")),
                Posting(cash("USD"), usd("0.005")),
                Posting(opening_equity("USD"), usd("-0.01")),
            ],
            reference={},
        )


def test_posting_a_currency_the_account_does_not_hold_is_refused() -> None:
    with pytest.raises(AccountingError, match="holds exactly one currency"):
        Posting(cash("USD"), eur("100.00"))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_the_books_balance_after_every_posting(ledger: Ledger) -> None:
    _fund(ledger, usd("100000.00"))
    ledger.post(
        portfolio_id=PORTFOLIO,
        transaction_type="BUY",
        occurred_at=T2,
        postings=[
            Posting(position("id-alpha", "USD"), usd("1500.00")),
            Posting(fees("USD"), usd("1.50")),
            Posting(payable("USD"), usd("-1501.50")),
        ],
        reference={"order_id": "o-1"},
    )
    ledger.assert_balanced(PORTFOLIO)
    assert ledger.trial_balance(PORTFOLIO)["USD"].is_zero()


def test_assert_balanced_actually_fires_when_the_books_are_broken(
    ledger: Ledger, session: Session
) -> None:
    """Guards the previous test against passing vacuously: bypass `post` the
    way a stray raw INSERT would, and the audit check must notice."""
    _fund(ledger, usd("100.00"))
    session.add(
        LedgerEntryRow(
            entry_id="f" * 64,
            transaction_id="e" * 64,
            portfolio_id=PORTFOLIO,
            account_code=str(AccountCode.CASH),
            currency="USD",
            subject="",
            amount="42.00",
            occurred_at=str(T2),
            memo="smuggled in",
        )
    )
    session.flush()
    with pytest.raises(AccountingError, match="do not balance"):
        ledger.assert_balanced(PORTFOLIO)


def test_a_balance_can_be_read_as_of_a_past_instant(ledger: Ledger) -> None:
    _fund(ledger, usd("100.00"))
    ledger.post(
        portfolio_id=PORTFOLIO,
        transaction_type="MORE",
        occurred_at=T2,
        postings=[
            Posting(cash("USD"), usd("50.00")),
            Posting(opening_equity("USD"), usd("-50.00")),
        ],
        reference={},
    )
    assert ledger.balance(PORTFOLIO, cash("USD"), as_of=T1) == usd("100.00")
    assert ledger.balance(PORTFOLIO, cash("USD")) == usd("150.00")


def test_balances_lists_every_touched_account(ledger: Ledger) -> None:
    _fund(ledger, usd("100.00"))
    balances = ledger.balances(PORTFOLIO)
    assert set(balances) == {cash("USD"), opening_equity("USD")}


def test_an_untouched_account_reads_as_zero(ledger: Ledger) -> None:
    _fund(ledger, usd("100.00"))
    assert ledger.balance(PORTFOLIO, dividend_income("USD")).is_zero()
    assert ledger.balance(PORTFOLIO, receivable("USD")).is_zero()


def test_entries_are_traceable_back_to_their_transaction(ledger: Ledger) -> None:
    transaction = ledger.post(
        portfolio_id=PORTFOLIO,
        transaction_type="OPENING_BALANCE",
        occurred_at=T1,
        postings=[
            Posting(cash("USD"), usd("10.00")),
            Posting(opening_equity("USD"), usd("-10.00")),
        ],
        reference={"reason": "traceability"},
    )
    entries = ledger.entries_of(transaction.transaction_id)
    assert len(entries) == 2
    assert sum(Decimal(e.amount) for e in entries) == 0


def test_amounts_are_stored_in_plain_positional_notation(ledger: Ledger) -> None:
    """A previous implementation persisted '8E+4' for $80,000 (see the
    marketlab.core.money docstring). The ledger is where that would surface."""
    _fund(ledger, usd("80000.00"))
    stored = [e.amount for e in ledger.entries_of(ledger.transactions(PORTFOLIO)[0].transaction_id)]
    assert "80000.00" in stored
    assert not any("E" in amount or "e" in amount for amount in stored)


# ---------------------------------------------------------------------------
# Isolation and immutability
# ---------------------------------------------------------------------------


def test_two_portfolios_do_not_see_each_others_money(ledger: Ledger) -> None:
    """§30.3: one arm's trading must not move another arm's cash."""
    _fund(ledger, usd("100.00"))
    _fund(ledger, usd("7.00"), portfolio=OTHER_PORTFOLIO)
    assert ledger.balance(PORTFOLIO, cash("USD")) == usd("100.00")
    assert ledger.balance(OTHER_PORTFOLIO, cash("USD")) == usd("7.00")


def test_reposting_an_identical_transaction_is_a_no_op(ledger: Ledger) -> None:
    """§30.6: a resumed cycle must not double-charge a fee or book a fill twice."""
    _fund(ledger, usd("100.00"))
    _fund(ledger, usd("100.00"))
    assert ledger.balance(PORTFOLIO, cash("USD")) == usd("100.00")
    assert len(ledger.transactions(PORTFOLIO)) == 1


def test_a_genuinely_different_transaction_is_not_deduplicated(ledger: Ledger) -> None:
    _fund(ledger, usd("100.00"))
    ledger.post(
        portfolio_id=PORTFOLIO,
        transaction_type="OPENING_BALANCE",
        occurred_at=T1,
        postings=[
            Posting(cash("USD"), usd("100.00")),
            Posting(opening_equity("USD"), usd("-100.00")),
        ],
        reference={"reason": "a different reason"},
    )
    assert ledger.balance(PORTFOLIO, cash("USD")) == usd("200.00")


def test_ledger_entries_are_append_only(
    ledger: Ledger, session: Session, database: Database
) -> None:
    _fund(ledger, usd("100.00"))
    session.commit()  # post() only flushes; the trigger needs committed rows to bite
    with pytest.raises(SqlIntegrityError, match="append-only"), database.engine.begin() as conn:
        conn.execute(text("UPDATE ledger_entries SET amount = '999.00'"))


def test_ledger_transactions_are_append_only(
    ledger: Ledger, session: Session, database: Database
) -> None:
    _fund(ledger, usd("100.00"))
    session.commit()
    with pytest.raises(SqlIntegrityError, match="append-only"), database.engine.begin() as conn:
        conn.execute(text("DELETE FROM ledger_transactions"))
