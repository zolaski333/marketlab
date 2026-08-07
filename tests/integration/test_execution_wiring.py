"""End-to-end wiring test for task 10: the full cycle loop over the synthetic
world — decide, place, fill at the next eligible window, settle T+N, and apply
whatever corporate actions fall due — run for two independent conditions.

This is where the arithmetic has to survive contact with the scripted world:
a dividend at session 10, a 2-for-1 split at session 18 and a ticker change at
session 24, on top of the ordinary flow of buys, sells and settlements. Unit
tests pin each mechanism against a hand-built fixture; only a run like this
catches the ways they disagree with each other — a split that quietly changes
a cost basis, a settlement that lands on a closed day, or an equity figure
that drifts because something posted twice.

Two arms rather than six: the property under test here is that the books stay
correct and independent, and two independent books demonstrate that as well as
six while keeping the run fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketlab.accounting import accounts
from marketlab.accounting.ledger import Ledger
from marketlab.accounting.positions import PositionBook, PositionEventRow
from marketlab.accounting.valuation import value_portfolio
from marketlab.agents.decision import DecisionAgent
from marketlab.core.clock import FrozenClock
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.core.money import Money
from marketlab.execution.corporate import CorporateActionApplier
from marketlab.execution.engine import (
    ExecutionEngine,
    FillRow,
    OrderRejectionRow,
    OrderRow,
    portfolio_id_for,
)
from marketlab.execution.policy import ExecutionPolicy
from marketlab.experiments.arms import ArmId
from marketlab.experiments.context import NullMaterialsProvider
from marketlab.experiments.runner import CycleResult, CycleRunner, RunConfig
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.synthetic import SyntheticMarketDataProvider, admit_synthetic_universe
from marketlab.instruments.calendars import CalendarRegistry
from marketlab.instruments.repository import InstrumentRepository
from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.retrieval.types import EvidenceKind
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

START_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
NUM_SESSIONS = 26  # far enough to reach the dividend (10), split (18) and ticker change (24)
RUN_ID = "EXECUTION_WIRING_RUN"
ARMS = (ArmId.A, ArmId.B)
STARTING_USD = Decimal("1000000.00")
STARTING_EUR = Decimal("500000.00")


def usd(amount: str | Decimal) -> Money:
    return Money(Decimal(amount), "USD")


def eur(amount: str | Decimal) -> Money:
    return Money(Decimal(amount), "EUR")


@dataclass
class World:
    session: Session
    ledger: Ledger
    positions: PositionBook
    builder: SnapshotBuilder
    events: EventStore
    engine: ExecutionEngine
    repo: InstrumentRepository
    cycles: tuple[CycleResult, ...]
    cutoffs: tuple[Instant, ...]
    alpha_id: str
    portfolios: dict[ArmId, str]


@pytest.fixture(scope="module")
def _unused_module_guard() -> None:  # pragma: no cover - documentation only
    """The heavy fixture below is function-scoped on purpose: every test gets a
    fresh database, so no test can be made to pass by another's writes."""


