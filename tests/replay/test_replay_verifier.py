"""Replay that recomputes and compares, and can actually fail (§12.5, §30.4).

The predecessor of this project shipped an "exact replay" verifier that
returned success unconditionally. So the positive test here — a clean replay
of a real run reports no divergence — is the *less* important half. The tests
that matter are the ones below it, which corrupt a recorded run in several
different ways and require the verifier to notice each one. Without them, the
green tick above would mean nothing at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from marketlab.accounting.ledger import Ledger
from marketlab.accounting.positions import PositionBook
from marketlab.core.clock import FrozenClock
from marketlab.core.failures import ConfigurationError, IntegrityError
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.core.money import Money
from marketlab.evaluation.collection import ForecastCollector
from marketlab.evaluation.panels import PanelStore
from marketlab.evaluation.resolution import (
    MarketRecordBuilder,
    ResolutionService,
    ResolutionStatus,
)
from marketlab.execution.corporate import CorporateActionApplier
from marketlab.execution.engine import ExecutionEngine, portfolio_id_for
from marketlab.execution.policy import ExecutionPolicy
from marketlab.experiments.arms import ArmId
from marketlab.experiments.context import NullMaterialsProvider
from marketlab.experiments.driver import CycleDriver
from marketlab.experiments.runner import CycleRunner, RunConfig
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.synthetic import SyntheticMarketDataProvider, admit_synthetic_universe
from marketlab.instruments.calendars import CalendarRegistry
from marketlab.instruments.repository import InstrumentRepository
from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.replay.verifier import ReplayConfig, ReplayReport, ReplayVerifier
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate
from marketlab.storage.blobs import BlobStore
from marketlab.storage.database import Database
from marketlab.storage.events import EventStore

START_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
NUM_SESSIONS = 12
RUN_ID = "REPLAY_RUN"
ARMS = (ArmId.A, ArmId.B)
TARGET_WEIGHT = Decimal("0.05")


def usd(amount: str) -> Money:
    return Money(Decimal(amount), "USD")


def eur(amount: str) -> Money:
    return Money(Decimal(amount), "EUR")


@dataclass
class Recorded:
    """One completed study, plus everything a replay must be handed."""

    session: Session
    blobs: BlobStore
    clock: FrozenClock
    calendars: CalendarRegistry
    run: RunConfig
    cutoffs: tuple[Instant, ...]


def _record_a_study(session: Session, clock: FrozenClock, blobs: BlobStore) -> Recorded:
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
        blobs,
        events,
        session,
        clock,
        source_id="SYNTHETIC",
        licence="internal-synthetic",
        redistributable=True,
    )
    builder = SnapshotBuilder(session, clock, blobs, repo, events)
    ledger = Ledger(session, clock)
    positions = PositionBook(session)
    engine = ExecutionEngine(
        session=session,
        clock=clock,
        events=events,
        ledger=ledger,
        positions=positions,
        calendars=calendars,
        policy=ExecutionPolicy(target_weight=TARGET_WEIGHT),
    )
    applier = CorporateActionApplier(
        session=session,
        clock=clock,
        events=events,
        ledger=ledger,
        positions=positions,
        instruments=repo,
    )
    run = RunConfig(run_id=RUN_ID, arms=ARMS)
    panels = PanelStore(session, clock, blobs)
    driver = CycleDriver(
        runner=CycleRunner(
            session=session,
            clock=clock,
            blobs=blobs,
            events=events,
            builder=builder,
            model_factory=DeterministicPolicyModel,
            materials=NullMaterialsProvider(),
            config=run,
            panels=panels,
        ),
        engine=engine,
        applier=applier,
    )

    for arm in ARMS:
        engine.fund(
            portfolio_id_for(RUN_ID, str(arm), 0),
            [usd("1000000.00"), eur("500000.00")],
            at=START_AT,
        )

    candidates: list[SnapshotCandidate] = []
    cutoffs: list[Instant] = []
    for index_number, cutoff in enumerate(provider.session_cutoffs()):
        for bar in provider.fetch_price_bars(cutoff):
            candidates.append(SnapshotCandidate("PRICE_BAR", pipeline.ingest_price_bar(bar)))
        for item in provider.fetch_news(cutoff):
            candidates.append(SnapshotCandidate("NEWS_ITEM", pipeline.ingest_news_item(item)))
        for rate in provider.fetch_fx_rates(cutoff):
            candidates.append(SnapshotCandidate("FX_RATE", pipeline.ingest_fx_rate(rate)))
        for action in provider.fetch_corporate_actions(cutoff):
            candidates.append(
                SnapshotCandidate("CORPORATE_ACTION", pipeline.ingest_corporate_action(action))
            )
        manifest = builder.build(candidates, as_of=cutoff, run_id=RUN_ID)
        driver.run(cycle_index=index_number, snapshot_id=manifest.snapshot_id, as_of=cutoff)
        cutoffs.append(cutoff)

    market = MarketRecordBuilder(session, builder).build(RUN_ID, through=cutoffs[-1])
    forecasts = ForecastCollector(session, blobs, panels).collect(RUN_ID)
    ResolutionService(session, clock, events).resolve(forecasts, market, run_id=RUN_ID)
    session.commit()

    return Recorded(
        session=session,
        blobs=blobs,
        clock=clock,
        calendars=calendars,
        run=run,
        cutoffs=tuple(cutoffs),
    )


@pytest.fixture
def recorded(session: Session, clock: FrozenClock, blob_store: BlobStore) -> Recorded:
    return _record_a_study(session, clock, blob_store)


@pytest.fixture
def replay_session(tmp_path: Path) -> Session:
    database = Database(tmp_path / "replay.db")
    database.create_schema()
    with database.session_scope() as active:
        yield active
    database.close()


def _verify(
    recorded: Recorded,
    replay_session: Session,
    *,
    policy: ExecutionPolicy | None = None,
    calendars: CalendarRegistry | None = None,
) -> ReplayReport:
    return ReplayVerifier(
        recorded=recorded.session,
        replayed=replay_session,
        blobs=recorded.blobs,
        clock=recorded.clock,
        config=ReplayConfig(
            run=recorded.run,
            calendars=calendars or recorded.calendars,
            policy=policy or ExecutionPolicy(target_weight=TARGET_WEIGHT),
        ),
    ).verify()


# ---------------------------------------------------------------------------
# The run being replayed is a real one
# ---------------------------------------------------------------------------


def test_the_recorded_study_actually_traded_and_resolved(recorded: Recorded) -> None:
    """Guards every assertion below against passing on an empty study."""
    resolutions = ResolutionService(
        recorded.session, recorded.clock, EventStore(recorded.session, recorded.clock)
    ).resolutions_for(RUN_ID)
    assert resolutions
    assert any(row.status == str(ResolutionStatus.RESOLVED) for row in resolutions)
    fills = recorded.session.execute(text("SELECT COUNT(*) FROM fills")).scalar_one()
    assert int(fills) > 0


# ---------------------------------------------------------------------------
# A clean replay
# ---------------------------------------------------------------------------


def test_a_clean_replay_is_exact(recorded: Recorded, replay_session: Session) -> None:
    report = _verify(recorded, replay_session)
    assert report.divergences == (), [str(d) for d in report.divergences]
    assert report.is_exact


def test_a_clean_replay_actually_compared_every_kind_of_artefact(
    recorded: Recorded, replay_session: Session
) -> None:
    """The assertion the predecessor's verifier could not have made. An exact
    replay that compared nothing is the failure this whole module exists to
    rule out."""
    report = _verify(recorded, replay_session)
    for kind in ("SNAPSHOT", "DECISION", "ORDER", "FILL", "LEDGER", "POSITION", "RESOLUTION"):
        assert report.compared.get(kind, 0) > 0, f"{kind} was never compared: {report.summary()}"


def test_the_replay_recomputed_the_books_rather_than_copying_them(
    recorded: Recorded, replay_session: Session
) -> None:
    """The replay database starts empty. If fills exist in it afterwards, they
    were produced by running the execution engine, not read across."""
    assert int(replay_session.execute(text("SELECT COUNT(*) FROM fills")).scalar_one()) == 0
    _verify(recorded, replay_session)
    assert int(replay_session.execute(text("SELECT COUNT(*) FROM fills")).scalar_one()) > 0
    assert int(replay_session.execute(text("SELECT COUNT(*) FROM ledger_entries")).scalar_one()) > 0


def test_a_replay_never_reaches_for_a_model() -> None:
    """Structural, not conventional. Every runner a replay builds is handed a
    factory that raises, so a future change that started re-eliciting
    decisions would fail loudly instead of quietly comparing a second draw
    against the first and calling the difference a divergence."""
    from marketlab.replay.verifier import _no_model

    with pytest.raises(ConfigurationError, match="never re-elicited"):
        _no_model()


# ---------------------------------------------------------------------------
# ...and it can fail
# ---------------------------------------------------------------------------


def test_a_replay_under_a_different_execution_policy_diverges(
    recorded: Recorded, replay_session: Session
) -> None:
    """Sizing is configuration a replay must be handed. Handed a different
    target weight it is a different study, and saying so is correct."""
    report = _verify(
        recorded, replay_session, policy=ExecutionPolicy(target_weight=Decimal("0.20"))
    )
    assert not report.is_exact
    assert report.divergences_of("ORDER")
    assert any(d.field_name == "quantity" for d in report.divergences_of("ORDER"))


def test_a_tampered_fill_price_is_reported(
    recorded: Recorded, replay_session: Session, database: Database
) -> None:
    """The append-only triggers refuse an ordinary UPDATE, so the corruption
    goes through the audited migration window — which is exactly the shape a
    real tampering incident would have."""
    recorded.session.commit()
    with database.migration_mode(reason="test: corrupt a fill", author="test-suite") as engine:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE fills SET price = '1.00' WHERE fill_id = "
                    "(SELECT fill_id FROM fills ORDER BY fill_id LIMIT 1)"
                )
            )
    recorded.session.expire_all()

    report = _verify(recorded, replay_session)
    assert not report.is_exact
    assert any(d.field_name == "price" for d in report.divergences_of("FILL"))


def test_a_tampered_decision_payload_is_reported(
    recorded: Recorded, replay_session: Session, database: Database
) -> None:
    """The decision is an input to the replay, but its fingerprint is not: a
    content hash that no longer matches the payload behind it is caught."""
    recorded.session.commit()
    with database.migration_mode(reason="test: corrupt a bundle", author="test-suite") as engine:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE decision_bundles SET content_hash = "
                    "'0000000000000000000000000000000000000000000000000000000000000000'"
                )
            )
    recorded.session.expire_all()

    report = _verify(recorded, replay_session)
    assert not report.is_exact
    assert report.divergences_of("DECISION")


def test_a_tampered_resolution_is_reported(
    recorded: Recorded, replay_session: Session, database: Database
) -> None:
    recorded.session.commit()
    with database.migration_mode(reason="test: corrupt a verdict", author="test-suite") as engine:
        with engine.begin() as connection:
            connection.execute(text("UPDATE forecast_resolutions SET outcome_up = NOT outcome_up"))
    recorded.session.expire_all()

    report = _verify(recorded, replay_session)
    assert not report.is_exact
    assert any(d.field_name == "outcome_up" for d in report.divergences_of("RESOLUTION"))


def test_a_deleted_order_is_reported_as_missing_rather_than_ignored(
    recorded: Recorded, replay_session: Session, database: Database
) -> None:
    """A comparison that only walked the recorded side would miss an artefact
    the replay produced and the recording does not have."""
    recorded.session.commit()
    with database.migration_mode(reason="test: drop an order", author="test-suite") as engine:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM orders WHERE order_id = (SELECT order_id FROM orders LIMIT 1)")
            )
    recorded.session.expire_all()

    report = _verify(recorded, replay_session)
    assert not report.is_exact
    existence = [d for d in report.divergences_of("ORDER") if d.field_name == "existence"]
    assert existence and existence[0].recomputed == "present"


def test_replaying_a_run_that_was_never_recorded_is_an_error(
    recorded: Recorded, replay_session: Session
) -> None:
    """Not an exact replay of nothing. This is the precise shape of the defect
    the audit found, so it is refused rather than reported as success."""
    verifier = ReplayVerifier(
        recorded=recorded.session,
        replayed=replay_session,
        blobs=recorded.blobs,
        clock=recorded.clock,
        config=ReplayConfig(
            run=RunConfig(run_id="NEVER_RAN", arms=ARMS), calendars=recorded.calendars
        ),
    )
    with pytest.raises(IntegrityError, match="nothing to replay"):
        verifier.verify()


def test_an_empty_report_is_not_an_exact_one() -> None:
    assert not ReplayReport(run_id="X", compared={}, divergences=()).is_exact
