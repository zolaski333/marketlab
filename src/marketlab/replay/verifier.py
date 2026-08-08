"""Replay that recomputes and compares (§12.5, §30.4).

What the previous implementation shipped
----------------------------------------
Its "exact replay" verifier re-serialised a string, checked the result was
non-empty, and returned ``EXACT_REPLAY_SUCCESS`` unconditionally. It could not
fail. That is the most instructive defect in this project's history, because
it passed review, passed its own tests, and appeared in a validation report as
evidence of reproducibility.

So this module is built to be falsifiable. It recomputes every artefact the
platform derived — snapshots, orders, fills, ledger balances, positions,
forecast resolutions — into a **separate database**, then compares them field
by field against what was recorded. ``tests/replay/test_replay_verifier.py``
corrupts a recorded run in several different ways and asserts that each one is
reported; without those tests the code below would be a longer version of the
same lie.

What a replay can and cannot reproduce
--------------------------------------
It cannot re-elicit a model. A real provider is not a pure function, so
re-running the decision loop would produce a different decision and report a
divergence that is not a defect. The sealed decision is therefore an **input**
to the replay, exactly as the raw market data is: it is re-read, its
``content_hash`` is re-derived from the stored payload and compared, and
everything downstream of it is genuinely recomputed. :func:`_no_model` makes
that structural — a replay that tried to construct a model would raise rather
than quietly produce a second, different decision.

That boundary is the honest one, and it is where the value is. Every mistake
this platform can make in sizing, filling, settling, bookkeeping, applying a
corporate action, or resolving a forecast lies downstream of the model, and
all of it is recomputed here from nothing.

Inputs a replay must be handed
------------------------------
Calendars, the execution policy and the run configuration are configuration
rather than recorded facts — the platform does not persist them yet
(``docs/ROADMAP.md``). A replay is given them, and a replay given *different*
ones will report divergences, which is correct: it would genuinely be a
different study. Starting capital is not in that list; it is read back out of
the recorded opening balances, because it is an input the platform does
record.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketlab.accounting.ledger import Ledger, LedgerEntryRow, LedgerTransactionRow
from marketlab.accounting.positions import PositionBook, PositionEventRow
from marketlab.core.clock import Clock
from marketlab.core.failures import ConfigurationError, IntegrityError
from marketlab.core.instants import Instant
from marketlab.core.money import Currency, Money, decimal_from_str
from marketlab.evaluation.collection import ForecastCollector
from marketlab.evaluation.panels import PanelStore
from marketlab.evaluation.resolution import (
    ForecastResolutionRow,
    MarketRecordBuilder,
    ResolutionService,
)
from marketlab.execution.corporate import CorporateActionApplier
from marketlab.execution.engine import ExecutionEngine, FillRow, OrderRow
from marketlab.execution.policy import DEFAULT_POLICY, ExecutionPolicy
from marketlab.experiments.arms import ArmSpec
from marketlab.experiments.driver import CycleDriver
from marketlab.experiments.runner import (
    ArmExecution,
    CycleRunner,
    DecisionBundleRow,
    RunConfig,
    decision_content_hash,
)
from marketlab.ingestion.pipeline import IngestedRecord
from marketlab.instruments.calendars import CalendarRegistry
from marketlab.instruments.repository import (
    InstrumentRepository,
    InstrumentRow,
    InstrumentVersionRow,
)
from marketlab.models.types import LanguageModel
from marketlab.snapshots.builder import (
    SnapshotBuilder,
    SnapshotCandidate,
    SnapshotMemberRow,
    SnapshotRow,
)
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

__all__ = [
    "Divergence",
    "ReplayConfig",
    "ReplayReport",
    "ReplayVerifier",
]

_ADMISSION_VERSION = 1
_OPENING_BALANCE = "OPENING_BALANCE"
_CASH_ACCOUNT = "CASH"


@dataclass(frozen=True, slots=True)
class Divergence:
    """One recomputed value that does not match what was recorded."""

    kind: str
    key: str
    field_name: str
    recorded: str
    recomputed: str

    def __str__(self) -> str:
        return (
            f"{self.kind} {self.key}: {self.field_name} recorded={self.recorded!r} "
            f"recomputed={self.recomputed!r}"
        )


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """What a replay found."""

    run_id: str
    compared: Mapping[str, int]
    """Artefacts actually compared, per kind. A replay that compared nothing
    is not an exact replay, and this is what makes that visible."""

    divergences: tuple[Divergence, ...]

    @property
    def is_exact(self) -> bool:
        """True only if something was compared **and** nothing diverged."""
        return self.total_compared > 0 and not self.divergences

    @property
    def total_compared(self) -> int:
        return sum(self.compared.values())

    def divergences_of(self, kind: str) -> tuple[Divergence, ...]:
        return tuple(d for d in self.divergences if d.kind == kind)

    def summary(self) -> str:
        counts = ", ".join(f"{kind}={count}" for kind, count in sorted(self.compared.items()))
        verdict = "exact" if self.is_exact else f"{len(self.divergences)} divergence(s)"
        return f"replay of {self.run_id}: {verdict} over [{counts}]"


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """The configuration a replay must be handed, because it is not recorded."""

    run: RunConfig
    calendars: CalendarRegistry
    policy: ExecutionPolicy = DEFAULT_POLICY
    base_currency: Currency = "USD"


@dataclass(frozen=True, slots=True)
class _ReplaySide:
    """The freshly-built platform the replay recomputes into."""

    events: EventStore
    builder: SnapshotBuilder
    engine: ExecutionEngine
    driver: CycleDriver
    panels: PanelStore


@dataclass(slots=True)
class ReplayVerifier:
    """Recomputes one recorded run into a fresh database and compares."""

    recorded: Session
    replayed: Session
    blobs: BlobStore
    """Shared between the two databases on purpose: a blob is content-addressed
    raw bytes, so re-storing it would produce the identical digest. Replaying
    the bytes would test SHA-256, not the platform."""

    clock: Clock
    config: ReplayConfig
    _divergences: list[Divergence] = field(default_factory=list, init=False)
    _compared: dict[str, int] = field(default_factory=dict, init=False)

    # -- entry point ---------------------------------------------------------

    def verify(self) -> ReplayReport:
        """Recompute the whole run and compare it to what was recorded.

        Raises:
            IntegrityError: if the recorded run has no snapshots or no sealed
                decisions. A replay of nothing that reported success is the
                defect this module exists to make impossible, so an empty
                source is an error rather than a clean bill of health.
        """
        self._divergences.clear()
        self._compared.clear()

        cycles = self._recorded_cycles()
        if not cycles:
            raise IntegrityError(
                f"Run {self.config.run.run_id} has no recorded snapshots: there is "
                "nothing to replay, and reporting success would be a false claim.",
                run_id=self.config.run.run_id,
            )
        if not self._recorded_bundles():
            raise IntegrityError(
                f"Run {self.config.run.run_id} has no sealed decisions: there is "
                "nothing to replay.",
                run_id=self.config.run.run_id,
            )

        side = self._build_replay_side()
        self._copy_admissions()
        self._carry_sealed_model_output(side)
        self._replay_funding(side.engine)

        for snapshot_id, as_of in cycles:
            self._rebuild_snapshot(side.builder, snapshot_id, as_of)
            index = side.builder.load_index(snapshot_id)
            side.driver.open_cycle(index, as_of=as_of)
            executions = self._sealed_executions(snapshot_id)
            self._check_decisions(executions)
            side.driver.place(executions, index=index, as_of=as_of)

        self._replay_resolutions(side, through=cycles[-1][1])
        self.replayed.commit()

        self._compare_orders()
        self._compare_fills()
        self._compare_ledger()
        self._compare_positions()
        self._compare_resolutions()

        return ReplayReport(
            run_id=self.config.run.run_id,
            compared=dict(self._compared),
            divergences=tuple(self._divergences),
        )

    # -- building the replay side -------------------------------------------

    def _build_replay_side(self) -> _ReplaySide:
        events = EventStore(self.replayed, self.clock)
        instruments = InstrumentRepository(self.replayed, self.clock)
        builder = SnapshotBuilder(self.replayed, self.clock, self.blobs, instruments, events)
        ledger = Ledger(self.replayed, self.clock)
        positions = PositionBook(self.replayed)
        engine = ExecutionEngine(
            session=self.replayed,
            clock=self.clock,
            events=events,
            ledger=ledger,
            positions=positions,
            calendars=self.config.calendars,
            policy=self.config.policy,
            base_currency=self.config.base_currency,
        )
        applier = CorporateActionApplier(
            session=self.replayed,
            clock=self.clock,
            events=events,
            ledger=ledger,
            positions=positions,
            instruments=instruments,
        )
        runner = CycleRunner(
            session=self.replayed,
            clock=self.clock,
            blobs=self.blobs,
            events=events,
            builder=builder,
            model_factory=_no_model,
            materials=_NoMaterials(),
            config=self.config.run,
        )
        return _ReplaySide(
            events=events,
            builder=builder,
            engine=engine,
            driver=CycleDriver(runner=runner, engine=engine, applier=applier),
            panels=PanelStore(self.replayed, self.clock, self.blobs),
        )

    def _copy_admissions(self) -> None:
        """Copy the universe as admitted, and nothing after.

        Later versions — a ticker change, say — are *recomputed* by replaying
        the corporate action that caused them. Copying those too would replay
        an input that is really an output, and would hide a broken applier
        behind data the replay had been handed.
        """
        for row in self.recorded.execute(select(InstrumentRow)).scalars():
            self.replayed.merge(
                InstrumentRow(
                    instrument_id=row.instrument_id,
                    asset_class=row.asset_class,
                    admitted_at=row.admitted_at,
                )
            )
        admissions = select(InstrumentVersionRow).where(
            InstrumentVersionRow.version_number == _ADMISSION_VERSION
        )
        for version in self.recorded.execute(admissions).scalars():
            self.replayed.merge(
                InstrumentVersionRow(
                    version_id=version.version_id,
                    instrument_id=version.instrument_id,
                    version_number=version.version_number,
                    ticker=version.ticker,
                    name=version.name,
                    quote_currency=version.quote_currency,
                    native_timezone=version.native_timezone,
                    calendar_code=version.calendar_code,
                    settlement_days=version.settlement_days,
                    status=version.status,
                    execution_model=version.execution_model,
                    effective_from=version.effective_from,
                    supersedes_version_id=version.supersedes_version_id,
                    created_at=version.created_at,
                )
            )
        self.replayed.commit()

    def _carry_sealed_model_output(self, side: _ReplaySide) -> None:
        """Copy the sealed decisions and panels across.

        These are the model's output, and a replay cannot reproduce a model
        (see the module docstring). They are therefore inputs, and the replay
        database is a genuine reconstruction of the study only if it holds
        them: everything downstream — orders, books, forecast resolutions — is
        then recomputed *from* them rather than read across.

        The content of a decision is not taken on trust even so:
        :meth:`_check_decisions` re-derives its fingerprint from the payload
        and compares.
        """
        for bundle in self._recorded_bundles():
            self.replayed.merge(
                DecisionBundleRow(
                    bundle_id=bundle.bundle_id,
                    run_id=bundle.run_id,
                    cycle_id=bundle.cycle_id,
                    snapshot_id=bundle.snapshot_id,
                    arm_id=bundle.arm_id,
                    repetition=bundle.repetition,
                    position=bundle.position,
                    as_of=bundle.as_of,
                    sealed_at=bundle.sealed_at,
                    model_id=bundle.model_id,
                    content_hash=bundle.content_hash,
                    payload_blob_hash=bundle.payload_blob_hash,
                    context_blob_hash=bundle.context_blob_hash,
                    forecast_count=bundle.forecast_count,
                    trade_intent_count=bundle.trade_intent_count,
                    failure_count=bundle.failure_count,
                    tool_calls_made=bundle.tool_calls_made,
                    model_turns=bundle.model_turns,
                )
            )
        for record in PanelStore(self.recorded, self.clock, self.blobs).for_run(
            self.config.run.run_id
        ):
            side.panels.record(
                record.outcome,
                decision_bundle_id=record.decision_bundle_id,
                run_id=record.run_id,
                cycle_id=record.cycle_id,
                arm_id=record.arm_id,
                repetition=record.repetition,
                as_of=record.as_of,
                model_id=record.model_id,
                item_count=record.item_count,
            )
        self.replayed.commit()

    def _replay_funding(self, engine: ExecutionEngine) -> None:
        """Re-post the opening balances. Capital is an input, not a result."""
        rows = self.recorded.execute(
            select(LedgerEntryRow)
            .join(
                LedgerTransactionRow,
                LedgerEntryRow.transaction_id == LedgerTransactionRow.transaction_id,
            )
            .where(LedgerTransactionRow.transaction_type == _OPENING_BALANCE)
            .where(LedgerEntryRow.account_code == _CASH_ACCOUNT)
            .order_by(LedgerEntryRow.portfolio_id.asc(), LedgerEntryRow.currency.asc())
        ).scalars()

        funding: dict[str, list[tuple[Money, Instant]]] = {}
        for row in rows:
            amount = Money(decimal_from_str(row.amount), row.currency)
            if amount.amount <= 0:
                continue
            funding.setdefault(row.portfolio_id, []).append((amount, Instant(row.occurred_at)))

        for portfolio_id, amounts in funding.items():
            engine.fund(
                portfolio_id,
                [amount for amount, _ in amounts],
                at=min(moment for _, moment in amounts),
            )

    # -- recomputation -------------------------------------------------------

    def _rebuild_snapshot(self, builder: SnapshotBuilder, snapshot_id: str, as_of: Instant) -> None:
        """Rebuild one snapshot from its recorded members and compare identity.

        The members supply which raw facts were visible; the *universe* is
        resolved afresh from the replayed reference data, so a ticker change
        that failed to replay surfaces here as a manifest hash divergence
        rather than several cycles later as a mispriced order.
        """
        recorded = self.recorded.get(SnapshotRow, snapshot_id)
        if recorded is None:  # pragma: no cover - the caller enumerated it
            raise IntegrityError(f"Snapshot {snapshot_id} vanished mid-replay.")

        members = self.recorded.execute(
            select(SnapshotMemberRow).where(SnapshotMemberRow.snapshot_id == snapshot_id)
        ).scalars()
        candidates = [
            SnapshotCandidate(
                member.record_type,
                IngestedRecord(
                    blob_hash=member.blob_hash, first_seen_at=Instant(member.first_seen_at)
                ),
            )
            for member in members
        ]
        manifest = builder.build(candidates, as_of=as_of, run_id=self.config.run.run_id)

        self._count("SNAPSHOT")
        self._compare(
            "SNAPSHOT", snapshot_id, "snapshot_id", recorded.snapshot_id, manifest.snapshot_id
        )
        self._compare(
            "SNAPSHOT", snapshot_id, "manifest_hash", recorded.manifest_hash, manifest.manifest_hash
        )
        self._compare("SNAPSHOT", snapshot_id, "status", recorded.status, str(manifest.status))
        self._compare(
            "SNAPSHOT",
            snapshot_id,
            "member_count",
            str(recorded.member_count),
            str(manifest.member_count),
        )

    def _check_decisions(self, executions: Sequence[ArmExecution]) -> None:
        """Re-derive each decision's fingerprint from its stored payload.

        Not a re-elicitation — see the module docstring. It does catch a
        payload edited after sealing, and a change to
        :func:`decision_content_hash` that would silently reinterpret every
        historical arm comparison.
        """
        for execution in executions:
            self._count("DECISION")
            self._compare(
                "DECISION",
                execution.bundle_id,
                "content_hash",
                execution.content_hash,
                decision_content_hash(execution.outcome),
            )

    def _replay_resolutions(self, side: _ReplaySide, *, through: Instant) -> None:
        """Recompute every forecast verdict from the replayed snapshots.

        Skipped when the recorded run resolved nothing: there is no claim to
        check, and recomputing verdicts to compare against an empty set would
        report every one of them as a divergence.
        """
        run_id = self.config.run.run_id
        if not self._recorded_resolutions():
            return

        market = MarketRecordBuilder(self.replayed, side.builder).build(run_id, through=through)
        forecasts = ForecastCollector(self.replayed, self.blobs, side.panels).collect(run_id)
        ResolutionService(self.replayed, self.clock, side.events).resolve(
            forecasts, market, run_id=run_id
        )

    # -- comparison ----------------------------------------------------------

    def _compare_orders(self) -> None:
        recorded = {row.order_id: row for row in self.recorded.execute(select(OrderRow)).scalars()}
        recomputed = {
            row.order_id: row for row in self.replayed.execute(select(OrderRow)).scalars()
        }
        self._compare_keysets("ORDER", set(recorded), set(recomputed))
        for order_id in sorted(set(recorded) & set(recomputed)):
            left, right = recorded[order_id], recomputed[order_id]
            self._count("ORDER")
            for name in ("instrument_id", "side", "quantity", "currency", "execute_after"):
                self._compare(
                    "ORDER", order_id, name, str(getattr(left, name)), str(getattr(right, name))
                )

    def _compare_fills(self) -> None:
        recorded = {row.fill_id: row for row in self.recorded.execute(select(FillRow)).scalars()}
        recomputed = {row.fill_id: row for row in self.replayed.execute(select(FillRow)).scalars()}
        self._compare_keysets("FILL", set(recorded), set(recomputed))
        for fill_id in sorted(set(recorded) & set(recomputed)):
            left, right = recorded[fill_id], recomputed[fill_id]
            self._count("FILL")
            for name in (
                "quantity",
                "price",
                "gross",
                "fee",
                "slippage",
                "realized_pnl",
                "executed_at",
                "settles_at",
                "transaction_id",
            ):
                self._compare(
                    "FILL", fill_id, name, str(getattr(left, name)), str(getattr(right, name))
                )

    def _compare_ledger(self) -> None:
        recorded = _ledger_totals(self.recorded)
        recomputed = _ledger_totals(self.replayed)
        self._compare_keysets("LEDGER", set(recorded), set(recomputed))
        for key in sorted(set(recorded) & set(recomputed)):
            self._count("LEDGER")
            self._compare("LEDGER", key, "balance", str(recorded[key]), str(recomputed[key]))

    def _compare_positions(self) -> None:
        recorded = _position_totals(self.recorded)
        recomputed = _position_totals(self.replayed)
        self._compare_keysets("POSITION", set(recorded), set(recomputed))
        for key in sorted(set(recorded) & set(recomputed)):
            self._count("POSITION")
            self._compare("POSITION", key, "quantity", str(recorded[key]), str(recomputed[key]))

    def _compare_resolutions(self) -> None:
        recorded = {row.forecast_id: row for row in self._recorded_resolutions()}
        recomputed = {
            row.forecast_id: row
            for row in self.replayed.execute(select(ForecastResolutionRow)).scalars()
        }
        self._compare_keysets("RESOLUTION", set(recorded), set(recomputed))
        for forecast_id in sorted(set(recorded) & set(recomputed)):
            left, right = recorded[forecast_id], recomputed[forecast_id]
            self._count("RESOLUTION")
            for name in ("status", "outcome_up", "target_at", "total_return"):
                self._compare(
                    "RESOLUTION",
                    forecast_id,
                    name,
                    str(getattr(left, name)),
                    str(getattr(right, name)),
                )

    # -- bookkeeping ---------------------------------------------------------

    def _compare_keysets(self, kind: str, recorded: set[str], recomputed: set[str]) -> None:
        for missing in sorted(recorded - recomputed):
            self._divergences.append(Divergence(kind, missing, "existence", "present", "absent"))
        for extra in sorted(recomputed - recorded):
            self._divergences.append(Divergence(kind, extra, "existence", "absent", "present"))

    def _compare(self, kind: str, key: str, name: str, recorded: str, recomputed: str) -> None:
        if recorded != recomputed:
            self._divergences.append(Divergence(kind, key, name, recorded, recomputed))

    def _count(self, kind: str) -> None:
        self._compared[kind] = self._compared.get(kind, 0) + 1

    # -- reading the recorded side ------------------------------------------

    def _recorded_cycles(self) -> list[tuple[str, Instant]]:
        rows = self.recorded.execute(
            select(SnapshotRow.snapshot_id, SnapshotRow.as_of)
            .where(SnapshotRow.run_id == self.config.run.run_id)
            .order_by(SnapshotRow.as_of.asc())
        )
        return [(str(snapshot_id), Instant(str(as_of))) for snapshot_id, as_of in rows]

    def _recorded_bundles(self) -> list[DecisionBundleRow]:
        return list(
            self.recorded.execute(
                select(DecisionBundleRow).where(DecisionBundleRow.run_id == self.config.run.run_id)
            ).scalars()
        )

    def _recorded_resolutions(self) -> list[ForecastResolutionRow]:
        return list(
            self.recorded.execute(
                select(ForecastResolutionRow).where(
                    ForecastResolutionRow.run_id == self.config.run.run_id
                )
            ).scalars()
        )

    def _sealed_executions(self, snapshot_id: str) -> list[ArmExecution]:
        """One cycle's sealed decisions, in the order they were originally run.

        Read through a :class:`CycleRunner` pointed at the *recorded*
        database, so the rehydration path is the platform's own rather than a
        second parser that could disagree with it.
        """
        reader = self._recorded_reader()
        rows = self.recorded.execute(
            select(DecisionBundleRow)
            .where(DecisionBundleRow.run_id == self.config.run.run_id)
            .where(DecisionBundleRow.snapshot_id == snapshot_id)
            .order_by(DecisionBundleRow.position.asc())
        ).scalars()
        loaded = (reader.load_execution(row.bundle_id) for row in rows)
        return [execution for execution in loaded if execution is not None]

    def _recorded_reader(self) -> CycleRunner:
        events = EventStore(self.recorded, self.clock)
        return CycleRunner(
            session=self.recorded,
            clock=self.clock,
            blobs=self.blobs,
            events=events,
            builder=SnapshotBuilder(
                self.recorded,
                self.clock,
                self.blobs,
                InstrumentRepository(self.recorded, self.clock),
                events,
            ),
            model_factory=_no_model,
            materials=_NoMaterials(),
            config=self.config.run,
        )


# -- helpers -------------------------------------------------------------------


def _ledger_totals(session: Session) -> dict[str, Decimal]:
    """Balance per portfolio|account|subject|currency.

    Compared as balances rather than as individual entries because an entry id
    embeds its transaction id, which legitimately differs when nothing else
    does. What must match to the cent is what the books *say*.
    """
    totals: dict[str, Decimal] = {}
    for row in session.execute(select(LedgerEntryRow)).scalars():
        key = "|".join((row.portfolio_id, row.account_code, row.subject, row.currency))
        totals[key] = totals.get(key, Decimal(0)) + decimal_from_str(row.amount)
    return totals


def _position_totals(session: Session) -> dict[str, Decimal]:
    """Net quantity per portfolio|instrument, folded from the events."""
    totals: dict[str, Decimal] = {}
    for row in session.execute(select(PositionEventRow)).scalars():
        key = "|".join((row.portfolio_id, row.instrument_id))
        totals[key] = totals.get(key, Decimal(0)) + decimal_from_str(row.quantity_delta)
    return totals


class _NoMaterials:
    """A replay grants nothing, because it never asks a model anything."""

    def materials_for(
        self, arm: ArmSpec, *, cycle_id: str, as_of: Instant, repetition: int
    ) -> str | None:
        return None


def _no_model() -> LanguageModel:
    """Refuse to build a model.

    A replay reads sealed decisions; reaching for a model means it was about
    to produce a *second, different* one and call the difference a divergence.
    """
    raise ConfigurationError(
        "A replay attempted to construct a language model. Decisions are read from "
        "their sealed bundles, never re-elicited."
    )
