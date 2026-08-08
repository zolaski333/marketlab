"""Every rejection reason, produced and correctly classified (§16.4, §23.3).

``test_execution_engine.py`` checks what a rejection *does* to the books. This
checks what it is *called*, and whether it counts as an observation about the
agent — which §23.3 makes a scientific result rather than an operational
detail.

The distinction is the whole point of :data:`marketlab.execution.engine._AGENT_FAILING_REJECTIONS`.
An agent that orders a suspended instrument has hallucinated something about
the world; an agent that says SELL on something it does not hold has not, since
short positions are simply not modelled here. Recording the second as an agent
failure would inflate the failure rate of every arm that ever wanted to sell,
and that rate is one of the study's outcome measures.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from marketlab.accounting.ledger import Ledger
from marketlab.accounting.positions import PositionBook
from marketlab.core.clock import FrozenClock
from marketlab.core.failures import AgentFailureKind
from marketlab.execution.engine import ExecutionEngine, OrderRejectionRow
from marketlab.execution.policy import ExecutionPolicy
from marketlab.execution.types import RejectionReason
from marketlab.instruments.types import ExecutionModel, InstrumentStatus
from marketlab.storage.events import EventStore
from tests.unit.test_execution_engine import (
    ALPHA,
    BUNDLE,
    PORTFOLIO,
    Rig,
    _buy,
    _calendars,
    _index,
    _price,
    _sell,
    _view,
    session_close,
    usd,
)


def _rig(session: Session, clock: FrozenClock, *, capital: str, weight: str = "0.10") -> Rig:
    ledger = Ledger(session, clock)
    positions = PositionBook(session)
    engine = ExecutionEngine(
        session=session,
        clock=clock,
        events=EventStore(session, clock),
        ledger=ledger,
        positions=positions,
        calendars=_calendars(),
        policy=ExecutionPolicy(target_weight=Decimal(weight), minimum_notional=Decimal("100")),
    )
    if Decimal(capital) > 0:
        engine.fund(PORTFOLIO, [usd(capital)], at=session_close(3))
    return Rig(engine=engine, ledger=ledger, positions=positions, session=session)


def _reasons(rig: Rig) -> list[str]:
    return [
        row.reason
        for row in rig.session.execute(select(OrderRejectionRow)).scalars()
        if row.portfolio_id == PORTFOLIO
    ]


def _place(rig: Rig, intent: object, *, day: int = 3) -> None:
    at = session_close(day)
    rig.engine.place_orders(
        [intent],  # type: ignore[list-item]
        portfolio_id=PORTFOLIO,
        bundle_id=BUNDLE,
        index=_index(at=at),
        decided_at=at,
    )


# ---------------------------------------------------------------------------
# Rejections that are the agent's doing
# ---------------------------------------------------------------------------


def test_a_buy_with_an_empty_book_is_insufficient_cash_and_an_agent_failure(
    session: Session, clock: FrozenClock
) -> None:
    """An unfunded condition that keeps ordering is producing observations,
    not merely failing to trade."""
    rig = _rig(session, clock, capital="0")
    _place(rig, _buy())

    assert RejectionReason.INSUFFICIENT_CASH in _reasons(rig)
    kinds = {failure.kind for failure in rig.engine.drain_failures()}
    assert AgentFailureKind.INSUFFICIENT_CASH in kinds


def test_a_suspended_instrument_is_not_tradable_and_is_an_agent_failure(
    session: Session, clock: FrozenClock
) -> None:
    rig = _rig(session, clock, capital="100000.00")
    at = session_close(3)
    rig.engine.place_orders(
        [_buy()], portfolio_id=PORTFOLIO, bundle_id=BUNDLE, index=_index(at=at), decided_at=at
    )
    rig.engine.drain_failures()

    later = session_close(4)
    rig.engine.execute_due(
        portfolio_id=PORTFOLIO,
        index=_index(
            at=later,
            views=(_view(status=InstrumentStatus.SUSPENDED),),
            evidence=(_price(ALPHA, "99.95", "100.05", at=later),),
        ),
        now=later,
    )
    assert RejectionReason.NOT_TRADABLE in _reasons(rig)
    assert AgentFailureKind.NON_TRADABLE_INSTRUMENT in {
        failure.kind for failure in rig.engine.drain_failures()
    }


def test_an_instrument_with_no_fill_model_is_unsupported_execution(
    session: Session, clock: FrozenClock
) -> None:
    rig = _rig(session, clock, capital="100000.00")
    at = session_close(3)
    rig.engine.place_orders(
        [_buy()], portfolio_id=PORTFOLIO, bundle_id=BUNDLE, index=_index(at=at), decided_at=at
    )
    rig.engine.drain_failures()

    later = session_close(4)
    rig.engine.execute_due(
        portfolio_id=PORTFOLIO,
        index=_index(
            at=later,
            views=(_view(execution_model=ExecutionModel.UNSUPPORTED),),
            evidence=(_price(ALPHA, "99.95", "100.05", at=later),),
        ),
        now=later,
    )
    assert RejectionReason.UNSUPPORTED_EXECUTION in _reasons(rig)
    assert AgentFailureKind.UNSUPPORTED_EXECUTION in {
        failure.kind for failure in rig.engine.drain_failures()
    }


# ---------------------------------------------------------------------------
# Rejections that are not
# ---------------------------------------------------------------------------


def test_selling_what_is_not_held_is_nothing_to_sell_and_no_agent_failure(
    session: Session, clock: FrozenClock
) -> None:
    """Shorts are not modelled, so a bearish arm can only express itself by not
    buying. Counting this as an agent failure would penalise exactly that."""
    rig = _rig(session, clock, capital="100000.00")
    _place(rig, _sell())

    assert RejectionReason.NOTHING_TO_SELL in _reasons(rig)
    assert rig.engine.drain_failures() == ()


def test_an_order_too_small_to_be_worth_filling_is_below_minimum_size(
    session: Session, clock: FrozenClock
) -> None:
    """Dust, not a malfunction: at this equity the target weight buys less than
    the fee minimum makes economic, so the order is dropped rather than filled
    at a size where the fee dominates the result."""
    rig = _rig(session, clock, capital="500.00", weight="0.10")
    _place(rig, _buy())

    assert RejectionReason.BELOW_MINIMUM_SIZE in _reasons(rig)
    assert rig.engine.drain_failures() == ()


def test_a_window_with_no_price_is_no_execution_quote_and_no_agent_failure(
    session: Session, clock: FrozenClock
) -> None:
    """An empty market is not the agent's doing."""
    rig = _rig(session, clock, capital="100000.00")
    at = session_close(3)
    rig.engine.place_orders(
        [_buy()], portfolio_id=PORTFOLIO, bundle_id=BUNDLE, index=_index(at=at), decided_at=at
    )
    rig.engine.drain_failures()

    later = session_close(4)
    rig.engine.execute_due(
        portfolio_id=PORTFOLIO,
        index=_index(
            at=later, views=(_view(),), evidence=(_price("id-other", "1", "2", at=later),)
        ),
        now=later,
    )
    assert RejectionReason.NO_EXECUTION_QUOTE in _reasons(rig)
    assert rig.engine.drain_failures() == ()


