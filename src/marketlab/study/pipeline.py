"""One study, assembled and run (§29.1, §30.6).

This is the single place the whole component graph is wired together — the
instrument repository, the synthetic provider, the ingestion pipeline, the
snapshot builder, the ledger, the position book, the execution engine, the
corporate-action applier, memory, reflection, the panel store and the cycle
runner. Before it existed, every caller assembled its own; the CLI would have
been the fourth, and the fourth assembly is where two of them start to differ
in a way nobody notices.

Resumable by construction, not by care
--------------------------------------
:meth:`Study.run` can be called again on the same database and will continue
where it stopped rather than redo or duplicate anything. That is not a feature
implemented here: it falls out of every layer beneath being idempotent on a
derived identifier — snapshots by ``(run_id, as_of)``, decisions by
``(run, cycle, arm, repetition)``, orders by ``(portfolio, bundle, instrument,
side)``, funding by its opening transaction, episodes by ``(scope, bundle)``.
What this module adds is a single entry point that exercises them in the
supported order, so "resume the study" is one call rather than a sequence a
caller has to reconstruct correctly.

Ingestion is replayed from the provider, not from the network
--------------------------------------------------------------
The Phase 1 world is a closed-form function of a session index, so re-running
an interrupted study regenerates byte-identical records and the content-
addressed store recognises them as already present. Under a real Phase 3
provider that is no longer true, and ingestion would have to read from what
was stored rather than re-fetch — §P2's rule that a study never calls a
provider twice for the same fact.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketlab.accounting.ledger import Ledger
from marketlab.accounting.positions import PositionBook
from marketlab.analysis.plan import AnalysisPlan, AnalysisReport
from marketlab.core.clock import Clock
from marketlab.core.instants import Instant
from marketlab.evaluation.collection import ForecastCollector
from marketlab.evaluation.panels import PanelBundleRow, PanelStore
from marketlab.evaluation.resolution import (
    ForecastResolutionRow,
    MarketRecordBuilder,
    ResolutionReport,
    ResolutionService,
)
from marketlab.execution.corporate import CorporateActionApplier
from marketlab.execution.engine import ExecutionEngine, FillRow, OrderRow
from marketlab.experiments.driver import CycleDriver, portfolios_for
from marketlab.experiments.materials import GrantedMaterialsProvider, MemoryRecorder
from marketlab.experiments.runner import CycleRunner, DecisionBundleRow
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.synthetic import SyntheticMarketDataProvider, admit_synthetic_universe
from marketlab.instruments.calendars import CalendarRegistry
from marketlab.instruments.repository import InstrumentRepository
from marketlab.memory.store import MemoryStore
from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.models.types import LanguageModel
from marketlab.reflection.engine import ReflectionEngine
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate, SnapshotRow
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore
from marketlab.study.config import StudyConfig, StudyRegistry

__all__ = ["RunSummary", "Study", "open_study"]


@dataclass(frozen=True, slots=True)
class RunSummary:
    """What one study currently contains. Counted, never claimed."""

    run_id: str
    cycles: int
    decisions: int
    missing: int
    panels: int
    orders: int
    fills: int
    resolutions: int

    def as_payload(self) -> dict[str, int | str]:
        return {
            "run_id": self.run_id,
            "cycles": self.cycles,
            "decisions": self.decisions,
            "missing": self.missing,
            "panels": self.panels,
            "orders": self.orders,
            "fills": self.fills,
            "resolutions": self.resolutions,
        }


@dataclass(slots=True)
class Study:
    """One assembled study, ready to run, resolve, analyse or inspect."""

    config: StudyConfig
    session: Session
    clock: Clock
    blobs: BlobStore
    events: EventStore
    builder: SnapshotBuilder
    calendars: CalendarRegistry
    provider: SyntheticMarketDataProvider
    pipeline: IngestionPipeline
    driver: CycleDriver
    engine: ExecutionEngine
    panels: PanelStore

    # -- running -------------------------------------------------------------

    def cutoffs(self) -> tuple[Instant, ...]:
        """The study's full sequence of decision instants."""
        return self.provider.session_cutoffs()

    def fund(self) -> None:
        """Open every condition's book. Idempotent."""
        for portfolio_id in portfolios_for(self.config.run_config()).values():
            self.engine.fund(portfolio_id, list(self.config.capital()), at=self.config.start_at)

    def run(self, *, sessions: int | None = None) -> RunSummary:
        """Run the study to completion, or to ``sessions`` cycles.

        Safe to call again: every step underneath is idempotent on a derived
        identifier, so a resumed study continues rather than duplicating.
        """
        self.fund()
        limit = len(self.cutoffs()) if sessions is None else min(sessions, len(self.cutoffs()))
        candidates: list[SnapshotCandidate] = []

        for cycle_index, cutoff in enumerate(self.cutoffs()[:limit]):
            candidates.extend(self._ingest(cutoff))
            manifest = self.builder.build(candidates, as_of=cutoff, run_id=self.config.run_id)
            self.driver.run(cycle_index=cycle_index, snapshot_id=manifest.snapshot_id, as_of=cutoff)
        self.session.commit()
        return self.summary()

    def _ingest(self, cutoff: Instant) -> list[SnapshotCandidate]:
        """Every raw record this session makes visible.

        Content-addressed, so re-ingesting an already-stored record is a
        lookup rather than a second write — which is what makes the whole run
        method safe to repeat.
        """
        collected: list[SnapshotCandidate] = []
        for bar in self.provider.fetch_price_bars(cutoff):
            collected.append(SnapshotCandidate("PRICE_BAR", self.pipeline.ingest_price_bar(bar)))
        for item in self.provider.fetch_news(cutoff):
            collected.append(SnapshotCandidate("NEWS_ITEM", self.pipeline.ingest_news_item(item)))
        for record in self.provider.fetch_macro_records(cutoff):
            collected.append(
                SnapshotCandidate("MACRO_RECORD", self.pipeline.ingest_macro_record(record))
            )
        for rate in self.provider.fetch_fx_rates(cutoff):
            collected.append(SnapshotCandidate("FX_RATE", self.pipeline.ingest_fx_rate(rate)))
        for action in self.provider.fetch_corporate_actions(cutoff):
            collected.append(
                SnapshotCandidate("CORPORATE_ACTION", self.pipeline.ingest_corporate_action(action))
            )
        return collected

    # -- resolving and analysing --------------------------------------------

    def resolve(self, *, now: Instant | None = None) -> ResolutionReport:
        """Resolve every forecast whose horizon has elapsed.

        Raises:
            marketlab.core.failures.ConfigurationError: if the study has no
                snapshots at all. Resolving a run that never happened would
                report a clean zero.
        """
        through = now if now is not None else self._last_cutoff()
        record = MarketRecordBuilder(self.session, self.builder).build(
            self.config.run_id, through=through
        )
        forecasts = ForecastCollector(self.session, self.blobs, self.panels).collect(
            self.config.run_id
        )
        report = ResolutionService(self.session, self.clock, self.events).resolve(
            forecasts, record, run_id=self.config.run_id
        )
        self.session.commit()
        return report

    def analyse(self, plan: AnalysisPlan) -> AnalysisReport:
        """Run a pre-registered analysis over this study's verdicts."""
        return plan.run(self.resolutions())

    def resolutions(self) -> list[ForecastResolutionRow]:
        return list(
            ResolutionService(self.session, self.clock, self.events).resolutions_for(
                self.config.run_id
            )
        )

    # -- inspecting ----------------------------------------------------------

    def summary(self) -> RunSummary:
        run_id = self.config.run_id
        bundles = self._count(DecisionBundleRow, DecisionBundleRow.run_id == run_id)
        return RunSummary(
            run_id=run_id,
            cycles=self._count(SnapshotRow, SnapshotRow.run_id == run_id),
            decisions=bundles,
            missing=self._missing_conditions(),
            panels=self._count(PanelBundleRow, PanelBundleRow.run_id == run_id),
            orders=self._count_books(OrderRow.portfolio_id),
            fills=self._count_books(FillRow.portfolio_id),
            resolutions=self._count(ForecastResolutionRow, ForecastResolutionRow.run_id == run_id),
        )

    def verify_chain(self) -> int:
        return self.events.verify_chain()

    def _last_cutoff(self) -> Instant:
        cutoffs = self.cutoffs()
        latest = self.session.execute(
            select(func.max(SnapshotRow.as_of)).where(SnapshotRow.run_id == self.config.run_id)
        ).scalar_one_or_none()
        return Instant(str(latest)) if latest is not None else cutoffs[0]

    def _count(self, model: type, where: object) -> int:
        total = self.session.execute(
            select(func.count()).select_from(model).where(where)  # type: ignore[arg-type]
        ).scalar_one()
        return int(total)

    def _count_books(self, column: object) -> int:
        books = list(portfolios_for(self.config.run_config()).values())
        total = self.session.execute(
            select(func.count()).select_from(column.parent).where(column.in_(books))  # type: ignore[attr-defined]
        ).scalar_one()
        return int(total)

    def _missing_conditions(self) -> int:
        return sum(
            1
            for record in self.events.iter_events(event_type="CONDITION_MISSING")
            if record.payload.get("arm_id") is not None
        )


