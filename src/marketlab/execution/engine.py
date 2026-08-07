"""Virtual execution: orders, fills, settlement (§16, §17.1).

Never at the moment of decision
-------------------------------
§16.2's rule is the one this module is built around: an order placed on the
information available at cutoff *t* may not fill at *t*. It fills at the first
instant the instrument's own calendar declares eligible **strictly after** the
decision. A backtest that fills at the decision price is measuring foresight,
not skill, and the error is invisible in the results — which is exactly why it
is enforced by the calendar rather than by care.

Sized on what was known, filled on what happened
------------------------------------------------
Quantity is fixed at decision time from the decision-time price; the fill uses
the execution window's price. The gap between them is real slippage, recorded
per fill, and is what makes §17.3's decomposition (price move / FX / trading
cost) possible rather than notional.

Every state change is a new row
-------------------------------
There is no ``order.status`` column to update — that would be the in-place
edit §P4 forbids. An order is placed once and never touched; whether it filled
is answered by whether a :class:`FillRow` or :class:`OrderRejectionRow`
references it, and whether it settled by whether a :class:`SettlementRow`
does. "Still pending" is the absence of those, not a value someone wrote.

One book per condition
----------------------
Every method takes a ``portfolio_id`` — derived per (run, arm, repetition) by
the caller. Arm B's fills cannot move arm A's cash because they are posted to
different books, which is §30.3 made structural rather than procedural.

Fidelity limit worth stating
----------------------------
The synthetic world prints one bar per session, so "the next eligible window"
resolves to *that session's bar*. There is no separate opening price to fill
against. ``execute_after`` therefore selects which session fills the order, not
which intraday price — a FID-level simplification recorded in
``docs/ROADMAP.md``, not a hidden assumption.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.accounting import accounts
from marketlab.accounting.ledger import Ledger, Posting
from marketlab.accounting.positions import PositionBook
from marketlab.accounting.valuation import FxTable, value_portfolio
from marketlab.agents.decision import TradeIntent
from marketlab.core.clock import Clock
from marketlab.core.failures import (
    AgentFailureKind,
    ConfigurationError,
    ObservedAgentFailure,
)
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant
from marketlab.core.money import Currency, Money, decimal_from_str, decimal_to_str
from marketlab.execution.policy import (
    DEFAULT_POLICY,
    ExecutionPolicy,
    OrderSide,
    fill_price,
    slippage_against_mid,
)
from marketlab.execution.types import (
    ExecutionReport,
    Fill,
    Order,
    Rejection,
    RejectionReason,
)
from marketlab.instruments.calendars import CalendarRegistry, MarketCalendar
from marketlab.instruments.repository import compute_tradability
from marketlab.instruments.types import ExecutionModel, InstrumentView, TradabilityStatus
from marketlab.models.types import TradeSide
from marketlab.retrieval.types import (
    EvidenceKind,
    PriceQuote,
    RetrievalIndex,
    price_quote_from_evidence,
)
from marketlab.storage.base import Base, DecimalStr, HashStr, InstantStr, ShortStr
from marketlab.storage.events import EventStore

__all__ = [
    "ExecutionEngine",
    "FillRow",
    "OrderRejectionRow",
    "OrderRow",
    "SettlementRow",
    "portfolio_id_for",
]

_AGENT_FAILING_REJECTIONS = {
    RejectionReason.NOT_TRADABLE: AgentFailureKind.NON_TRADABLE_INSTRUMENT,
    RejectionReason.UNSUPPORTED_EXECUTION: AgentFailureKind.UNSUPPORTED_EXECUTION,
    RejectionReason.INSUFFICIENT_CASH: AgentFailureKind.INSUFFICIENT_CASH,
}
"""Rejections that are also observations about the agent (§23.3).