def test_a_market_with_no_volume_is_liquidity_exhausted_and_no_agent_failure(
    session: Session, clock: FrozenClock
) -> None:
    rig = _rig(session, clock, capital="100000.00")
    at = session_close(3)
    rig.engine.place_orders(
        [_buy()], portfolio_id=PORTFOLIO, bundle_id=BUNDLE, index=_index(at=at), decided_at=at
    )
    rig.engine.drain_failures()

    later = session_close(4)
    rig.engine.execute_due(
        portfolio_id=PORTFOLIO,
        index=_index(
            at=later,
            views=(_view(),),
            evidence=(_price(ALPHA, "99.95", "100.05", at=later, volume=0),),
        ),
        now=later,
    )
    assert RejectionReason.LIQUIDITY_EXHAUSTED in _reasons(rig)
    assert rig.engine.drain_failures() == ()


# ---------------------------------------------------------------------------
# The classification itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(RejectionReason), ids=str)
def test_every_rejection_reason_has_a_settled_classification(
    reason: RejectionReason,
) -> None:
    """No third state. A reason that was neither an agent failure nor
    explicitly not one would be counted differently depending on which code
    path produced it."""
    from marketlab.execution.engine import _AGENT_FAILING_REJECTIONS

    agent_attributable = {
        RejectionReason.NOT_TRADABLE,
        RejectionReason.UNSUPPORTED_EXECUTION,
        RejectionReason.INSUFFICIENT_CASH,
    }
    assert (reason in _AGENT_FAILING_REJECTIONS) is (reason in agent_attributable)
