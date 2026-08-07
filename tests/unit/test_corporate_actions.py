"""Tests for applying corporate actions to books and reference data (§17.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from marketlab.accounting import accounts
from marketlab.accounting.ledger import Ledger
from marketlab.accounting.positions import PositionBook
from marketlab.core.clock import FrozenClock
from marketlab.core.cutoff import Cutoff
from marketlab.core.failures import AccountingError, SnapshotStatus
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.core.money import Money
from marketlab.execution.corporate import CorporateActionApplier
from marketlab.instruments.repository import InstrumentRepository
from marketlab.instruments.types import (
    AssetClass,
    ExecutionModel,
    InstrumentStatus,
    InstrumentView,
)
from marketlab.retrieval.types import Evidence, EvidenceKind, RetrievalIndex
from marketlab.storage.events import EventStore

ALPHA = "id-alpha"
PORTFOLIO = "portfolio-under-test"
OTHER_PORTFOLIO = "another-portfolio"


def at(day: int) -> Instant:
    return instant_from_datetime(datetime(2026, 8, day, 20, 0, tzinfo=UTC))


def usd(amount: str) -> Money:
    return Money(Decimal(amount), "USD")


@dataclass
class Rig:
    applier: CorporateActionApplier
    ledger: Ledger
    positions: PositionBook
    repo: InstrumentRepository
    session: Session


@pytest.fixture
def rig(session: Session, clock: FrozenClock) -> Rig:
    ledger = Ledger(session, clock)
    positions = PositionBook(session)
    repo = InstrumentRepository(session, clock)
    applier = CorporateActionApplier(
        session=session,
        clock=clock,
        events=EventStore(session, clock),
        ledger=ledger,
        positions=positions,
        instruments=repo,
    )
    return Rig(applier=applier, ledger=ledger, positions=positions, repo=repo, session=session)


def _action(
    action_type: str, details: dict[str, object], *, effective_at: Instant, suffix: str = ""
) -> Evidence:
    return Evidence(
        evidence_id=f"ev-ca-{action_type}{suffix}",
        kind=EvidenceKind.CORPORATE_ACTION,
        subject_ids=(ALPHA,),
        as_of=effective_at,
        first_seen_at=effective_at,
        blob_hash="a" * 64,
        headline=f"{ALPHA} {action_type}",
        fields={
            "instrument_id": ALPHA,
            "action_type": action_type,
            "effective_at": str(effective_at),
            "details": details,
        },
    )


def _view(ticker: str = "ALPHA") -> InstrumentView:
    return InstrumentView(
        instrument_id=ALPHA,
        asset_class=AssetClass.EQUITY,
        version_number=1,
        ticker=ticker,
        name="Alpha",
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="TEST_CAL",
        settlement_days=2,
        status=InstrumentStatus.ACTIVE,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        effective_from=at(1),
    )


def _index(*evidence: Evidence, cutoff: Instant) -> RetrievalIndex:
    return RetrievalIndex(
        snapshot_id=f"snap-{cutoff}",
        cutoff=cutoff,
        status=SnapshotStatus.COMPLETE,
        universe=(_view(),),
        evidence=evidence,
    )


def _hold(
    rig: Rig, quantity: str, unit_cost: str = "100.00", *, portfolio: str = PORTFOLIO
) -> None:
    rig.positions.open_lot(
        portfolio_id=portfolio,
        instrument_id=ALPHA,
        quantity=Decimal(quantity),
        unit_cost=Decimal(unit_cost),
        currency="USD",
        occurred_at=at(3),
        reason="FILL",
        reference_id=f"f-{quantity}-{unit_cost}-{portfolio}",
    )


# ---------------------------------------------------------------------------
# Dividends
# ---------------------------------------------------------------------------


def _dividend(amount: str = "0.50", *, effective_at: Instant) -> Evidence:
    return _action(
        "CASH_DIVIDEND",
        {"amount_per_share": amount, "currency": "USD", "pay_at_session_index": 12},
        effective_at=effective_at,
    )


def test_a_dividend_pays_the_holder_in_proportion_to_the_position(rig: Rig) -> None:
    _hold(rig, "100")
    applied = rig.applier.apply_to_portfolio(
        _index(_dividend(effective_at=at(5)), cutoff=at(5)), portfolio_id=PORTFOLIO
    )
    assert len(applied) == 1
    assert rig.ledger.balance(PORTFOLIO, accounts.cash("USD")) == usd("50.00")
    assert rig.ledger.balance(PORTFOLIO, accounts.dividend_income("USD")) == usd("-50.00")
    rig.ledger.assert_balanced(PORTFOLIO)


def test_a_dividend_does_not_pay_a_book_holding_nothing(rig: Rig) -> None:
    applied = rig.applier.apply_to_portfolio(
        _index(_dividend(effective_at=at(5)), cutoff=at(5)), portfolio_id=PORTFOLIO
    )
    assert applied == ()
    assert rig.ledger.balance(PORTFOLIO, accounts.cash("USD")).is_zero()


def test_each_condition_is_paid_on_its_own_holding(rig: Rig) -> None:
    """§30.3: entitlement is computed from one book's position and nothing else."""
    _hold(rig, "100")
    _hold(rig, "40", portfolio=OTHER_PORTFOLIO)
    index = _index(_dividend(effective_at=at(5)), cutoff=at(5))

    rig.applier.apply_to_portfolio(index, portfolio_id=PORTFOLIO)
    rig.applier.apply_to_portfolio(index, portfolio_id=OTHER_PORTFOLIO)

    assert rig.ledger.balance(PORTFOLIO, accounts.cash("USD")) == usd("50.00")
    assert rig.ledger.balance(OTHER_PORTFOLIO, accounts.cash("USD")) == usd("20.00")