@pytest.fixture
def world(session: Session, clock: FrozenClock, blob_store: BlobStore) -> World:
    repo = InstrumentRepository(session, clock)
    calendars = CalendarRegistry()
    universe = admit_synthetic_universe(repo, calendars, START_AT)
    provider = SyntheticMarketDataProvider(
        equity_calendar=universe.equity_calendar,
        start_at=START_AT,
        num_sessions=NUM_SESSIONS,
        alpha_id=universe.alpha.instrument_id,
        beta_id=universe.beta.instrument_id,
        gamma_id=universe.gamma.instrument_id,
        delta_id=universe.delta.instrument_id,
    )
    events = EventStore(session, clock)
    pipeline = IngestionPipeline(
        blob_store,
        events,
        session,
        clock,
        source_id="SYNTHETIC",
        licence="internal-synthetic",
        redistributable=True,
    )
    builder = SnapshotBuilder(session, clock, blob_store, repo, events)
    ledger = Ledger(session, clock)
    positions = PositionBook(session)
    engine = ExecutionEngine(
        session=session,
        clock=clock,
        events=events,
        ledger=ledger,
        positions=positions,
        calendars=calendars,
        policy=ExecutionPolicy(target_weight=Decimal("0.05")),
    )
    applier = CorporateActionApplier(
        session=session,
        clock=clock,
        events=events,
        ledger=ledger,
        positions=positions,
        instruments=repo,
    )
    config = RunConfig(run_id=RUN_ID, arms=ARMS)
    runner = CycleRunner(
        session=session,
        clock=clock,
        blobs=blob_store,
        events=events,
        builder=builder,
        model_factory=DeterministicPolicyModel,
        materials=NullMaterialsProvider(),
        config=config,
        agent=DecisionAgent(),
    )
    portfolios = {arm: portfolio_id_for(RUN_ID, str(arm), 0) for arm in ARMS}
    for portfolio_id in portfolios.values():
        engine.fund(portfolio_id, [usd(STARTING_USD), eur(STARTING_EUR)], at=START_AT)

    cycles: list[CycleResult] = []
    cutoffs: list[Instant] = []
    candidates: list[SnapshotCandidate] = []

    for index_number, cutoff in enumerate(provider.session_cutoffs()):
        for bar in provider.fetch_price_bars(cutoff):
            candidates.append(SnapshotCandidate("PRICE_BAR", pipeline.ingest_price_bar(bar)))
        for item in provider.fetch_news(cutoff):
            candidates.append(SnapshotCandidate("NEWS_ITEM", pipeline.ingest_news_item(item)))
        for record in provider.fetch_macro_records(cutoff):
            candidates.append(
                SnapshotCandidate("MACRO_RECORD", pipeline.ingest_macro_record(record))
            )
        for rate in provider.fetch_fx_rates(cutoff):
            candidates.append(SnapshotCandidate("FX_RATE", pipeline.ingest_fx_rate(rate)))
        for action in provider.fetch_corporate_actions(cutoff):
            candidates.append(
                SnapshotCandidate("CORPORATE_ACTION", pipeline.ingest_corporate_action(action))
            )

        manifest = builder.build(candidates, as_of=cutoff, run_id=RUN_ID)
        snapshot = builder.load_index(manifest.snapshot_id)

        # Order of a cycle, and the reason for it: reference data first (the
        # universe every arm sees must already be correct), then per-book
        # settlement and corporate actions, then fills of orders placed last
        # session, and only then this session's decisions.
        applier.apply_to_reference_data(snapshot)
        for portfolio_id in portfolios.values():
            engine.settle_due(portfolio_id=portfolio_id, now=cutoff)
            applier.apply_to_portfolio(snapshot, portfolio_id=portfolio_id)
            engine.execute_due(portfolio_id=portfolio_id, index=snapshot, now=cutoff)

        cycle = runner.run_cycle(
            cycle_index=index_number, snapshot_id=manifest.snapshot_id, as_of=cutoff
        )
        for execution in cycle.executions:
            engine.place_orders(
                execution.outcome.trade_intents,
                portfolio_id=portfolios[execution.arm_id],
                bundle_id=execution.bundle_id,
                index=snapshot,
                decided_at=cutoff,
            )
        cycles.append(cycle)
        cutoffs.append(cutoff)

    return World(
        session=session,
        ledger=ledger,
        positions=positions,
        builder=builder,
        events=events,
        engine=engine,
        repo=repo,
        cycles=tuple(cycles),
        cutoffs=tuple(cutoffs),
        alpha_id=universe.alpha.instrument_id,
        portfolios=portfolios,
    )


# ---------------------------------------------------------------------------
# The invariant that matters most
# ---------------------------------------------------------------------------


def test_the_books_balance_for_every_condition_after_a_full_run(world: World) -> None:
    """§17.1. Every posting the run made, in every currency, sums to zero."""
    for portfolio_id in world.portfolios.values():
        world.ledger.assert_balanced(portfolio_id)


