"""Deterministic forecast resolution in total return (§20).

A forecast is worthless until something decides whether it came true, and
*how* that is decided is a scientific choice, not a detail. This module makes
every part of it explicit.

Total return, not price
-----------------------
"Did it close higher?" cannot be answered from two raw closes. The synthetic
world halves ``EQ_US_ALPHA``'s quote on its split session — deliberately, per
§17.5's warning against treating an adjusted series as a substitute for
corporate-action accounting — so a naive comparison would score a 2-for-1
split as a 50% loss for every arm that forecast that instrument. Resolution
therefore reconstructs what a holder of one unit at the anchor actually ended
up with:

    total_return = (split_factor * target_close + dividends) / anchor_close - 1

``split_factor`` is the product of the ratios of every split in the half-open
interval ``(anchor, target]``; ``dividends`` sums each cash dividend in that
interval multiplied by the units held when it went ex. The interval is
half-open at the anchor for the same reason the ledger's entitlement rule is:
a holder who buys at the ex-date close has already missed the dividend
(``tests/integration/test_execution_wiring.py`` pins that behaviour), so
counting it here would credit the forecast with cash the book never saw.

The horizon grid is the run's own cadence
-----------------------------------------
"In N sessions" is resolved against :class:`SessionGrid` — the ordered
instants at which the run actually decided, read back from its snapshots.
This is an interpretation, recorded in ``docs/ROADMAP.md``: the alternative is
each instrument's own trading calendar, which is more faithful for a universe
spanning several calendars but resolves to instants at which no snapshot
exists and therefore no price was ever frozen. The grid has the properties the
study needs — identical for every arm, deterministic, reconstructible from
persisted artefacts alone, and immune to a missing bar silently shifting a
5-session horizon into a 6-session one.

Five outcomes, and no sixth
---------------------------
:class:`ResolutionStatus` is exhaustive by design and none of its members is a
fabricated value. In particular there is no "assume flat" and no default
probability: a missing price makes a forecast ``UNRESOLVABLE`` and it is
excluded from the analysis, loudly and countably, rather than being scored
against an invented number.

``PENDING`` is the one status never written down. It is the *absence* of a
resolution, not a resolution — persisting it would mean either updating that
row later (which the append-only triggers refuse) or leaving a stale row
claiming a resolved forecast is still open.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import Boolean, Float, Integer, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.core.clock import Clock
from marketlab.core.failures import ConfigurationError
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant
from marketlab.core.money import decimal_to_str
from marketlab.instruments.types import InstrumentStatus
from marketlab.retrieval.types import (
    EvidenceKind,
    RetrievalIndex,
    price_quote_from_evidence,
)
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotRow
from marketlab.storage.base import Base, DecimalStr, HashStr, InstantStr, ShortStr
from marketlab.storage.events import EventStore

__all__ = [
    "CASH_DIVIDEND",
    "STOCK_SPLIT",
    "CorporateEvent",
    "ForecastResolutionRow",
    "ForecastSource",
    "MarketRecord",
    "MarketRecordBuilder",
    "PendingForecast",
    "Resolution",
    "ResolutionReport",
    "ResolutionService",
    "ResolutionStatus",
    "ResolvedOutcome",
    "SessionGrid",
    "forecast_id_for",
    "resolve_forecast",
]

CASH_DIVIDEND: Final = "CASH_DIVIDEND"
STOCK_SPLIT: Final = "STOCK_SPLIT"

_CENSORING_STATUSES: Final = frozenset({InstrumentStatus.DELISTED, InstrumentStatus.EXPIRED})
"""Instrument states that end the series rather than interrupt it.