def test_a_dividend_is_paid_once_however_often_the_cycle_is_replayed(rig: Rig) -> None:
    """§30.6. The failure this prevents is a resumed run quietly paying twice."""
    _hold(rig, "100")
    index = _index(_dividend(effective_at=at(5)), cutoff=at(5))
    rig.applier.apply_to_portfolio(index, portfolio_id=PORTFOLIO)
    assert rig.applier.apply_to_portfolio(index, portfolio_id=PORTFOLIO) == ()
    assert rig.ledger.balance(PORTFOLIO, accounts.cash("USD")) == usd("50.00")


def test_a_past_dividend_carried_in_a_later_snapshot_is_not_paid_again(rig: Rig) -> None:
    """A snapshot is cumulative: it still carries every earlier session's
    actions. Re-applying them would pay one dividend once per later cycle."""
    _hold(rig, "100")
    old = _dividend(effective_at=at(5))
    rig.applier.apply_to_portfolio(_index(old, cutoff=at(5)), portfolio_id=PORTFOLIO)
    later = rig.applier.apply_to_portfolio(_index(old, cutoff=at(6)), portfolio_id=PORTFOLIO)
    assert later == ()
    assert rig.ledger.balance(PORTFOLIO, accounts.cash("USD")) == usd("50.00")


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------


def _split(ratio: str = "2", *, effective_at: Instant) -> Evidence:
    return _action("STOCK_SPLIT", {"split_ratio": ratio}, effective_at=effective_at)


def test_a_split_multiplies_the_quantity_and_preserves_the_cost_basis(rig: Rig) -> None:
    """A split re-denominates a holding; it does not change what it is worth.
    Booking a gain here would invent one out of a renaming."""
    _hold(rig, "100", "150.00")
    rig.applier.apply_to_portfolio(
        _index(_split("2", effective_at=at(5)), cutoff=at(5)), portfolio_id=PORTFOLIO
    )
    lots = rig.positions.open_lots(PORTFOLIO, ALPHA)
    assert rig.positions.quantity_of(PORTFOLIO, ALPHA) == Decimal("200")
    assert lots[0].unit_cost == Decimal("75.00")
    assert sum(lot.cost_basis.amount for lot in lots) == Decimal("15000.00")