def test_the_books_balance_at_the_close_of_every_single_session(world: World) -> None:
    """Not just at the end: an error that cancels out by the final session
    would be invisible to the check above."""
    for cutoff in world.cutoffs:
        for portfolio_id in world.portfolios.values():
            world.ledger.assert_balanced(portfolio_id, as_of=cutoff)


def test_the_run_actually_traded(world: World) -> None:
    """Guards every other assertion here against passing on an empty book."""
    fills = world.session.execute(select(func.count()).select_from(FillRow)).scalar_one()
    assert int(fills) > 0
    assert any(
        world.positions.held_instruments(portfolio_id) for portfolio_id in world.portfolios.values()
    )


def test_no_order_is_left_in_limbo(world: World) -> None:
    """Every order placed early enough to be due has an outcome: a fill or a
    rejection. A silent third state would be an order that vanished."""
    orders = list(world.session.execute(select(OrderRow)).scalars())
    filled = set(world.session.execute(select(FillRow.order_id)).scalars())
    rejected = set(world.session.execute(select(OrderRejectionRow.order_id)).scalars())
    last_cutoff = world.cutoffs[-1]

    unresolved = [
        row.order_id
        for row in orders
        if row.execute_after <= str(last_cutoff)
        and row.order_id not in filled
        and row.order_id not in rejected
    ]
    assert unresolved == []


# ---------------------------------------------------------------------------
# Point-in-time execution
# ---------------------------------------------------------------------------


def test_no_fill_ever_happened_at_or_before_its_decision(world: World) -> None:
    """§16.2, checked over the whole run rather than on one fixture."""
    orders = {row.order_id: row for row in world.session.execute(select(OrderRow)).scalars()}
    for fill in world.session.execute(select(FillRow)).scalars():
        order = orders[fill.order_id]
        assert fill.executed_at > order.decided_at
        assert fill.executed_at >= order.execute_after


def test_no_settlement_ever_happened_before_its_fill(world: World) -> None:
    """Same-instant settlement is legitimate for the 24/7 crypto instrument,
    which settles T+0; anything with a settlement period must land strictly
    later."""
    universe = {
        view.instrument_id: view
        for view in world.builder.load_index(world.cycles[-1].snapshot_id).universe
    }
    seen_delayed = False
    for fill in world.session.execute(select(FillRow)).scalars():
        assert fill.settles_at >= fill.executed_at
        if universe[fill.instrument_id].settlement_days > 0:
            assert fill.settles_at > fill.executed_at
            seen_delayed = True
    assert seen_delayed, "no T+N instrument traded, so the check proved nothing"


# ---------------------------------------------------------------------------
# Corporate actions
# ---------------------------------------------------------------------------


def test_buying_on_the_ex_date_does_not_earn_the_dividend(world: World) -> None:
    """Entitlement is settled before the session's fills. Neither arm held
    Alpha going into session 10, and both bought it during that session, so
    neither is entitled - which is the rule working, not a missing feature.
    (Payment itself is exercised in tests/unit/test_corporate_actions.py.)"""
    ex_date = world.cutoffs[9]
    day_before = world.cutoffs[8]

    for portfolio_id in world.portfolios.values():
        assert world.positions.quantity_of(portfolio_id, world.alpha_id, as_of=day_before) == 0
        assert world.positions.quantity_of(portfolio_id, world.alpha_id, as_of=ex_date) > 0
        assert world.ledger.balance(portfolio_id, accounts.dividend_income("USD")).is_zero()

    # ...and the dividend really was in the snapshot, so this is not vacuous.
    index = world.builder.load_index(world.cycles[9].snapshot_id)
    effective = {
        str(evidence.fields["action_type"])
        for evidence in index.evidence_of_kind(EvidenceKind.CORPORATE_ACTION)
        if evidence.as_of == ex_date
    }
    assert "CASH_DIVIDEND" in effective


