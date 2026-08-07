"""Tests for append-only position lots and FIFO cost basis (§17.1, §17.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SqlIntegrityError
from sqlalchemy.orm import Session

from marketlab.accounting.positions import PositionBook
from marketlab.core.failures import AccountingError
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.storage.database import Database

PORTFOLIO = "portfolio-under-test"
OTHER_PORTFOLIO = "another-portfolio"
ALPHA = "id-alpha"


def at(day: int) -> Instant:
    return instant_from_datetime(datetime(2026, 8, day, 20, 0, tzinfo=UTC))


@pytest.fixture
def book(session: Session) -> PositionBook:
    return PositionBook(session)


def _open(
    book: PositionBook,
    quantity: str,
    unit_cost: str,
    *,
    day: int,
    reference_id: str,
    portfolio: str = PORTFOLIO,
    instrument: str = ALPHA,
) -> str:
    return book.open_lot(
        portfolio_id=portfolio,
        instrument_id=instrument,
        quantity=Decimal(quantity),
        unit_cost=Decimal(unit_cost),
        currency="USD",
        occurred_at=at(day),
        reason="FILL",
        reference_id=reference_id,
    )


def _close(book: PositionBook, quantity: str, *, day: int, reference_id: str) -> tuple[object, ...]:
    return book.close_quantity(
        portfolio_id=PORTFOLIO,
        instrument_id=ALPHA,
        quantity=Decimal(quantity),
        occurred_at=at(day),
        reason="FILL",
        reference_id=reference_id,
    )


# ---------------------------------------------------------------------------
# Opening and folding
# ---------------------------------------------------------------------------


def test_an_opened_lot_is_visible_in_the_fold(book: PositionBook) -> None:
    _open(book, "10", "150.00", day=3, reference_id="f-1")
    lots = book.open_lots(PORTFOLIO, ALPHA)
    assert len(lots) == 1
    assert lots[0].quantity == Decimal("10")
    assert lots[0].unit_cost == Decimal("150.00")
    assert lots[0].cost_basis.amount == Decimal("1500.00")


def test_an_instrument_never_traded_has_no_lots(book: PositionBook) -> None:
    assert book.open_lots(PORTFOLIO, ALPHA) == ()
    assert book.quantity_of(PORTFOLIO, ALPHA) == 0


def test_a_non_positive_opening_is_refused(book: PositionBook) -> None:
    """An 'opening' of zero or less is a closing wearing the wrong name."""
    with pytest.raises(AccountingError, match="strictly positive"):
        _open(book, "0", "150.00", day=3, reference_id="f-1")


def test_lots_are_folded_as_of_an_instant(book: PositionBook) -> None:
    """The property that makes the append-only design worth its cost: the book
    is reconstructible for every past instant, not just for now."""
    _open(book, "10", "150.00", day=3, reference_id="f-1")
    _open(book, "5", "160.00", day=5, reference_id="f-2")

    assert book.quantity_of(PORTFOLIO, ALPHA, as_of=at(4)) == Decimal("10")
    assert book.quantity_of(PORTFOLIO, ALPHA, as_of=at(6)) == Decimal("15")


def test_two_portfolios_hold_separate_positions(book: PositionBook) -> None:
    _open(book, "10", "150.00", day=3, reference_id="f-1")
    _open(book, "3", "150.00", day=3, reference_id="f-1", portfolio=OTHER_PORTFOLIO)
    assert book.quantity_of(PORTFOLIO, ALPHA) == Decimal("10")
    assert book.quantity_of(OTHER_PORTFOLIO, ALPHA) == Decimal("3")


def test_held_instruments_lists_only_non_empty_positions(book: PositionBook) -> None:
    _open(book, "10", "150.00", day=3, reference_id="f-1")
    _open(book, "4", "80.00", day=3, reference_id="f-2", instrument="id-beta")
    _close(book, "10", day=4, reference_id="f-3")
    assert book.held_instruments(PORTFOLIO) == ("id-beta",)


# ---------------------------------------------------------------------------
# FIFO
# ---------------------------------------------------------------------------


def test_a_sale_consumes_the_oldest_lot_first(book: PositionBook) -> None:
    _open(book, "10", "100.00", day=3, reference_id="f-1")
    _open(book, "10", "200.00", day=4, reference_id="f-2")

    consumed = _close(book, "4", day=5, reference_id="f-3")
    assert len(consumed) == 1
    assert consumed[0].quantity == Decimal("4")  # type: ignore[attr-defined]
    assert consumed[0].unit_cost == Decimal("100.00")  # type: ignore[attr-defined]


def test_a_sale_spanning_two_lots_reports_each_at_its_own_cost(book: PositionBook) -> None:
    """Cost basis is attributed, never averaged: the realised gain depends on
    which parcel was sold."""
    _open(book, "10", "100.00", day=3, reference_id="f-1")
    _open(book, "10", "200.00", day=4, reference_id="f-2")

    consumed = _close(book, "15", day=5, reference_id="f-3")
    assert [(c.quantity, c.unit_cost) for c in consumed] == [  # type: ignore[attr-defined]
        (Decimal("10"), Decimal("100.00")),
        (Decimal("5"), Decimal("200.00")),
    ]


def test_a_partly_consumed_lot_keeps_its_original_unit_cost(book: PositionBook) -> None:
    _open(book, "10", "100.00", day=3, reference_id="f-1")
    _close(book, "4", day=4, reference_id="f-2")

    lots = book.open_lots(PORTFOLIO, ALPHA)
    assert len(lots) == 1
    assert lots[0].quantity == Decimal("6")
    assert lots[0].unit_cost == Decimal("100.00")


def test_a_fully_consumed_lot_disappears_from_the_fold(book: PositionBook) -> None:
    _open(book, "10", "100.00", day=3, reference_id="f-1")
    _close(book, "10", day=4, reference_id="f-2")
    assert book.open_lots(PORTFOLIO, ALPHA) == ()
    assert book.quantity_of(PORTFOLIO, ALPHA) == 0


def test_closing_more_than_is_held_is_refused(book: PositionBook) -> None:
    """Short positions are not modelled, so an over-close is a bug rather than
    a short sale — silently allowing it would invent an asset."""
    _open(book, "10", "100.00", day=3, reference_id="f-1")
    with pytest.raises(AccountingError, match=r"only 10\.00000000 is open"):
        _close(book, "11", day=4, reference_id="f-2")


def test_closing_against_an_empty_position_is_refused(book: PositionBook) -> None:
    with pytest.raises(AccountingError, match="only 0 is open"):
        _close(book, "1", day=4, reference_id="f-2")


def test_a_non_positive_close_is_refused(book: PositionBook) -> None:
    _open(book, "10", "100.00", day=3, reference_id="f-1")
    with pytest.raises(AccountingError, match="strictly positive"):
        _close(book, "-1", day=4, reference_id="f-2")


def test_a_close_cannot_reach_back_before_the_lot_existed(book: PositionBook) -> None:
    """Lots are folded as of the closing instant, so a lot opened later is not
    available to a sale dated earlier."""
    _open(book, "10", "100.00", day=5, reference_id="f-1")
    with pytest.raises(AccountingError, match="only 0 is open"):
        _close(book, "1", day=4, reference_id="f-2")


# ---------------------------------------------------------------------------
# Fractional quantities
# ---------------------------------------------------------------------------


def test_a_tiny_quantity_is_stored_in_plain_positional_notation(
    book: PositionBook, session: Session
) -> None:
    """Quantising a sub-satoshi quantity yields Decimal("1E-8"), whose str()
    is scientific notation - the exact form marketlab.core.money exists to keep
    out of storage, and which sorts nonsensically as text."""
    _open(book, "0.000000009", "60000.00", day=3, reference_id="f-tiny")
    stored = session.execute(text("SELECT quantity_delta FROM position_events")).scalars().all()
    assert stored == ["0.00000001"]
    assert not any("E" in value or "e" in value for value in stored)


def test_fractional_quantities_survive_a_round_trip(book: PositionBook) -> None:
    """Crypto is fractional by nature; an integer share count would truncate."""
    _open(book, "0.37500000", "60000.00", day=3, reference_id="f-1")
    _close(book, "0.12500000", day=4, reference_id="f-2")
    assert book.quantity_of(PORTFOLIO, ALPHA) == Decimal("0.25000000")


# ---------------------------------------------------------------------------
# Immutability and idempotence
# ---------------------------------------------------------------------------


def test_replaying_the_same_fill_does_not_double_the_position(book: PositionBook) -> None:
    _open(book, "10", "100.00", day=3, reference_id="f-1")
    _open(book, "10", "100.00", day=3, reference_id="f-1")
    assert book.quantity_of(PORTFOLIO, ALPHA) == Decimal("10")


def test_position_events_are_append_only(
    book: PositionBook, session: Session, database: Database
) -> None:
    _open(book, "10", "100.00", day=3, reference_id="f-1")
    session.commit()
    with pytest.raises(SqlIntegrityError, match="append-only"), database.engine.begin() as conn:
        conn.execute(text("UPDATE position_events SET quantity_delta = '999'"))


# ---------------------------------------------------------------------------
# Ordering within one instant
# ---------------------------------------------------------------------------


def test_a_split_and_a_sale_in_the_same_session_preserve_the_cost_basis(
    book: PositionBook,
) -> None:
    """Every event of one session shares that session's cutoff, so the fold
    order is decided by `sequence`, not by the timestamp. Replaying the sale
    before the split would subtract from the same lot twice, drive it negative
    and silently drop it - losing its cost basis outright. Found by
    tests/integration/test_execution_wiring.py, pinned here."""
    _open(book, "100", "150.00", day=3, reference_id="f-1")
    book.apply_split(
        portfolio_id=PORTFOLIO,
        instrument_id=ALPHA,
        ratio=Decimal("2"),
        occurred_at=at(5),
        reference_id="ca-1",
    )
    _close(book, "50", day=5, reference_id="f-2")

    lots = book.open_lots(PORTFOLIO, ALPHA)
    assert book.quantity_of(PORTFOLIO, ALPHA) == Decimal("150")
    assert sum(lot.cost_basis.amount for lot in lots) == Decimal("11250.00")


def test_a_sale_cannot_consume_a_lot_bought_in_the_same_session(
    book: PositionBook,
) -> None:
    """T+N means a same-session purchase is not yours to sell yet, so the fold
    ranks purchases after sales rather than letting one feed the other."""
    _open(book, "10", "100.00", day=3, reference_id="f-1")
    _open(book, "10", "200.00", day=5, reference_id="f-2")
    consumed = _close(book, "10", day=5, reference_id="f-3")
    assert [c.unit_cost for c in consumed] == [Decimal("100.00")]  # type: ignore[attr-defined]