def open_study(
    config: StudyConfig,
    *,
    session: Session,
    clock: Clock,
    blobs: BlobStore,
    model_factory: Callable[[], LanguageModel] = DeterministicPolicyModel,
    declare: bool = True,
) -> Study:
    """Assemble every component one study needs.

    ``declare`` writes the configuration under its ``run_id`` (and refuses a
    changed one). A ``--dry-run`` passes ``False``: validating a configuration
    must not be the thing that pre-registers it.
    """
    # Declared first, before anything is admitted or ingested: a run whose
    # parameters contradict its declaration must be refused before it has
    # written a single fact under that run_id.
    if declare:
        StudyRegistry(session, clock, blobs).declare(config)

    events = EventStore(session, clock)
    instruments = InstrumentRepository(session, clock)
    calendars = CalendarRegistry()
    universe = admit_synthetic_universe(instruments, calendars, config.start_at)
    provider = SyntheticMarketDataProvider(
        equity_calendar=universe.equity_calendar,
        start_at=config.start_at,
        num_sessions=config.sessions,
        alpha_id=universe.alpha.instrument_id,
        beta_id=universe.beta.instrument_id,
        gamma_id=universe.gamma.instrument_id,
        delta_id=universe.delta.instrument_id,
    )
    pipeline = IngestionPipeline(
        blobs,
        events,
        session,
        clock,
        source_id=config.world,
        licence="internal-synthetic",
        redistributable=True,
    )
    builder = SnapshotBuilder(session, clock, blobs, instruments, events)
    ledger = Ledger(session, clock)
    positions = PositionBook(session)
    engine = ExecutionEngine(
        session=session,
        clock=clock,
        events=events,
        ledger=ledger,
        positions=positions,
        calendars=calendars,
        policy=config.execution_policy(),
        base_currency=config.base_currency,
    )
    memory = MemoryStore(session, clock, blobs)
    reflection = ReflectionEngine(session, clock, blobs)
    panels = PanelStore(session, clock, blobs)

    driver = CycleDriver(
        runner=CycleRunner(
            session=session,
            clock=clock,
            blobs=blobs,
            events=events,
            builder=builder,
            model_factory=model_factory,
            materials=GrantedMaterialsProvider(
                run_id=config.run_id,
                memory=memory,
                reflection=reflection,
                recall_limit=config.recall_limit,
            ),
            config=config.run_config(),
            panels=panels if config.panel else None,
        ),
        engine=engine,
        applier=CorporateActionApplier(
            session=session,
            clock=clock,
            events=events,
            ledger=ledger,
            positions=positions,
            instruments=instruments,
        ),
        recorder=MemoryRecorder(
            run_id=config.run_id,
            memory=memory,
            reflection=reflection,
            reflection_interval=config.reflection_interval,
            recall_limit=config.recall_limit,
        ),
    )
    return Study(
        config=config,
        session=session,
        clock=clock,
        blobs=blobs,
        events=events,
        builder=builder,
        calendars=calendars,
        provider=provider,
        pipeline=pipeline,
        driver=driver,
        engine=engine,
        panels=panels,
    )


def starting_capital_total(amounts: Sequence[Decimal]) -> Decimal:
    """Sum of nominal starting amounts, for display only — never valuation.

    Adding figures in different currencies is meaningless as money; this is a
    one-line human summary of what was funded, and the only place in the
    platform where such a sum appears.
    """
    return sum(amounts, Decimal(0))
