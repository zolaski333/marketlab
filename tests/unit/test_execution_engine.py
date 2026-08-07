"""Tests for virtual execution, settlement and their accounting (§16, §17)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from marketlab.accounting import accounts
from marketlab.accounting.ledger import Ledger
from marketlab.accounting.positions import PositionBook
from marketlab.accounting.valuation import value_portfolio
from marketlab.agents.decision import TradeIntent
from marketlab.core.clock import FrozenClock
from marketlab.core.failures import AgentFailureKind, SnapshotStatus
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.core.money import Money
from marketlab.execution.engine import ExecutionEngine, portfolio_id_for
from marketlab.execution.policy import ExecutionPolicy, OrderSide
from marketlab.execution.types import ExecutionReport, RejectionReason
from marketlab.instruments.calendars import CalendarRegistry, WeekdaySessionCalendar
from marketlab.instruments.types import (
    AssetClass,
    ExecutionModel,
    InstrumentStatus,
    InstrumentView,
)
from marketlab.models.types import TradeSide
from marketlab.retrieval.types import Evidence, EvidenceKind, RetrievalIndex
from marketlab.storage.events import EventStore

ALPHA = "id-alpha"
GAMMA = "id-gamma"  # quoted in EUR, so nothing may assume a single currency
CAL = "TEST_CAL"
BUNDLE = "b" * 64
PORTFOLIO = portfolio_id_for("RUN", "A", 0)
OTHER_PORTFOLIO = portfolio_id_for("RUN", "B", 0)


def usd(amount: str) -> Money:
    return Money(Decimal(amount), "USD")


def eur(amount: str) -> Money:
    return Money(Decimal(amount), "EUR")


def session_close(day: int) -> Instant:
    """A Monday-Friday 16:00 New York close, in UTC."""
    return instant_from_datetime(datetime(2026, 8, day, 20, 0, tzinfo=UTC))


@dataclass
class Rig:
    engine: ExecutionEngine
    ledger: Ledger
    positions: PositionBook
    session: Session


def _calendars() -> CalendarRegistry:
    registry = CalendarRegistry()
    registry.register(
        WeekdaySessionCalendar(
            code=CAL,
            version="test-1",
            iana_tz="America/New_York",
            session_open=time(9, 30),
            session_close=time(16, 0),
        )
    )
    return registry


def _view(
    instrument_id: str = ALPHA,
    *,
    currency: str = "USD",
    status: InstrumentStatus = InstrumentStatus.ACTIVE,
    execution_model: ExecutionModel = ExecutionModel.LEVEL_A_REAL_QUOTES,
    settlement_days: int = 2,
) -> InstrumentView:
    return InstrumentView(
        instrument_id=instrument_id,
        asset_class=AssetClass.EQUITY,
        version_number=1,
        ticker=instrument_id.upper(),
        name=instrument_id,
        quote_currency=currency,
        native_timezone="America/New_York",
        calendar_code=CAL,
        settlement_days=settlement_days,
        status=status,
        execution_model=execution_model,
        effective_from=session_close(3),
    )


def _price(
    instrument_id: str, bid: str, ask: str, *, at: Instant, volume: int = 1_000_000
) -> Evidence:
    return Evidence(
        evidence_id=f"ev-price-{instrument_id}-{at}",
        kind=EvidenceKind.PRICE_BAR,
        subject_ids=(instrument_id,),
        as_of=at,
        first_seen_at=at,
        blob_hash="a" * 64,
        headline=f"{instrument_id} close",
        fields={
            "bid": bid,
            "ask": ask,
            "close": str((Decimal(bid) + Decimal(ask)) / 2),
            "volume": volume,
        },
    )


def _fx(rate: str, *, at: Instant) -> Evidence:
    return Evidence(
        evidence_id=f"ev-fx-{at}",
        kind=EvidenceKind.FX_RATE,
        subject_ids=("EUR_USD",),
        as_of=at,
        first_seen_at=at,
        blob_hash="c" * 64,
        headline="EUR_USD",
        fields={"pair": "EUR_USD", "rate": rate},
    )


def _index(
    *,
    at: Instant,
    views: tuple[InstrumentView, ...] = (),
    evidence: tuple[Evidence, ...] = (),
) -> RetrievalIndex:
    return RetrievalIndex(
        snapshot_id=f"snap-{at}",
        cutoff=at,
        status=SnapshotStatus.COMPLETE,
        universe=views or (_view(),),
        evidence=evidence or (_price(ALPHA, "99.95", "100.05", at=at),),
    )


@pytest.fixture
def rig(session: Session, clock: FrozenClock) -> Rig:
    ledger = Ledger(session, clock)
    positions = PositionBook(session)
    engine = ExecutionEngine(
        session=session,
        clock=clock,
        events=EventStore(session, clock),
        ledger=ledger,
        positions=positions,
        calendars=_calendars(),
        policy=ExecutionPolicy(target_weight=Decimal("0.10")),
    )
    engine.fund(PORTFOLIO, [usd("100000.00")], at=session_close(3))
    return Rig(engine=engine, ledger=ledger, positions=positions, session=session)


def _buy(instrument_id: str = ALPHA) -> TradeIntent:
    return TradeIntent(instrument_id, TradeSide.BUY, "test", ("ev-1",))


def _sell(instrument_id: str = ALPHA) -> TradeIntent:
    return TradeIntent(instrument_id, TradeSide.SELL, "test", ("ev-1",))


# ---------------------------------------------------------------------------
# Funding
# ---------------------------------------------------------------------------


def test_funding_opens_a_balanced_book(rig: Rig) -> None:
    assert rig.ledger.balance(PORTFOLIO, accounts.cash("USD")) == usd("100000.00")
    rig.ledger.assert_balanced(PORTFOLIO)


def test_a_book_can_be_funded_in_several_currencies(rig: Rig) -> None:
    """The universe holds a EUR-quoted instrument, so nothing may assume one
    currency."""
    rig.engine.fund(OTHER_PORTFOLIO, [usd("50000.00"), eur("40000.00")], at=session_close(3))
    assert rig.ledger.balance(OTHER_PORTFOLIO, accounts.cash("USD")) == usd("50000.00")
    assert rig.ledger.balance(OTHER_PORTFOLIO, accounts.cash("EUR")) == eur("40000.00")
    rig.ledger.assert_balanced(OTHER_PORTFOLIO)


def test_funding_twice_does_not_double_the_capital(rig: Rig) -> None:
    """§30.6: a resumed run must not hand one condition twice the money."""
    rig.engine.fund(PORTFOLIO, [usd("100000.00")], at=session_close(3))
    assert rig.ledger.balance(PORTFOLIO, accounts.cash("USD")) == usd("100000.00")


# ---------------------------------------------------------------------------
# Placement: never at the moment of decision
# ---------------------------------------------------------------------------


def test_an_order_may_not_execute_at_or_before_the_decision(rig: Rig) -> None:
    """§16.2, the rule this whole module is built around: filling at the
    decision price measures foresight, not skill."""
    decided_at = session_close(3)
    orders = rig.engine.place_orders(
        [_buy()],
        portfolio_id=PORTFOLIO,
        bundle_id=BUNDLE,
        index=_index(at=decided_at),
        decided_at=decided_at,
    )
    assert orders[0].execute_after > decided_at


def test_a_hold_intent_produces_no_order(rig: Rig) -> None:
    intent = TradeIntent(ALPHA, TradeSide.HOLD, "wait", ())
    orders = rig.engine.place_orders(
        [intent],
        portfolio_id=PORTFOLIO,
        bundle_id=BUNDLE,
        index=_index(at=session_close(3)),
        decided_at=session_close(3),
    )
    assert orders == ()


def test_a_buy_is_sized_to_the_target_weight_of_equity(rig: Rig) -> None:
    # 10% of 100,000 equity is 10,000; at an ask of 100.05 that is 99.95 units,
    # rounded down so the cash on hand definitely covers it.
    at = session_close(3)
    orders = rig.engine.place_orders(
        [_buy()],
        portfolio_id=PORTFOLIO,
        bundle_id=BUNDLE,
        index=_index(at=at),
        decided_at=at,
    )
    assert orders[0].quantity == Decimal("99.95002498")


def test_a_sell_with_no_position_is_rejected_without_shorting(rig: Rig) -> None:
    """Not an agent failure: shorts simply are not modelled."""
    at = session_close(3)
    rig.engine.place_orders(
        [_sell()], portfolio_id=PORTFOLIO, bundle_id=BUNDLE, index=_index(at=at), decided_at=at
    )
    report = rig.engine.execute_due(
        portfolio_id=PORTFOLIO, index=_index(at=session_close(4)), now=session_close(4)
    )
    assert report.fills == ()
    assert rig.engine.drain_failures() == ()


# ---------------------------------------------------------------------------
# Filling and the double entry it produces
# ---------------------------------------------------------------------------


def _place_and_fill(
    rig: Rig, intents: list[TradeIntent], *, decide_day: int = 3, fill_day: int = 4
) -> ExecutionReport:
    decided_at = session_close(decide_day)
    rig.engine.place_orders(
        intents,
        portfolio_id=PORTFOLIO,
        bundle_id=BUNDLE,
        index=_index(at=decided_at),
        decided_at=decided_at,
    )
    now = session_close(fill_day)
    return rig.engine.execute_due(portfolio_id=PORTFOLIO, index=_index(at=now), now=now)


def test_a_buy_fills_at_the_ask_and_books_a_payable_not_cash(rig: Rig) -> None:
    """Cash does not move on trade date — that is what T+N means."""
    report = _place_and_fill(rig, [_buy()])
    assert len(report.fills) == 1
    fill = report.fills[0]
    assert fill.side is OrderSide.BUY
    assert fill.price == usd("100.05")

    gross = fill.gross
    assert rig.ledger.balance(PORTFOLIO, accounts.position(ALPHA, "USD")) == gross
    assert rig.ledger.balance(PORTFOLIO, accounts.cash("USD")) == usd("100000.00")
    assert rig.ledger.balance(PORTFOLIO, accounts.payable("USD")) == -(gross + fill.fee)
    rig.ledger.assert_balanced(PORTFOLIO)


def test_a_fill_opens_a_position_lot_at_the_price_paid(rig: Rig) -> None:
    report = _place_and_fill(rig, [_buy()])
    lots = rig.positions.open_lots(PORTFOLIO, ALPHA)
    assert len(lots) == 1
    assert lots[0].quantity == report.fills[0].quantity
    assert lots[0].unit_cost == Decimal("100.05")


def test_the_spread_is_reported_but_never_posted_as_a_fee(rig: Rig) -> None:
    """Posting it would charge the spread twice: it is already inside the
    fill price."""
    report = _place_and_fill(rig, [_buy()])
    fill = report.fills[0]
    assert fill.slippage.amount > 0
    assert rig.ledger.balance(PORTFOLIO, accounts.fees("USD")) == fill.fee


def test_settlement_moves_the_cash_and_clears_the_payable(rig: Rig) -> None:
    report = _place_and_fill(rig, [_buy()])
    fill = report.fills[0]
    settled = rig.engine.settle_due(portfolio_id=PORTFOLIO, now=fill.settles_at)

    assert settled == (fill.fill_id,)
    assert rig.ledger.balance(PORTFOLIO, accounts.payable("USD")).is_zero()
    assert rig.ledger.balance(PORTFOLIO, accounts.cash("USD")) == usd("100000.00") - (
        fill.gross + fill.fee
    )
    rig.ledger.assert_balanced(PORTFOLIO)


def test_settlement_does_not_happen_early(rig: Rig) -> None:
    report = _place_and_fill(rig, [_buy()])
    fill = report.fills[0]
    assert fill.settles_at > fill.executed_at
    assert rig.engine.settle_due(portfolio_id=PORTFOLIO, now=fill.executed_at) == ()


def test_settling_twice_moves_the_cash_once(rig: Rig) -> None:
    report = _place_and_fill(rig, [_buy()])
    fill = report.fills[0]
    rig.engine.settle_due(portfolio_id=PORTFOLIO, now=fill.settles_at)
    balance = rig.ledger.balance(PORTFOLIO, accounts.cash("USD"))
    assert rig.engine.settle_due(portfolio_id=PORTFOLIO, now=fill.settles_at) == ()
    assert rig.ledger.balance(PORTFOLIO, accounts.cash("USD")) == balance


def test_settlement_skips_days_the_market_is_shut(rig: Rig) -> None:
    """T+2 over a weekend is four calendar days; a fixed 48-hour offset would
    settle cash on a day the exchange was closed."""
    # 2026-08-06 is a Thursday, so T+2 lands on Monday 2026-08-10.
    report = _place_and_fill(rig, [_buy()], decide_day=5, fill_day=6)
    settles = report.fills[0].settles_at
    assert "2026-08-10" in str(settles)


# ---------------------------------------------------------------------------
# Selling: FIFO cost basis and realised P&L
# ---------------------------------------------------------------------------


def test_a_sale_realises_profit_against_the_cost_basis_actually_paid(rig: Rig) -> None:
    _place_and_fill(rig, [_buy()])
    bought = rig.positions.quantity_of(PORTFOLIO, ALPHA)

    # Price rises; sell the whole position at the new bid.
    decided_at = session_close(5)
    higher = _index(at=decided_at, evidence=(_price(ALPHA, "119.95", "120.05", at=decided_at),))
    rig.engine.place_orders(
        [_sell()],
        portfolio_id=PORTFOLIO,
        bundle_id="c" * 64,
        index=higher,
        decided_at=decided_at,
    )
    now = session_close(6)
    at_fill = _index(at=now, evidence=(_price(ALPHA, "119.95", "120.05", at=now),))
    report = rig.engine.execute_due(portfolio_id=PORTFOLIO, index=at_fill, now=now)

    fill = report.fills[0]
    assert fill.side is OrderSide.SELL
    assert fill.quantity == bought
    assert fill.price == usd("119.95")
    # Sold at 119.95 against a 100.05 basis.
    assert fill.realized_pnl == (usd("119.95") - usd("100.05")).scaled_by(bought).quantized()
    assert rig.positions.quantity_of(PORTFOLIO, ALPHA) == 0
    rig.ledger.assert_balanced(PORTFOLIO)


def test_a_sale_books_a_receivable_until_it_settles(rig: Rig) -> None:
    _place_and_fill(rig, [_buy()])
    decided_at = session_close(5)
    index = _index(at=decided_at)
    rig.engine.place_orders(
        [_sell()], portfolio_id=PORTFOLIO, bundle_id="c" * 64, index=index, decided_at=decided_at
    )
    now = session_close(6)
    report = rig.engine.execute_due(portfolio_id=PORTFOLIO, index=_index(at=now), now=now)
    fill = report.fills[0]

    assert rig.ledger.balance(PORTFOLIO, accounts.receivable("USD")) == fill.gross - fill.fee
    rig.engine.settle_due(portfolio_id=PORTFOLIO, now=fill.settles_at)
    assert rig.ledger.balance(PORTFOLIO, accounts.receivable("USD")).is_zero()


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def _fill_against(
    rig: Rig, index: RetrievalIndex, *, decided_at: Instant, now: Instant
) -> ExecutionReport:
    rig.engine.place_orders(
        [_buy()],
        portfolio_id=PORTFOLIO,
        bundle_id=BUNDLE,
        index=_index(at=decided_at),
        decided_at=decided_at,
    )
    return rig.engine.execute_due(portfolio_id=PORTFOLIO, index=index, now=now)


def test_a_suspended_instrument_is_not_filled_and_is_an_agent_failure(rig: Rig) -> None:
    now = session_close(4)
    suspended = _index(
        at=now,
        views=(_view(status=InstrumentStatus.SUSPENDED),),
        evidence=(_price(ALPHA, "99.95", "100.05", at=now),),
    )
    report = _fill_against(rig, suspended, decided_at=session_close(3), now=now)

    assert report.fills == ()
    assert report.rejections[0].reason is RejectionReason.NOT_TRADABLE
    assert [f.kind for f in rig.engine.drain_failures()] == [
        AgentFailureKind.NON_TRADABLE_INSTRUMENT
    ]


def test_an_instrument_with_no_honest_fill_model_is_refused(rig: Rig) -> None:
    """§16.4: research remains possible, execution does not."""
    now = session_close(4)
    unsupported = _index(
        at=now,
        views=(_view(execution_model=ExecutionModel.UNSUPPORTED),),
        evidence=(_price(ALPHA, "99.95", "100.05", at=now),),
    )
    report = _fill_against(rig, unsupported, decided_at=session_close(3), now=now)
    assert report.rejections[0].reason is RejectionReason.UNSUPPORTED_EXECUTION
    assert [f.kind for f in rig.engine.drain_failures()] == [AgentFailureKind.UNSUPPORTED_EXECUTION]


def test_a_window_with_no_price_cannot_be_filled(rig: Rig) -> None:
    now = session_close(4)
    empty = RetrievalIndex(
        snapshot_id="snap-empty",
        cutoff=now,
        status=SnapshotStatus.DEGRADED,
        universe=(_view(),),
        evidence=(),
    )
    report = _fill_against(rig, empty, decided_at=session_close(3), now=now)
    assert report.rejections[0].reason is RejectionReason.NO_EXECUTION_QUOTE


def test_stale_data_blocks_execution(rig: Rig) -> None:
    """A price from an earlier session is not a price you can trade on."""
    now = session_close(4)
    stale = RetrievalIndex(
        snapshot_id="snap-stale",
        cutoff=now,
        status=SnapshotStatus.DEGRADED,
        universe=(_view(),),
        evidence=(_price(ALPHA, "99.95", "100.05", at=session_close(3)),),
    )
    report = _fill_against(rig, stale, decided_at=session_close(3), now=now)
    assert report.rejections[0].reason is RejectionReason.NOT_TRADABLE


def test_a_thin_market_caps_the_fill_below_what_was_asked(rig: Rig) -> None:
    """§16.3: an order that swallowed a day's whole volume would be a fill
    nobody could have obtained."""
    now = session_close(4)
    thin = _index(at=now, evidence=(_price(ALPHA, "99.95", "100.05", at=now, volume=100),))
    report = _fill_against(rig, thin, decided_at=session_close(3), now=now)

    fill = report.fills[0]
    assert fill.is_partial
    assert fill.quantity == Decimal("5")  # 5% of 100
    assert fill.requested_quantity > fill.quantity


def test_a_market_with_no_volume_rejects_rather_than_filling(rig: Rig) -> None:
    now = session_close(4)
    dead = _index(at=now, evidence=(_price(ALPHA, "99.95", "100.05", at=now, volume=0),))
    report = _fill_against(rig, dead, decided_at=session_close(3), now=now)
    assert report.rejections[0].reason is RejectionReason.LIQUIDITY_EXHAUSTED


def test_an_order_is_not_filled_twice(rig: Rig) -> None:
    now = session_close(4)
    _place_and_fill(rig, [_buy()])
    again = rig.engine.execute_due(portfolio_id=PORTFOLIO, index=_index(at=now), now=now)
    assert again.fills == ()


def test_a_rejected_order_is_not_retried_on_the_next_pass(rig: Rig) -> None:
    now = session_close(4)
    dead = _index(at=now, evidence=(_price(ALPHA, "99.95", "100.05", at=now, volume=0),))
    _fill_against(rig, dead, decided_at=session_close(3), now=now)
    later = session_close(5)
    healthy = _index(at=later, evidence=(_price(ALPHA, "99.95", "100.05", at=later),))
    assert rig.engine.execute_due(portfolio_id=PORTFOLIO, index=healthy, now=later).fills == ()


# ---------------------------------------------------------------------------
# Isolation between conditions
# ---------------------------------------------------------------------------


def test_one_conditions_trading_does_not_touch_anothers_book(rig: Rig) -> None:
    """§30.3. The books are separated by portfolio_id, so this is structural
    rather than a matter of care."""
    rig.engine.fund(OTHER_PORTFOLIO, [usd("100000.00")], at=session_close(3))
    _place_and_fill(rig, [_buy()])

    assert rig.ledger.balance(OTHER_PORTFOLIO, accounts.cash("USD")) == usd("100000.00")
    assert rig.ledger.balance(OTHER_PORTFOLIO, accounts.position(ALPHA, "USD")).is_zero()
    assert rig.positions.quantity_of(OTHER_PORTFOLIO, ALPHA) == 0


def test_executing_one_book_does_not_fill_anothers_orders(rig: Rig) -> None:
    rig.engine.fund(OTHER_PORTFOLIO, [usd("100000.00")], at=session_close(3))
    decided_at = session_close(3)
    rig.engine.place_orders(
        [_buy()],
        portfolio_id=OTHER_PORTFOLIO,
        bundle_id="d" * 64,
        index=_index(at=decided_at),
        decided_at=decided_at,
    )
    now = session_close(4)
    report = rig.engine.execute_due(portfolio_id=PORTFOLIO, index=_index(at=now), now=now)
    assert report.fills == ()


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------


def test_equity_is_preserved_by_a_fill_apart_from_costs(rig: Rig) -> None:
    """A trade converts cash into holdings; it does not create or destroy
    equity beyond the fee and the spread actually paid."""
    at = session_close(3)
    index = _index(at=at, evidence=(_price(ALPHA, "99.95", "100.05", at=at), _fx("1.10", at=at)))
    before = value_portfolio(
        rig.ledger, rig.positions, index, portfolio_id=PORTFOLIO, base_currency="USD"
    )

    _place_and_fill(rig, [_buy()])
    now = session_close(4)
    after_index = _index(
        at=now, evidence=(_price(ALPHA, "99.95", "100.05", at=now), _fx("1.10", at=now))
    )
    after = value_portfolio(
        rig.ledger, rig.positions, after_index, portfolio_id=PORTFOLIO, base_currency="USD"
    )

    lost = (before.equity - after.equity).amount
    assert lost > 0  # fee plus half the spread
    assert lost < Decimal("100")


def test_a_holding_with_no_price_makes_valuation_refuse_rather_than_guess(rig: Rig) -> None:
    _place_and_fill(rig, [_buy()])
    now = session_close(5)
    blind = RetrievalIndex(
        snapshot_id="snap-blind",
        cutoff=now,
        status=SnapshotStatus.DEGRADED,
        universe=(_view(),),
        evidence=(),
    )
    with pytest.raises(Exception, match="no price in snapshot"):
        value_portfolio(
            rig.ledger, rig.positions, blind, portfolio_id=PORTFOLIO, base_currency="USD"
        )


def test_valuing_a_foreign_holding_without_a_rate_refuses(rig: Rig) -> None:
    """§17.3: an implicit conversion is exactly what is forbidden."""
    at = session_close(3)
    gamma_view = replace(_view(GAMMA, currency="EUR"), instrument_id=GAMMA)
    index = _index(at=at, views=(gamma_view,), evidence=(_price(GAMMA, "84.95", "85.05", at=at),))
    with pytest.raises(Exception, match="No USD->EUR rate"):
        rig.engine.place_orders(
            [_buy(GAMMA)],
            portfolio_id=PORTFOLIO,
            bundle_id=BUNDLE,
            index=index,
            decided_at=at,
        )
