"""Applying corporate actions to books and to reference data (§17.5).

Ingestion records a corporate action as a raw fact; nothing before this module
acts on it. That separation is deliberate — the snapshot must show what was
*known* at the cutoff regardless of what has been *applied* — but leaving it
there permanently would be the silent-corruption failure §17.5 exists to
prevent: a portfolio that quietly keeps 100 shares through a 2-for-1, or never
receives a dividend it was entitled to, produces returns that look like
performance and are arithmetic.

Two audiences, deliberately separate methods
--------------------------------------------
:meth:`CorporateActionApplier.apply_to_reference_data` touches the shared
instrument repository — a ticker change is a fact about the world, identical
for every condition, and applying it once per arm would be six writes of the
same fact.

:meth:`CorporateActionApplier.apply_to_portfolio` touches one book. Dividends
and splits depend on what that condition happens to hold, so they are applied
per portfolio, and one arm's entitlement is computed from its own position and
nothing else.

Entitlement is dated, payment is simplified
-------------------------------------------
A dividend's entitlement is determined by the position held on the ex-date,
which is exactly what this computes. The cash, however, is credited on the
ex-date rather than on the later pay date the fixture carries. That is a
stated FID-level simplification (``docs/ROADMAP.md``), not an oversight: the
scientifically load-bearing part — *who* is entitled and *how much* — is
correct, while the few sessions of float between ex and pay would change only
the interest on an amount this study does not model interest on.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.accounting import accounts
from marketlab.accounting.ledger import Ledger, Posting
from marketlab.accounting.positions import PositionBook
from marketlab.core.clock import Clock
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.money import Money
from marketlab.instruments.repository import InstrumentRepository
from marketlab.retrieval.types import Evidence, EvidenceKind, RetrievalIndex
from marketlab.storage.base import Base, HashStr, InstantStr, ShortStr
from marketlab.storage.events import EventStore

__all__ = [
    "AppliedAction",
    "CorporateActionApplicationRow",
    "CorporateActionApplier",
]

CASH_DIVIDEND = "CASH_DIVIDEND"
STOCK_SPLIT = "STOCK_SPLIT"
TICKER_CHANGE = "TICKER_CHANGE"


class CorporateActionApplicationRow(Base):
    """Record that one action was applied to one book.

    Existence *is* the idempotence guard: a resumed cycle re-reads the same
    snapshot and must not pay the same dividend twice.
    """

    __tablename__ = "corporate_action_applications"

    application_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    scope_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    """The portfolio it was applied to, or ``REFERENCE`` for shared data."""

    evidence_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    instrument_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(ShortStr, nullable=False)
    applied_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    detail: Mapped[str] = mapped_column(ShortStr, nullable=False, default="")


REFERENCE_SCOPE = "REFERENCE"


@dataclass(frozen=True, slots=True)
class AppliedAction:
    """One corporate action that actually changed something."""

    evidence_id: str
    instrument_id: str
    action_type: str
    detail: str


@dataclass(slots=True)
class CorporateActionApplier:
    """Applies the corporate actions effective in one snapshot."""

    session: Session
    clock: Clock
    events: EventStore
    ledger: Ledger
    positions: PositionBook
    instruments: InstrumentRepository

    # -- shared reference data -----------------------------------------------

    def apply_to_reference_data(self, index: RetrievalIndex) -> tuple[AppliedAction, ...]:
        """Apply the actions that change the instrument universe itself.

        Called once per cycle, before any arm runs — every condition must see
        the same universe, so this cannot be per-portfolio.
        """
        applied: list[AppliedAction] = []
        for evidence in _effective_actions(index):
            action_type = str(evidence.fields.get("action_type", ""))
            if action_type != TICKER_CHANGE:
                continue
            instrument_id = str(evidence.fields["instrument_id"])
            new_ticker = str(evidence.fields["details"]["new_ticker"])
            if not self._claim(REFERENCE_SCOPE, evidence, instrument_id, action_type, new_ticker):
                continue
            self.instruments.record_ticker_change(instrument_id, new_ticker, index.cutoff)
            applied.append(
                AppliedAction(evidence.evidence_id, instrument_id, action_type, new_ticker)
            )
        self._announce(REFERENCE_SCOPE, applied, index)
        return tuple(applied)

    # -- one book ------------------------------------------------------------

    def apply_to_portfolio(
        self, index: RetrievalIndex, *, portfolio_id: str
    ) -> tuple[AppliedAction, ...]:
        """Apply dividends and splits to one condition's holdings."""
        applied: list[AppliedAction] = []
        for evidence in _effective_actions(index):
            action_type = str(evidence.fields.get("action_type", ""))
            instrument_id = str(evidence.fields["instrument_id"])
            held = self.positions.quantity_of(portfolio_id, instrument_id, as_of=index.cutoff)
            if held <= 0:
                # Nothing to adjust. Deliberately not claimed either, so a
                # position opened later is still adjusted by a later action.
                continue

            if action_type == CASH_DIVIDEND:
                outcome = self._pay_dividend(evidence, portfolio_id, instrument_id, held, index)
            elif action_type == STOCK_SPLIT:
                outcome = self._apply_split(evidence, portfolio_id, instrument_id, index)
            else:
                continue
            if outcome is not None:
                applied.append(outcome)
        self._announce(portfolio_id, applied, index)
        return tuple(applied)

    def _pay_dividend(
        self,
        evidence: Evidence,
        portfolio_id: str,
        instrument_id: str,
        held: Decimal,
        index: RetrievalIndex,
    ) -> AppliedAction | None:
        details = evidence.fields["details"]
        per_share = Decimal(str(details["amount_per_share"]))
        currency = str(details["currency"])
        amount = Money(per_share * held, currency).quantized()
        if amount.is_zero():
            return None
        if not self._claim(portfolio_id, evidence, instrument_id, CASH_DIVIDEND, amount.to_str()):
            return None

        self.ledger.post(
            portfolio_id=portfolio_id,
            transaction_type="CASH_DIVIDEND",
            occurred_at=index.cutoff,
            postings=[
                Posting(accounts.cash(currency), amount, "dividend received"),
                Posting(accounts.dividend_income(currency), -amount, "dividend income"),
            ],
            reference={"evidence_id": evidence.evidence_id, "instrument_id": instrument_id},
        )
        return AppliedAction(
            evidence.evidence_id, instrument_id, CASH_DIVIDEND, f"{amount} on {held} units"
        )

    def _apply_split(
        self, evidence: Evidence, portfolio_id: str, instrument_id: str, index: RetrievalIndex
    ) -> AppliedAction | None:
        ratio = Decimal(str(evidence.fields["details"]["split_ratio"]))
        if not self._claim(portfolio_id, evidence, instrument_id, STOCK_SPLIT, str(ratio)):
            return None
        self.positions.apply_split(
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            ratio=ratio,
            occurred_at=index.cutoff,
            reference_id=evidence.evidence_id,
        )
        # No ledger posting: a split re-denominates a holding, it does not
        # change what the holding is worth.
        return AppliedAction(evidence.evidence_id, instrument_id, STOCK_SPLIT, f"ratio {ratio}")

    # -- bookkeeping ---------------------------------------------------------

    def _claim(
        self,
        scope_id: str,
        evidence: Evidence,
        instrument_id: str,
        action_type: str,
        detail: str,
    ) -> bool:
        """Reserve this action for this scope. ``False`` if already applied."""
        application_id = derive_id(
            IdKind.CORPORATE_ACTION, scope_id=scope_id, evidence_id=evidence.evidence_id
        )
        if self.session.get(CorporateActionApplicationRow, application_id) is not None:
            return False
        self.session.add(
            CorporateActionApplicationRow(
                application_id=application_id,
                scope_id=scope_id,
                evidence_id=evidence.evidence_id,
                instrument_id=instrument_id,
                action_type=action_type,
                applied_at=str(self.clock.now_instant()),
                detail=detail,
            )
        )
        self.session.flush()
        return True

    def _announce(self, scope_id: str, applied: list[AppliedAction], index: RetrievalIndex) -> None:
        if not applied:
            return
        self.events.append(
            "CORPORATE_ACTIONS_APPLIED",
            {
                "scope_id": scope_id,
                "snapshot_id": index.snapshot_id,
                "actions": [
                    {
                        "evidence_id": action.evidence_id,
                        "instrument_id": action.instrument_id,
                        "action_type": action.action_type,
                        "detail": action.detail,
                    }
                    for action in applied
                ],
            },
            occurred_at=index.cutoff,
        )


def _effective_actions(index: RetrievalIndex) -> tuple[Evidence, ...]:
    """Actions taking effect in this session, in a defined order.

    Filtered on ``as_of == cutoff`` because a snapshot is cumulative: it still
    carries every action of every earlier session, and re-applying those would
    pay a dividend once per subsequent cycle. Sorted by ``evidence_id`` so two
    actions on one session are applied in the same order on every replay.
    """
    return tuple(
        sorted(
            (
                evidence
                for evidence in index.evidence_of_kind(EvidenceKind.CORPORATE_ACTION)
                if evidence.as_of == index.cutoff
            ),
            key=lambda evidence: evidence.evidence_id,
        )
    )