``SUSPENDED`` is deliberately absent: a halt is a gap, and if a price does
exist at the target the forecast resolves normally against it.
"""


class ResolutionStatus(StrEnum):
    """What became of one forecast (§20.3)."""

    RESOLVED = "RESOLVED"
    """The horizon elapsed and both anchor and target prices were available."""

    PENDING = "PENDING"
    """The horizon has not elapsed yet. Never persisted — see the module docstring."""

    UNRESOLVABLE = "UNRESOLVABLE"
    """The horizon elapsed but a required price is missing from the record."""

    CENSORED_BY_DELISTING = "CENSORED_BY_DELISTING"
    """The instrument stopped existing before the horizon elapsed."""

    INVALID_SOURCE_DATA = "INVALID_SOURCE_DATA"
    """A required number was present but unusable (non-positive price or ratio)."""


class ForecastSource(StrEnum):
    """Which elicitation a forecast came from.

    The distinction is load-bearing for the analysis: only ``PANEL`` forecasts
    answer questions every condition was asked, so only they can be paired.
    ``DECISION`` forecasts are the arm's own choice of subject and are
    resolved for completeness, not for comparison.
    """

    PANEL = "PANEL"
    DECISION = "DECISION"


def forecast_id_for(source: ForecastSource, **parts: object) -> str:
    """Stable id for one forecast, namespaced by where it came from."""
    return derive_id(IdKind.FORECAST, source=str(source), **parts)


# ---------------------------------------------------------------------------
# The frozen record resolution reads from
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionGrid:
    """The instants at which one run decided, in order.

    Reconstructed from the run's snapshots rather than recomputed from a
    calendar, so a replay years later resolves against the cadence the run
    actually had, not the one today's configuration would produce.
    """

    instants: tuple[Instant, ...]

    def position_of(self, at: Instant) -> int | None:
        for position, instant in enumerate(self.instants):
            if instant == at:
                return position
        return None

    def advance(self, at: Instant, sessions: int) -> Instant | None:
        """The instant ``sessions`` grid points after ``at``.

        ``None`` means the grid does not reach that far *yet* — the horizon has
        not elapsed. Distinguishing this from "the price is missing" is what
        keeps ``PENDING`` and ``UNRESOLVABLE`` from being confused.

        Raises:
            ConfigurationError: if ``at`` is not on the grid at all. That is a
                wiring mistake (a forecast anchored outside the run's cadence),
                not a data gap, and silently treating it as one would hide it.
        """
        position = self.position_of(at)
        if position is None:
            raise ConfigurationError(
                f"{at} is not one of this run's {len(self.instants)} decision instants; "
                "a forecast anchored off the grid cannot be given a horizon.",
                at=str(at),
            )
        target = position + sessions
        return self.instants[target] if target < len(self.instants) else None


@dataclass(frozen=True, slots=True)
class CorporateEvent:
    """One split or cash dividend, at the instant it took effect."""

    at: Instant
    instrument_id: str
    kind: str
    value: Decimal
    """Split ratio, or cash amount per share."""


@dataclass(frozen=True, slots=True)
class MarketRecord:
    """Everything resolution is allowed to look at, frozen.

    Assembled from snapshots only, so it inherits their point-in-time
    guarantee: nothing in here was visible later than the cutoff it is filed
    under. Holding it as a plain value also means
    :func:`resolve_forecast` needs no database at all and can be tested
    against a hand-built world.
    """

    grid: SessionGrid
    closes: Mapping[tuple[str, Instant], Decimal]
    statuses: Mapping[tuple[str, Instant], InstrumentStatus]
    corporate_events: tuple[CorporateEvent, ...]

    def close(self, instrument_id: str, at: Instant) -> Decimal | None:
        return self.closes.get((instrument_id, at))

    def status(self, instrument_id: str, at: Instant) -> InstrumentStatus | None:
        return self.statuses.get((instrument_id, at))

    def events_between(
        self, instrument_id: str, *, after: Instant, through: Instant
    ) -> tuple[CorporateEvent, ...]:
        """Corporate events in the half-open interval ``(after, through]``."""
        return tuple(
            sorted(
                (
                    event
                    for event in self.corporate_events
                    if event.instrument_id == instrument_id and after < event.at <= through
                ),
                key=lambda event: (event.at, event.kind),
            )
        )


class MarketRecordBuilder:
    """Assembles a :class:`MarketRecord` from one run's frozen snapshots."""

    __slots__ = ("_builder", "_session")

    def __init__(self, session: Session, builder: SnapshotBuilder) -> None:
        self._session = session
        self._builder = builder

    def build(self, run_id: str, *, through: Instant) -> MarketRecord:
        """Everything the run had frozen at or before ``through``."""
        rows = list(
            self._session.execute(
                select(SnapshotRow.snapshot_id, SnapshotRow.as_of)
                .where(SnapshotRow.run_id == run_id)
                .where(SnapshotRow.as_of <= str(through))
                .order_by(SnapshotRow.as_of.asc())
            )
        )
        closes: dict[tuple[str, Instant], Decimal] = {}
        statuses: dict[tuple[str, Instant], InstrumentStatus] = {}
        events: list[CorporateEvent] = []
        instants: list[Instant] = []

        for snapshot_id, as_of_text in rows:
            cutoff = Instant(str(as_of_text))
            instants.append(cutoff)
            index = self._builder.load_index(str(snapshot_id))
            _collect(index, cutoff, closes, statuses, events)

        return MarketRecord(
            grid=SessionGrid(tuple(instants)),
            closes=closes,
            statuses=statuses,
            corporate_events=tuple(events),
        )


def _collect(
    index: RetrievalIndex,
    cutoff: Instant,
    closes: dict[tuple[str, Instant], Decimal],
    statuses: dict[tuple[str, Instant], InstrumentStatus],
    events: list[CorporateEvent],
) -> None:
    """Fold one snapshot into the record under construction.

    Only facts dated *at* this cutoff are taken. A snapshot is cumulative —
    it carries every earlier session's evidence too — and re-reading those
    would file an old price under a new instant, which is precisely the
    look-ahead the snapshot machinery exists to prevent.
    """
    for view in index.universe:
        statuses[(view.instrument_id, cutoff)] = view.status

    for evidence in index.evidence_of_kind(EvidenceKind.PRICE_BAR):
        if evidence.as_of != cutoff:
            continue
        quote = price_quote_from_evidence(evidence)
        for instrument_id in evidence.subject_ids:
            closes[(instrument_id, cutoff)] = quote.close

    for evidence in index.evidence_of_kind(EvidenceKind.CORPORATE_ACTION):
        if evidence.as_of != cutoff:
            continue
        action_type = str(evidence.fields.get("action_type", ""))
        details = evidence.fields.get("details", {})
        instrument_id = str(evidence.fields["instrument_id"])
        if action_type == STOCK_SPLIT:
            events.append(
                CorporateEvent(
                    cutoff, instrument_id, STOCK_SPLIT, Decimal(str(details["split_ratio"]))
                )
            )
        elif action_type == CASH_DIVIDEND:
            events.append(
                CorporateEvent(
                    cutoff,
                    instrument_id,
                    CASH_DIVIDEND,
                    Decimal(str(details["amount_per_share"])),
                )
            )


# ---------------------------------------------------------------------------
# Resolving one forecast
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedOutcome:
    """The arithmetic behind a ``RESOLVED`` verdict, kept for audit.

    Every input to the verdict is here, so "why was this scored as a rise?"
    is answerable from the stored row without re-deriving anything.
    """

    anchor_close: Decimal
    target_close: Decimal
    split_factor: Decimal
    dividends: Decimal
    total_return: Decimal

    @property
    def outcome_up(self) -> bool:
        """A rise is strictly positive.

        A flat close is scored as "not up". It is a real outcome, rare with
        four-decimal prices, and the convention has to be pre-registered
        rather than settled once someone notices a tie in the data.
        """
        return self.total_return > 0


@dataclass(frozen=True, slots=True)
class Resolution:
    """What resolution concluded about one forecast."""

    status: ResolutionStatus
    instrument_id: str
    horizon_sessions: int
    anchor_at: Instant
    target_at: Instant | None
    outcome: ResolvedOutcome | None = None
    detail: str = ""

    @property
    def is_terminal(self) -> bool:
        """Whether this verdict can never change, and so may be written down."""
        return self.status is not ResolutionStatus.PENDING


def resolve_forecast(
    record: MarketRecord, *, instrument_id: str, anchor_at: Instant, horizon_sessions: int
) -> Resolution:
    """Decide what became of one forecast. Pure, and total over its inputs.

    Raises:
        ConfigurationError: if ``horizon_sessions`` is not positive, or if the
            anchor is not on the run's grid. Both are wiring errors; neither is
            an outcome the study should be able to record.
    """
    if horizon_sessions < 1:
        raise ConfigurationError(
            f"horizon_sessions must be >= 1, got {horizon_sessions}: a forecast about "
            "the instant it was made is not a forecast.",
            horizon_sessions=horizon_sessions,
        )

    target_at = record.grid.advance(anchor_at, horizon_sessions)
    if target_at is None:
        return Resolution(
            status=ResolutionStatus.PENDING,
            instrument_id=instrument_id,
            horizon_sessions=horizon_sessions,
            anchor_at=anchor_at,
            target_at=None,
            detail=f"the grid has not reached {horizon_sessions} sessions past {anchor_at}",
        )

    def verdict(status: ResolutionStatus, detail: str) -> Resolution:
        return Resolution(
            status=status,
            instrument_id=instrument_id,
            horizon_sessions=horizon_sessions,
            anchor_at=anchor_at,
            target_at=target_at,
            detail=detail,
        )

    status_at_target = record.status(instrument_id, target_at)
    if status_at_target in _CENSORING_STATUSES:
        return verdict(
            ResolutionStatus.CENSORED_BY_DELISTING,
            f"{instrument_id} was {status_at_target} at {target_at}",
        )

    anchor_close = record.close(instrument_id, anchor_at)
    target_close = record.close(instrument_id, target_at)
    if anchor_close is None or target_close is None:
        missing = "anchor" if anchor_close is None else "target"
        return verdict(
            ResolutionStatus.UNRESOLVABLE,
            f"no {missing} close recorded for {instrument_id}",
        )
    if anchor_close <= 0 or target_close <= 0:
        return verdict(
            ResolutionStatus.INVALID_SOURCE_DATA,
            f"non-positive close ({anchor_close} -> {target_close}) for {instrument_id}",
        )

    split_factor = Decimal(1)
    dividends = Decimal(0)
    for event in record.events_between(instrument_id, after=anchor_at, through=target_at):
        if event.kind == STOCK_SPLIT:
            if event.value <= 0:
                return verdict(
                    ResolutionStatus.INVALID_SOURCE_DATA,
                    f"non-positive split ratio {event.value} at {event.at}",
                )
            split_factor *= event.value
        else:
            # Paid on the units held when it went ex, which is after every
            # split up to that point but before any later one.
            dividends += event.value * split_factor

    outcome = ResolvedOutcome(
        anchor_close=anchor_close,
        target_close=target_close,
        split_factor=split_factor,
        dividends=dividends,
        total_return=(split_factor * target_close + dividends) / anchor_close - 1,
    )
    return Resolution(
        status=ResolutionStatus.RESOLVED,
        instrument_id=instrument_id,
        horizon_sessions=horizon_sessions,
        anchor_at=anchor_at,
        target_at=target_at,
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class ForecastResolutionRow(Base):
    """One terminal verdict. Append-only, and written exactly once.

    Deliberately carries no score. A Brier score is a property of a *scoring
    rule*, which is an analysis choice; the total return and the realised
    direction are properties of the world. Mixing them would make changing the
    scoring rule look like changing the data.
    """

    __tablename__ = "forecast_resolutions"

    resolution_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    forecast_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    source: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    source_bundle_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    arm_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)

    instrument_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    horizon_sessions: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    probability_up: Mapped[float] = mapped_column(Float, nullable=False)

    anchor_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    target_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    status: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    outcome_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    anchor_close: Mapped[str] = mapped_column(DecimalStr, nullable=False, default="")
    target_close: Mapped[str] = mapped_column(DecimalStr, nullable=False, default="")
    split_factor: Mapped[str] = mapped_column(DecimalStr, nullable=False, default="")
    dividends: Mapped[str] = mapped_column(DecimalStr, nullable=False, default="")
    total_return: Mapped[str] = mapped_column(DecimalStr, nullable=False, default="")

    detail: Mapped[str] = mapped_column(ShortStr, nullable=False, default="")
    resolved_at: Mapped[str] = mapped_column(InstantStr, nullable=False)


@dataclass(frozen=True, slots=True)
class PendingForecast:
    """One elicited probability, waiting to be told what happened.

    The routing keys are on it because resolution runs *after* every model
    call in the run is over — there is nothing left to leak a condition to.
    """

    forecast_id: str
    source: ForecastSource
    source_bundle_id: str
    arm_id: str
    repetition: int
    instrument_id: str
    horizon_sessions: int
    probability_up: float
    anchor_at: Instant


@dataclass(frozen=True, slots=True)
class ResolutionReport:
    """What one resolution pass concluded."""

    run_id: str
    resolved: tuple[tuple[PendingForecast, Resolution], ...]
    """Terminal verdicts reached in this pass, newly written or already present."""

    pending: tuple[PendingForecast, ...]

    def counts(self) -> dict[ResolutionStatus, int]:
        tally = dict.fromkeys(ResolutionStatus, 0)
        for _, resolution in self.resolved:
            tally[resolution.status] += 1
        tally[ResolutionStatus.PENDING] = len(self.pending)
        return tally


class ResolutionService:
    """Resolves a run's forecasts against its own frozen record."""

    __slots__ = ("_clock", "_events", "_session")

    def __init__(self, session: Session, clock: Clock, events: EventStore) -> None:
        self._session = session
        self._clock = clock
        self._events = events

    def resolve(
        self, forecasts: Sequence[PendingForecast], record: MarketRecord, *, run_id: str
    ) -> ResolutionReport:
        """Resolve every forecast that can be, and record the terminal ones."""
        resolved: list[tuple[PendingForecast, Resolution]] = []
        pending: list[PendingForecast] = []

        for forecast in forecasts:
            resolution = resolve_forecast(
                record,
                instrument_id=forecast.instrument_id,
                anchor_at=forecast.anchor_at,
                horizon_sessions=forecast.horizon_sessions,
            )
            if not resolution.is_terminal:
                pending.append(forecast)
                continue
            self._persist(forecast, resolution, run_id=run_id)
            resolved.append((forecast, resolution))

        report = ResolutionReport(run_id=run_id, resolved=tuple(resolved), pending=tuple(pending))
        if resolved:
            self._events.append(
                "FORECASTS_RESOLVED",
                {
                    "resolved": len(resolved),
                    "pending": len(pending),
                    "by_status": {
                        str(status): count
                        for status, count in report.counts().items()
                        if count and status is not ResolutionStatus.PENDING
                    },
                },
                occurred_at=max(r.target_at or r.anchor_at for _, r in resolved),
                run_id=run_id,
            )
        return report

    def resolutions_for(self, run_id: str) -> tuple[ForecastResolutionRow, ...]:
        """Every terminal verdict recorded for one run, in a stable order."""
        rows = self._session.execute(
            select(ForecastResolutionRow)
            .where(ForecastResolutionRow.run_id == run_id)
            .order_by(
                ForecastResolutionRow.anchor_at.asc(),
                ForecastResolutionRow.instrument_id.asc(),
                ForecastResolutionRow.horizon_sessions.asc(),
                ForecastResolutionRow.arm_id.asc(),
                ForecastResolutionRow.repetition.asc(),
            )
        ).scalars()
        return tuple(rows)

    def _persist(self, forecast: PendingForecast, resolution: Resolution, *, run_id: str) -> None:
        resolution_id = derive_id(IdKind.FORECAST_RESOLUTION, forecast_id=forecast.forecast_id)
        if self._session.get(ForecastResolutionRow, resolution_id) is not None:
            # Already decided. Re-deciding is impossible anyway (the table is
            # append-only), so this is the resume path, not a conflict.
            return
        outcome = resolution.outcome
        assert resolution.target_at is not None  # every terminal status has one
        self._session.add(
            ForecastResolutionRow(
                resolution_id=resolution_id,
                forecast_id=forecast.forecast_id,
                run_id=run_id,
                source=str(forecast.source),
                source_bundle_id=forecast.source_bundle_id,
                arm_id=forecast.arm_id,
                repetition=forecast.repetition,
                instrument_id=forecast.instrument_id,
                horizon_sessions=forecast.horizon_sessions,
                probability_up=forecast.probability_up,
                anchor_at=str(forecast.anchor_at),
                target_at=str(resolution.target_at),
                status=str(resolution.status),
                outcome_up=outcome.outcome_up if outcome is not None else None,
                anchor_close=decimal_to_str(outcome.anchor_close) if outcome else "",
                target_close=decimal_to_str(outcome.target_close) if outcome else "",
                split_factor=decimal_to_str(outcome.split_factor) if outcome else "",
                dividends=decimal_to_str(outcome.dividends) if outcome else "",
                total_return=decimal_to_str(outcome.total_return) if outcome else "",
                detail=resolution.detail[:64],
                resolved_at=str(self._clock.now_instant()),
            )
        )
        self._session.flush()
