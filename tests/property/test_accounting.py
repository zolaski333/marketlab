"""Property tests for double-entry bookkeeping (§17.1, §17.2, §30.5).

The unit tests in ``tests/unit/test_ledger.py`` check the ledger against
hand-picked transactions. That is exactly the shape of test that misses a
signed-arithmetic bug: the examples a person writes are the ones they already
reasoned about correctly.

So these generate transactions instead — arbitrary account mixes, arbitrary
amounts, several currencies at once — and assert the two things that must hold
for every one of them: a set of postings balances per currency if and only if
the ledger accepts it, and the folded balances equal the postings that produced
them, exactly, with no float anywhere in the path.

Each example gets its own database. Sharing one would let derived transaction
ids collide across examples, and a collision would look like an idempotence
success rather than the test-rig fault it is.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from marketlab.accounting import accounts
from marketlab.accounting.accounts import Account
from marketlab.accounting.ledger import Ledger, Posting
from marketlab.core.clock import FrozenClock
from marketlab.core.failures import AccountingError
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.core.money import Money
from marketlab.storage.database import Database
from tests.conftest import REFERENCE_INSTANT

PORTFOLIO = "portfolio-under-test"
AT: Instant = instant_from_datetime(REFERENCE_INSTANT)

CURRENCIES = ("USD", "EUR")
_ACCOUNT_BUILDERS = (
    accounts.cash,
    accounts.fees,
    accounts.realized_pnl,
    accounts.opening_equity,
    accounts.receivable,
    accounts.payable,
)

# Two decimal places, bounded well inside the working precision: the property
# under test is the arithmetic, not the quantisation, which test_money covers.
cents = st.integers(min_value=-5_000_000, max_value=5_000_000).map(
    lambda units: Decimal(units) / Decimal(100)
)
currencies = st.sampled_from(CURRENCIES)
account_indices = st.integers(min_value=0, max_value=len(_ACCOUNT_BUILDERS) - 1)

SETTINGS = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _account(index: int, currency: str) -> Account:
    return _ACCOUNT_BUILDERS[index](currency)


@st.composite
def balanced_postings(draw: st.DrawFn) -> list[Posting]:
    """A posting set that sums to zero in every currency it touches.

    Built by construction rather than filtered: drawing at random and
    discarding the unbalanced ones would almost never produce an example.
    """
    postings: list[Posting] = []
    used = draw(st.lists(currencies, min_size=1, max_size=len(CURRENCIES), unique=True))
    for currency in used:
        legs = draw(st.lists(st.tuples(account_indices, cents), min_size=1, max_size=6))
        total = Decimal(0)
        for index, amount in legs:
            postings.append(Posting(_account(index, currency), Money(amount, currency)))
            total += amount
        # The closing leg is what makes the set balance, which is what a real
        # transaction always has: money comes from somewhere.
        postings.append(Posting(accounts.cash(currency), Money(-total, currency), "closing leg"))
    return postings


@contextmanager
def _ledger() -> Iterator[tuple[Ledger, Session]]:
    """A fresh database per example."""
    with tempfile.TemporaryDirectory() as root:
        database = Database(Path(root) / "study.db")
        database.create_schema()
        try:
            with database.session_scope() as session:
                yield Ledger(session, FrozenClock(REFERENCE_INSTANT)), session
        finally:
            database.close()


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


@SETTINGS
@given(balanced_postings())
def test_any_balanced_transaction_is_accepted(postings: list[Posting]) -> None:
    with _ledger() as (ledger, _):
        ledger.post(
            portfolio_id=PORTFOLIO,
            transaction_type="TEST",
            occurred_at=AT,
            postings=postings,
            reference={},
        )
        ledger.assert_balanced(PORTFOLIO)


@SETTINGS
@given(balanced_postings(), cents)
def test_any_unbalanced_transaction_is_refused(
    postings: list[Posting], perturbation: Decimal
) -> None:
    """Rejected at the boundary, not detected later by a reconciliation job:
    an unbalanced transaction that reached storage would be a permanent,
    append-only error."""
    if perturbation == 0:
        return
    currency = postings[0].amount.currency
    broken = [*postings, Posting(accounts.fees(currency), Money(perturbation, currency))]
    with _ledger() as (ledger, _):
        with pytest.raises(AccountingError):
            ledger.post(
                portfolio_id=PORTFOLIO,
                transaction_type="TEST",
                occurred_at=AT,
                postings=broken,
                reference={},
            )


@SETTINGS
@given(balanced_postings())
def test_every_currency_balances_on_its_own(postings: list[Posting]) -> None:
    """Not merely in total. A EUR shortfall cancelled by a USD surplus is two
    errors, and a single-number check would report neither."""
    with _ledger() as (ledger, _):
        ledger.post(
            portfolio_id=PORTFOLIO,
            transaction_type="TEST",
            occurred_at=AT,
            postings=postings,
            reference={},
        )
        for currency, total in ledger.trial_balance(PORTFOLIO).items():
            assert total.is_zero(), currency


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


@SETTINGS
@given(balanced_postings())
def test_a_folded_balance_equals_the_postings_that_produced_it(
    postings: list[Posting],
) -> None:
    """Exactly. Not to within a cent: §17.2 forbids floats in this path, and a
    property test over hundreds of random amounts is where a stray float
    would first show."""
    expected: dict[tuple[str, str], Decimal] = {}
    for posting in postings:
        key = (str(posting.account), posting.amount.currency)
        expected[key] = expected.get(key, Decimal(0)) + posting.amount.amount

    with _ledger() as (ledger, _):
        ledger.post(
            portfolio_id=PORTFOLIO,
            transaction_type="TEST",
            occurred_at=AT,
            postings=postings,
            reference={},
        )
        balances = ledger.balances(PORTFOLIO)
        for account, balance in balances.items():
            assert balance.amount == expected[(str(account), balance.currency)]


@SETTINGS
@given(balanced_postings(), balanced_postings())
def test_posting_twice_accumulates_rather_than_replacing(
    first: list[Posting], second: list[Posting]
) -> None:
    with _ledger() as (ledger, _):
        for index, batch in enumerate((first, second)):
            ledger.post(
                portfolio_id=PORTFOLIO,
                transaction_type=f"TEST_{index}",
                occurred_at=AT,
                postings=batch,
                reference={"batch": index},
            )
        expected: dict[tuple[str, str], Decimal] = {}
        for posting in (*first, *second):
            key = (str(posting.account), posting.amount.currency)
            expected[key] = expected.get(key, Decimal(0)) + posting.amount.amount

        for account, balance in ledger.balances(PORTFOLIO).items():
            assert balance.amount == expected[(str(account), balance.currency)]
        ledger.assert_balanced(PORTFOLIO)


@SETTINGS
@given(balanced_postings())
def test_one_portfolios_postings_never_reach_another(postings: list[Posting]) -> None:
    """§30.3 made checkable: two conditions' books are separate, whatever is
    posted to either."""
    with _ledger() as (ledger, _):
        ledger.post(
            portfolio_id=PORTFOLIO,
            transaction_type="TEST",
            occurred_at=AT,
            postings=postings,
            reference={},
        )
        assert ledger.balances("some-other-portfolio") == {}