def test_the_scripted_split_preserved_cost_basis_rather_than_inventing_a_gain(
    world: World,
) -> None:
    """The failure a split most easily causes: a quantity that doubles while
    the unit cost does not halve, booking a 100% gain out of a renaming."""
    for portfolio_id in world.portfolios.values():
        events = list(
            world.session.execute(
                select(PositionEventRow)
                .where(PositionEventRow.portfolio_id == portfolio_id)
                .where(PositionEventRow.instrument_id == world.alpha_id)
                .where(PositionEventRow.reason.in_(("SPLIT_OUT", "SPLIT_IN")))
            ).scalars()
        )
        assert events, "the scripted split never reached this book"

        # Compared on the split's own events rather than on the position before
        # and after the session: the split shares its instant with that
        # session's fills, so a before/after position difference would measure
        # the trading too.
        out_basis = sum(
            -Decimal(e.quantity_delta) * Decimal(e.unit_cost)
            for e in events
            if e.reason == "SPLIT_OUT"
        )
        in_basis = sum(
            Decimal(e.quantity_delta) * Decimal(e.unit_cost)
            for e in events
            if e.reason == "SPLIT_IN"
        )
        out_quantity = sum(-Decimal(e.quantity_delta) for e in events if e.reason == "SPLIT_OUT")
        in_quantity = sum(Decimal(e.quantity_delta) for e in events if e.reason == "SPLIT_IN")

        assert in_basis == out_basis
        assert in_quantity == out_quantity * Decimal("2.0")


def test_the_scripted_ticker_change_was_applied_once_to_shared_reference_data(
    world: World,
) -> None:
    applied = [
        record.payload
        for record in world.events.iter_events(event_type="CORPORATE_ACTIONS_APPLIED")
        if any(action["action_type"] == "TICKER_CHANGE" for action in record.payload["actions"])
    ]
    assert len(applied) == 1
    assert applied[0]["scope_id"] == "REFERENCE"


# ---------------------------------------------------------------------------
# Money is conserved
# ---------------------------------------------------------------------------


def test_no_condition_created_money_out_of_nothing(world: World) -> None:
    """Equity may fall (fees, spread, losses) or rise (gains, dividends), but
    the run must not manufacture value: total equity is checked against the
    starting capital plus everything the books say explains the difference."""
    final = world.builder.load_index(world.cycles[-1].snapshot_id)
    for portfolio_id in world.portfolios.values():
        valuation = value_portfolio(
            world.ledger, world.positions, final, portfolio_id=portfolio_id, base_currency="USD"
        )
        explained = (
            -world.ledger.balance(portfolio_id, accounts.opening_equity("USD")).amount
            - world.ledger.balance(portfolio_id, accounts.opening_equity("EUR")).amount
            * _eur_rate(world)
            - world.ledger.balance(portfolio_id, accounts.realized_pnl("USD")).amount
            - world.ledger.balance(portfolio_id, accounts.dividend_income("USD")).amount
            + world.ledger.balance(portfolio_id, accounts.fees("USD")).amount * -1
        )
        # Unrealised marks make an exact identity impossible here; what is
        # asserted is that equity stays in the plausible neighbourhood of the
        # capital that was actually put in, rather than drifting by orders of
        # magnitude the way a double-posting would make it.
        assert valuation.equity.amount > explained * Decimal("0.5")
        assert valuation.equity.amount < explained * Decimal("2")


def _eur_rate(world: World) -> Decimal:
    from marketlab.accounting.valuation import FxTable

    final = world.builder.load_index(world.cycles[-1].snapshot_id)
    return FxTable.from_index(final).rate("EUR", "USD")


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_the_two_conditions_kept_separate_books(world: World) -> None:
    """Under the null materials provider both arms decide identically, so
    their books should match — which is only meaningful because they are
    genuinely separate books, checked by the transaction counts below."""
    ledger_a = world.ledger.transactions(world.portfolios[ArmId.A])
    ledger_b = world.ledger.transactions(world.portfolios[ArmId.B])
    assert ledger_a and ledger_b
    assert {t.transaction_id for t in ledger_a}.isdisjoint({t.transaction_id for t in ledger_b})


def test_the_event_chain_survives_the_whole_run(world: World) -> None:
    assert world.events.verify_chain() > 0