def test_a_split_posts_nothing_to_the_ledger(rig: Rig) -> None:
    _hold(rig, "100", "150.00")
    rig.applier.apply_to_portfolio(
        _index(_split("2", effective_at=at(5)), cutoff=at(5)), portfolio_id=PORTFOLIO
    )
    assert rig.ledger.transactions(PORTFOLIO) == []


def test_a_split_keeps_lots_separate_so_fifo_order_survives(rig: Rig) -> None:
    """Collapsing the lots would preserve the total cost basis but quietly
    change the realised gain of every future sale."""
    _hold(rig, "10", "100.00")
    _hold(rig, "10", "200.00")
    rig.applier.apply_to_portfolio(
        _index(_split("2", effective_at=at(5)), cutoff=at(5)), portfolio_id=PORTFOLIO
    )
    lots = rig.positions.open_lots(PORTFOLIO, ALPHA)
    assert [(lot.quantity, lot.unit_cost) for lot in lots] == [
        (Decimal("20"), Decimal("50.00")),
        (Decimal("20"), Decimal("100.00")),
    ]


def test_a_split_is_applied_once(rig: Rig) -> None:
    _hold(rig, "100", "150.00")
    index = _index(_split("2", effective_at=at(5)), cutoff=at(5))
    rig.applier.apply_to_portfolio(index, portfolio_id=PORTFOLIO)
    rig.applier.apply_to_portfolio(index, portfolio_id=PORTFOLIO)
    assert rig.positions.quantity_of(PORTFOLIO, ALPHA) == Decimal("200")


def test_a_non_positive_split_ratio_is_refused(rig: Rig) -> None:
    _hold(rig, "100", "150.00")
    with pytest.raises(AccountingError, match="Split ratio must be positive"):
        rig.applier.apply_to_portfolio(
            _index(_split("0", effective_at=at(5)), cutoff=at(5)), portfolio_id=PORTFOLIO
        )


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


def _admit(rig: Rig) -> str:
    view = rig.repo.admit(
        asset_class=AssetClass.EQUITY,
        ticker="ALPHA",
        name="Alpha",
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="TEST_CAL",
        settlement_days=2,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        at=at(1),
    )
    return view.instrument_id


def _ticker_change(instrument_id: str, new_ticker: str, *, effective_at: Instant) -> Evidence:
    evidence = _action("TICKER_CHANGE", {"new_ticker": new_ticker}, effective_at=effective_at)
    fields = dict(evidence.fields)
    fields["instrument_id"] = instrument_id
    return Evidence(
        evidence_id=evidence.evidence_id,
        kind=evidence.kind,
        subject_ids=(instrument_id,),
        as_of=evidence.as_of,
        first_seen_at=evidence.first_seen_at,
        blob_hash=evidence.blob_hash,
        headline=evidence.headline,
        fields=fields,
    )


def test_a_ticker_change_reaches_the_instrument_repository(rig: Rig) -> None:
    instrument_id = _admit(rig)
    index = _index(_ticker_change(instrument_id, "ALPHA_2", effective_at=at(5)), cutoff=at(5))
    applied = rig.applier.apply_to_reference_data(index)

    assert len(applied) == 1
    before = rig.repo.resolve(instrument_id, Cutoff(as_of=at(4)))
    after = rig.repo.resolve(instrument_id, Cutoff(as_of=at(6)))
    assert before is not None and before.ticker == "ALPHA"
    assert after is not None and after.ticker == "ALPHA_2"


def test_a_ticker_change_is_applied_once_for_the_whole_study(rig: Rig) -> None:
    """It is a fact about the world, identical for every condition; applying
    it per arm would be six writes of the same fact."""
    instrument_id = _admit(rig)
    index = _index(_ticker_change(instrument_id, "ALPHA_2", effective_at=at(5)), cutoff=at(5))
    rig.applier.apply_to_reference_data(index)
    assert rig.applier.apply_to_reference_data(index) == ()


def test_a_dividend_is_not_mistaken_for_reference_data(rig: Rig) -> None:
    _hold(rig, "100")
    assert (
        rig.applier.apply_to_reference_data(_index(_dividend(effective_at=at(5)), cutoff=at(5)))
        == ()
    )