``NOTHING_TO_SELL``, ``BELOW_MINIMUM_SIZE``, ``NO_EXECUTION_QUOTE`` and
``LIQUIDITY_EXHAUSTED`` are deliberately absent: an agent that says SELL on
something it does not hold has not malfunctioned — short positions are simply
not modelled — and an empty market is not the agent's doing.
"""


def portfolio_id_for(run_id: str, arm_id: str, repetition: int) -> str:
    """The book one condition trades in. Distinct per repetition (§30.3)."""
    return derive_id(IdKind.PORTFOLIO, run_id=run_id, arm_id=arm_id, repetition=repetition)


class OrderRow(Base):
    """One sized, scheduled order. Written once, never updated."""

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    bundle_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    instrument_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    side: Mapped[str] = mapped_column(ShortStr, nullable=False)
    quantity: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    currency: Mapped[str] = mapped_column(ShortStr, nullable=False)
    decided_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    execute_after: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    placed_at: Mapped[str] = mapped_column(InstantStr, nullable=False)


class FillRow(Base):
    """What an order got. Its existence is what "filled" means."""

    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("order_id", name="uq_fills_order"),)

    fill_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    order_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    portfolio_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    instrument_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    side: Mapped[str] = mapped_column(ShortStr, nullable=False)
    quantity: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    requested_quantity: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    price: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    gross: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    fee: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    slippage: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    realized_pnl: Mapped[str] = mapped_column(DecimalStr, nullable=False)
    currency: Mapped[str] = mapped_column(ShortStr, nullable=False)
    executed_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    settles_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    transaction_id: Mapped[str] = mapped_column(HashStr, nullable=False)


class OrderRejectionRow(Base):
    """Why an order did not fill. Its existence is what "rejected" means."""

    __tablename__ = "order_rejections"
    __table_args__ = (UniqueConstraint("order_id", name="uq_order_rejections_order"),)

    rejection_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    order_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    portfolio_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    instrument_id: Mapped[str] = mapped_column(ShortStr, nullable=False)
    reason: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    detail: Mapped[str] = mapped_column(ShortStr, nullable=False)
    occurred_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)


class SettlementRow(Base):
    """The cash leg of a fill, T+N later."""

    __tablename__ = "settlements"
    __table_args__ = (UniqueConstraint("fill_id", name="uq_settlements_fill"),)

    settlement_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    fill_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    portfolio_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    settled_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    transaction_id: Mapped[str] = mapped_column(HashStr, nullable=False)


@dataclass(slots=True)
class ExecutionEngine:
    """Places, fills and settles virtual orders for one study database."""

    session: Session
    clock: Clock
    events: EventStore
    ledger: Ledger
    positions: PositionBook
    calendars: CalendarRegistry
    policy: ExecutionPolicy = DEFAULT_POLICY
    base_currency: Currency = "USD"
    failures: list[ObservedAgentFailure] = field(default_factory=list)
    """Rejections that are also agent observations, drained by the caller."""

    # -- funding -------------------------------------------------------------

    def fund(self, portfolio_id: str, amounts: Sequence[Money], *, at: Instant) -> None:
        """Open a book with its virtual starting capital.

        Idempotent: funding the same portfolio with the same amounts twice
        leaves it funded once, so a resumed run does not double the capital
        one condition trades with.
        """
        postings: list[Posting] = []
        for amount in amounts:
            postings.append(Posting(accounts.cash(amount.currency), amount))
            postings.append(Posting(accounts.opening_equity(amount.currency), -amount))
        transaction = self.ledger.post(
            portfolio_id=portfolio_id,
            transaction_type="OPENING_BALANCE",
            occurred_at=at,
            postings=postings,
            reference={"reason": "initial virtual funding"},
        )
        self.events.append(
            "PORTFOLIO_FUNDED",
            {
                "portfolio_id": portfolio_id,
                "transaction_id": transaction.transaction_id,
                "amounts": [amount.to_str() + " " + amount.currency for amount in amounts],
            },
            occurred_at=at,
        )

    # -- placing -------------------------------------------------------------

    def place_orders(
        self,
        intents: Sequence[TradeIntent],
        *,
        portfolio_id: str,
        bundle_id: str,
        index: RetrievalIndex,
        decided_at: Instant,
    ) -> tuple[Order, ...]:
        """Size each intent and schedule it for its next eligible window.

        ``HOLD`` intents produce nothing: a decision not to trade is a
        decision, but it is not an order.
        """
        valuation = value_portfolio(
            self.ledger,
            self.positions,
            index,
            portfolio_id=portfolio_id,
            base_currency=self.base_currency,
        )
        fx = FxTable.from_index(index)

        placed: list[Order] = []
        for intent in intents:
            if intent.side is TradeSide.HOLD:
                continue
            side = OrderSide(str(intent.side))
            view = index.resolve_instrument(intent.instrument_id)
            if view is None:  # pragma: no cover - DecisionAgent already validated
                raise ConfigurationError(
                    f"Intent references unknown instrument {intent.instrument_id}",
                    instrument_id=intent.instrument_id,
                )

            quantity, shortfall = self._size(
                view, side, index=index, fx=fx, equity=valuation.equity, portfolio_id=portfolio_id
            )
            order = self._record_order(
                portfolio_id=portfolio_id,
                bundle_id=bundle_id,
                view=view,
                side=side,
                quantity=quantity,
                decided_at=decided_at,
            )
            placed.append(order)
            if shortfall is not None:
                self._reject(order, shortfall, "sized to zero at decision time", decided_at)
        return tuple(placed)

    def _size(
        self,
        view: InstrumentView,
        side: OrderSide,
        *,
        index: RetrievalIndex,
        fx: FxTable,
        equity: Money,
        portfolio_id: str,
    ) -> tuple[Decimal, RejectionReason | None]:
        if side is OrderSide.SELL:
            held = self.positions.quantity_of(portfolio_id, view.instrument_id, as_of=index.cutoff)
            return (held, None) if held > 0 else (Decimal(0), RejectionReason.NOTHING_TO_SELL)

        evidence = index.latest(EvidenceKind.PRICE_BAR, view.instrument_id)
        if evidence is None:
            return Decimal(0), RejectionReason.NO_EXECUTION_QUOTE
        quote = price_quote_from_evidence(evidence)

        currency = view.quote_currency
        available = self.ledger.balance(
            portfolio_id, accounts.cash(currency), as_of=index.cutoff
        ) + self.ledger.balance(portfolio_id, accounts.payable(currency), as_of=index.cutoff)
        quantity = self.policy.buy_quantity(
            equity=fx.convert(equity, currency),
            price=Money(quote.ask, currency),
            available_cash=available,
            asset_class=view.asset_class,
        )
        if quantity > 0:
            return quantity, None
        reason = (
            RejectionReason.INSUFFICIENT_CASH
            if available.amount <= 0
            else RejectionReason.BELOW_MINIMUM_SIZE
        )
        return Decimal(0), reason

    def _record_order(
        self,
        *,
        portfolio_id: str,
        bundle_id: str,
        view: InstrumentView,
        side: OrderSide,
        quantity: Decimal,
        decided_at: Instant,
    ) -> Order:
        calendar = self.calendars.get(view.calendar_code)
        execute_after = calendar.next_eligible_execution(decided_at)
        order_id = derive_id(
            IdKind.ORDER,
            portfolio_id=portfolio_id,
            bundle_id=bundle_id,
            instrument_id=view.instrument_id,
            side=str(side),
        )
        order = Order(
            order_id=order_id,
            portfolio_id=portfolio_id,
            bundle_id=bundle_id,
            instrument_id=view.instrument_id,
            side=side,
            quantity=quantity,
            currency=view.quote_currency,
            decided_at=decided_at,
            execute_after=execute_after,
        )
        if self.session.get(OrderRow, order_id) is not None:
            return order

        self.session.add(
            OrderRow(
                order_id=order_id,
                portfolio_id=portfolio_id,
                bundle_id=bundle_id,
                instrument_id=view.instrument_id,
                side=str(side),
                quantity=decimal_to_str(quantity),
                currency=view.quote_currency,
                decided_at=str(decided_at),
                execute_after=str(execute_after),
                placed_at=str(self.clock.now_instant()),
            )
        )
        self.events.append(
            "ORDER_PLACED",
            {
                "order_id": order_id,
                "instrument_id": view.instrument_id,
                "side": str(side),
                "quantity": decimal_to_str(quantity),
                "execute_after": str(execute_after),
            },
            occurred_at=decided_at,
        )
        return order

    # -- filling -------------------------------------------------------------

    def execute_due(
        self, *, portfolio_id: str, index: RetrievalIndex, now: Instant
    ) -> ExecutionReport:
        """Fill every order whose execution window has opened."""
        fills: list[Fill] = []
        rejections: list[Rejection] = []

        for row in self._due_orders(portfolio_id, now):
            order = _order_from_row(row)
            outcome = self._fill_one(order, index=index, now=now)
            if isinstance(outcome, Rejection):
                rejections.append(outcome)
            else:
                fills.append(outcome)

        return ExecutionReport(fills=tuple(fills), rejections=tuple(rejections))

    def _due_orders(self, portfolio_id: str, now: Instant) -> list[OrderRow]:
        settled_or_rejected = set(
            self.session.execute(
                select(FillRow.order_id).where(FillRow.portfolio_id == portfolio_id)
            ).scalars()
        ) | set(
            self.session.execute(
                select(OrderRejectionRow.order_id).where(
                    OrderRejectionRow.portfolio_id == portfolio_id
                )
            ).scalars()
        )
        query = (
            select(OrderRow)
            .where(OrderRow.portfolio_id == portfolio_id)
            .where(OrderRow.execute_after <= str(now))
            .order_by(OrderRow.decided_at.asc(), OrderRow.order_id.asc())
        )
        return [
            row
            for row in self.session.execute(query).scalars()
            if row.order_id not in settled_or_rejected
        ]

    def _fill_one(self, order: Order, *, index: RetrievalIndex, now: Instant) -> Fill | Rejection:
        view = index.resolve_instrument(order.instrument_id)
        evidence = index.latest(EvidenceKind.PRICE_BAR, order.instrument_id)
        if view is None or evidence is None:
            return self._reject(
                order, RejectionReason.NO_EXECUTION_QUOTE, "no price in the execution window", now
            )

        quote = price_quote_from_evidence(evidence)
        blocked = _tradability_rejection(view, quote, stale=evidence.as_of != index.cutoff)
        if blocked is not None:
            reason, detail = blocked
            return self._reject(order, reason, detail, now)

        capped = min(order.quantity, self.policy.liquidity_cap(quote.volume))
        if capped <= 0:
            return self._reject(
                order,
                RejectionReason.LIQUIDITY_EXHAUSTED,
                f"participation cap on volume {quote.volume} left nothing to fill",
                now,
            )
        return self._book_fill(order, view, quote, quantity=capped, now=now)

    def _book_fill(
        self,
        order: Order,
        view: InstrumentView,
        quote: PriceQuote,
        *,
        quantity: Decimal,
        now: Instant,
    ) -> Fill:
        currency = view.quote_currency
        price = Money(fill_price(quote, order.side), currency)
        gross = price.scaled_by(quantity).quantized()
        fee = self.policy.fee_schedule(view.asset_class).fee_for(gross)
        slippage = Money(slippage_against_mid(quote, order.side, quantity), currency).quantized()
        fill_id = derive_id(IdKind.FILL, order_id=order.order_id, executed_at=str(now))

        if order.side is OrderSide.BUY:
            postings = [
                Posting(accounts.position(view.instrument_id, currency), gross, "cost basis"),
                Posting(accounts.fees(currency), fee, "commission"),
                Posting(accounts.payable(currency), -(gross + fee), "unsettled purchase"),
            ]
            realized = Money.zero(currency)
        else:
            consumed = self.positions.close_quantity(
                portfolio_id=order.portfolio_id,
                instrument_id=order.instrument_id,
                quantity=quantity,
                occurred_at=now,
                reason="FILL",
                reference_id=fill_id,
            )
            cost = Money(
                sum((c.cost_basis.amount for c in consumed), Decimal(0)), currency
            ).quantized()
            realized = (gross - cost).quantized()
            postings = [
                Posting(accounts.receivable(currency), gross - fee, "unsettled sale"),
                Posting(accounts.fees(currency), fee, "commission"),
                Posting(accounts.position(view.instrument_id, currency), -cost, "cost basis out"),
                Posting(accounts.realized_pnl(currency), -realized, "realised on sale"),
            ]

        transaction = self.ledger.post(
            portfolio_id=order.portfolio_id,
            transaction_type=f"{order.side}_FILL",
            occurred_at=now,
            postings=postings,
            reference={"order_id": order.order_id, "fill_id": fill_id},
        )
        if order.side is OrderSide.BUY:
            self.positions.open_lot(
                portfolio_id=order.portfolio_id,
                instrument_id=order.instrument_id,
                quantity=quantity,
                unit_cost=price.amount,
                currency=currency,
                occurred_at=now,
                reason="FILL",
                reference_id=fill_id,
            )

        calendar = self.calendars.get(view.calendar_code)
        settles_at = _settlement_instant(calendar, now, view.settlement_days)

        self.session.add(
            FillRow(
                fill_id=fill_id,
                order_id=order.order_id,
                portfolio_id=order.portfolio_id,
                instrument_id=order.instrument_id,
                side=str(order.side),
                quantity=decimal_to_str(quantity),
                requested_quantity=decimal_to_str(order.quantity),
                price=price.to_str(),
                gross=gross.to_str(),
                fee=fee.to_str(),
                slippage=slippage.to_str(),
                realized_pnl=realized.to_str(),
                currency=currency,
                executed_at=str(now),
                settles_at=str(settles_at),
                transaction_id=transaction.transaction_id,
            )
        )
        self.events.append(
            "ORDER_FILLED",
            {
                "order_id": order.order_id,
                "fill_id": fill_id,
                "instrument_id": order.instrument_id,
                "side": str(order.side),
                "quantity": decimal_to_str(quantity),
                "requested_quantity": decimal_to_str(order.quantity),
                "price": price.to_str(),
                "fee": fee.to_str(),
                "slippage": slippage.to_str(),
                "realized_pnl": realized.to_str(),
                "currency": currency,
                "settles_at": str(settles_at),
                "transaction_id": transaction.transaction_id,
            },
            occurred_at=now,
        )
        return Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            instrument_id=order.instrument_id,
            side=order.side,
            quantity=quantity,
            requested_quantity=order.quantity,
            price=price,
            gross=gross,
            fee=fee,
            slippage=slippage,
            realized_pnl=realized,
            executed_at=now,
            settles_at=settles_at,
            transaction_id=transaction.transaction_id,
        )

    # -- settlement ----------------------------------------------------------

    def settle_due(self, *, portfolio_id: str, now: Instant) -> tuple[str, ...]:
        """Move cash for every fill whose settlement date has arrived (T+N)."""
        already = set(
            self.session.execute(
                select(SettlementRow.fill_id).where(SettlementRow.portfolio_id == portfolio_id)
            ).scalars()
        )
        query = (
            select(FillRow)
            .where(FillRow.portfolio_id == portfolio_id)
            .where(FillRow.settles_at <= str(now))
            .order_by(FillRow.settles_at.asc(), FillRow.fill_id.asc())
        )
        settled: list[str] = []
        for row in self.session.execute(query).scalars():
            if row.fill_id in already:
                continue
            self._settle(row, now)
            settled.append(row.fill_id)
        return tuple(settled)

    def _settle(self, row: FillRow, now: Instant) -> None:
        currency = row.currency
        gross = Money(decimal_from_str(row.gross), currency)
        fee = Money(decimal_from_str(row.fee), currency)
        settled_at = Instant(row.settles_at)

        if row.side == str(OrderSide.BUY):
            total = gross + fee
            postings = [
                Posting(accounts.payable(currency), total, "clearing purchase"),
                Posting(accounts.cash(currency), -total, "cash out"),
            ]
        else:
            net = gross - fee
            postings = [
                Posting(accounts.cash(currency), net, "cash in"),
                Posting(accounts.receivable(currency), -net, "clearing sale"),
            ]

        transaction = self.ledger.post(
            portfolio_id=row.portfolio_id,
            transaction_type=f"{row.side}_SETTLEMENT",
            occurred_at=settled_at,
            postings=postings,
            reference={"fill_id": row.fill_id},
        )
        settlement_id = derive_id(
            IdKind.LEDGER_TRANSACTION, fill_id=row.fill_id, purpose="settlement"
        )
        self.session.add(
            SettlementRow(
                settlement_id=settlement_id,
                fill_id=row.fill_id,
                portfolio_id=row.portfolio_id,
                settled_at=str(settled_at),
                transaction_id=transaction.transaction_id,
            )
        )
        self.events.append(
            "SETTLEMENT_COMPLETED",
            {
                "fill_id": row.fill_id,
                "settlement_id": settlement_id,
                "transaction_id": transaction.transaction_id,
                "side": row.side,
                "currency": currency,
            },
            occurred_at=settled_at,
        )

    # -- rejection -----------------------------------------------------------

    def _reject(self, order: Order, reason: RejectionReason, detail: str, at: Instant) -> Rejection:
        rejection_id = derive_id(IdKind.FAILURE, order_id=order.order_id, reason=str(reason))
        if self.session.get(OrderRejectionRow, rejection_id) is None:
            self.session.add(
                OrderRejectionRow(
                    rejection_id=rejection_id,
                    order_id=order.order_id,
                    portfolio_id=order.portfolio_id,
                    instrument_id=order.instrument_id,
                    reason=str(reason),
                    detail=detail,
                    occurred_at=str(at),
                )
            )
            self.events.append(
                "ORDER_REJECTED",
                {
                    "order_id": order.order_id,
                    "instrument_id": order.instrument_id,
                    "reason": str(reason),
                    "detail": detail,
                },
                occurred_at=at,
            )
        failure_kind = _AGENT_FAILING_REJECTIONS.get(reason)
        if failure_kind is not None:
            self.failures.append(
                ObservedAgentFailure(
                    kind=failure_kind,
                    detail=f"{order.instrument_id}: {detail}",
                    occurred_at=at,
                    context={"order_id": order.order_id, "reason": str(reason)},
                )
            )
        return Rejection(
            order_id=order.order_id,
            instrument_id=order.instrument_id,
            reason=reason,
            detail=detail,
            occurred_at=at,
        )

    def drain_failures(self) -> tuple[ObservedAgentFailure, ...]:
        """Take the agent-attributable rejections observed since the last call."""
        drained = tuple(self.failures)
        self.failures.clear()
        return drained


# -- helpers ------------------------------------------------------------------


def _order_from_row(row: OrderRow) -> Order:
    return Order(
        order_id=row.order_id,
        portfolio_id=row.portfolio_id,
        bundle_id=row.bundle_id,
        instrument_id=row.instrument_id,
        side=OrderSide(row.side),
        quantity=decimal_from_str(row.quantity),
        currency=row.currency,
        decided_at=Instant(row.decided_at),
        execute_after=Instant(row.execute_after),
    )


def _tradability_rejection(
    view: InstrumentView, quote: PriceQuote, *, stale: bool
) -> tuple[RejectionReason, str] | None:
    """Whether §7.5 forbids trading this instrument in this window."""
    status = compute_tradability(
        view,
        has_tradable_quote=quote.bid > 0 and quote.ask > 0,
        data_is_stale=stale,
    )
    if status is TradabilityStatus.TRADABLE:
        return None
    if (
        status is TradabilityStatus.RESEARCH_ONLY
        or view.execution_model is ExecutionModel.UNSUPPORTED
    ):
        return (
            RejectionReason.UNSUPPORTED_EXECUTION,
            f"no honest fill model for {view.execution_model}",
        )
    return RejectionReason.NOT_TRADABLE, f"tradability is {status}"


def _settlement_instant(
    calendar: MarketCalendar, executed_at: Instant, settlement_days: int
) -> Instant:
    """Advance ``settlement_days`` eligible windows from the fill.

    Calendar-driven rather than a fixed 48 hours: T+2 over a weekend is four
    calendar days, and a fixed offset would settle cash on a day the market
    was shut.
    """
    moment = executed_at
    for _ in range(max(settlement_days, 0)):
        moment = calendar.next_eligible_execution(moment)
    return moment
