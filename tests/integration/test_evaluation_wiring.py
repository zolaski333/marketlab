"""End-to-end wiring test for task 12: six conditions answer an imposed panel,
the panel is resolved in total return, and the pre-registered analysis runs on
the result.

This is the first point at which the platform produces a *scientific* output
rather than a record of activity. Unit tests pin each piece against a
hand-built fixture; only a run like this catches the ways they disagree — a
horizon counted off the wrong grid, a split that makes every arm look
incompetent, a pairing that quietly drops half the panel, or an analysis that
reports a difference between conditions that decided identically.

Twenty sessions, because the scripted world puts a dividend at session 10 and a
2-for-1 split at session 18, and both must land inside some forecast's horizon
for the total-return arithmetic to be exercised at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from marketlab.analysis.equivalence import EquivalenceVerdict, Rope
from marketlab.analysis.pairing import pair_scores
from marketlab.analysis.plan import PRIMARY_CONTRASTS, AnalysisPlan
from marketlab.core.clock import FrozenClock
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.evaluation.collection import ForecastCollector
from marketlab.evaluation.panels import PanelStore
from marketlab.evaluation.resolution import (
    ForecastResolutionRow,
    ForecastSource,
    MarketRecordBuilder,
    ResolutionReport,
    ResolutionService,
    ResolutionStatus,
)
from marketlab.experiments.arms import ArmId
from marketlab.experiments.context import NullMaterialsProvider
from marketlab.experiments.runner import CycleRunner, RunConfig
from marketlab.forecasting.panel import DEFAULT_HORIZONS
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.synthetic import SyntheticMarketDataProvider, admit_synthetic_universe
from marketlab.instruments.calendars import CalendarRegistry
from marketlab.instruments.repository import InstrumentRepository
from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

START_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
NUM_SESSIONS = 20
RUN_ID = "EVALUATION_WIRING_RUN"
HORIZONS = (1, 5, 20)
SPLIT_SESSION = 18  # 1-based, per marketlab.ingestion.synthetic
DIVIDEND_SESSION = 10


@dataclass
class World:
    session: Session
    resolutions: tuple[ForecastResolutionRow, ...]
    report: ResolutionReport
    cutoffs: tuple[Instant, ...]
    alpha_id: str


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    """Module-scoped because it runs 120 decisions and 120 panels.

    Nothing below writes to the database, so sharing it cannot let one test
    pass on another's writes — the reason every other fixture in this suite is
    function-scoped.
    """
    from marketlab.storage.database import Database

    root = tmp_path_factory.mktemp("evaluation-wiring")
    database = Database(root / "study.db")
    database.create_schema()
    blobs = BlobStore(root / "blobs")
    clock = FrozenClock(datetime(2026, 9, 1, 12, 0, tzinfo=UTC))

    with database.session_scope() as session:
        built = _run_study(session, clock, blobs)
    return built


def _run_study(session: Session, clock: FrozenClock, blobs: BlobStore) -> World:
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
    panels = PanelStore(session, clock, blobs)
    runner = CycleRunner(
        session=session,
        clock=clock,
        blobs=blobs,
        events=events,
        builder=builder,
        model_factory=DeterministicPolicyModel,
        materials=NullMaterialsProvider(),
        config=RunConfig(run_id=RUN_ID, panel_horizons=HORIZONS),
        panels=panels,
    )

    candidates: list[SnapshotCandidate] = []
    cutoffs: list[Instant] = []
    for index_number, cutoff in enumerate(provider.session_cutoffs()):
        for bar in provider.fetch_price_bars(cutoff):
            candidates.append(SnapshotCandidate("PRICE_BAR", pipeline.ingest_price_bar(bar)))
        for action in provider.fetch_corporate_actions(cutoff):
            candidates.append(
                SnapshotCandidate("CORPORATE_ACTION", pipeline.ingest_corporate_action(action))
            )
        manifest = builder.build(candidates, as_of=cutoff, run_id=RUN_ID)
        runner.run_cycle(cycle_index=index_number, snapshot_id=manifest.snapshot_id, as_of=cutoff)
        cutoffs.append(cutoff)

    market = MarketRecordBuilder(session, builder).build(RUN_ID, through=cutoffs[-1])
    forecasts = ForecastCollector(session, blobs, panels).collect(RUN_ID)
    service = ResolutionService(session, clock, events)
    report = service.resolve(forecasts, market, run_id=RUN_ID)
    session.commit()

    return World(
        session=session,
        resolutions=service.resolutions_for(RUN_ID),
        report=report,
        cutoffs=tuple(cutoffs),
        alpha_id=universe.alpha.instrument_id,
    )


def _panel_rows(world: World) -> list[ForecastResolutionRow]:
    return [row for row in world.resolutions if row.source == str(ForecastSource.PANEL)]


# ---------------------------------------------------------------------------
# The run produced something to analyse
# ---------------------------------------------------------------------------


def test_every_condition_answered_the_same_imposed_panel(world: World) -> None:
    """Pairing rests entirely on this. If the arms answered different
    questions there would be nothing to compare, and every result below would
    be a comparison of what each arm chose to talk about."""
    by_arm: dict[str, set[tuple[str, str, int]]] = {}
    for row in _panel_rows(world):
        by_arm.setdefault(row.arm_id, set()).add(
            (row.anchor_at, row.instrument_id, row.horizon_sessions)
        )
    assert set(by_arm) == {str(arm) for arm in ArmId}
    assert len(set(map(frozenset, by_arm.values()))) == 1


def test_the_panel_asked_every_pre_registered_horizon(world: World) -> None:
    """Asked, not necessarily answerable: see the next test."""
    asked = {forecast.horizon_sessions for forecast in world.report.pending} | {
        row.horizon_sessions for row in _panel_rows(world)
    }
    assert asked == set(DEFAULT_HORIZONS)


def test_most_forecasts_actually_resolved(world: World) -> None:
    """Guards every assertion below against passing on an empty result set."""
    counts = world.report.counts()
    assert counts[ResolutionStatus.RESOLVED] > 100
    assert counts[ResolutionStatus.UNRESOLVABLE] == 0
    assert counts[ResolutionStatus.INVALID_SOURCE_DATA] == 0


def test_a_horizon_the_run_never_reached_resolves_nothing_at_all(world: World) -> None:
    """A 20-session horizon in a 20-session run never elapses for any forecast,
    so *every* one of them stays pending — and pending is the absence of a
    verdict, not a verdict, so nothing is written down for them.

    This is the shape of result that most invites a fabricated number: an
    analysis that filled the gap with 0.5, or scored the last available price
    as if the horizon had elapsed, would produce a full-looking results table
    for a question the data cannot answer.
    """
    assert {row.horizon_sessions for row in _panel_rows(world)} == {1, 5}
    assert any(forecast.horizon_sessions == 20 for forecast in world.report.pending)
    assert not any(row.horizon_sessions == 20 for row in world.resolutions)


def test_a_forecast_made_too_late_to_elapse_is_pending_rather_than_dropped(
    world: World,
) -> None:
    """The last few sessions' 5-session forecasts have nowhere to resolve to.
    They are reported as pending — countable, and resolvable later if the run
    continues — rather than silently discarded."""
    late = [forecast for forecast in world.report.pending if forecast.horizon_sessions == 5]
    assert late
    assert all(forecast.anchor_at >= world.cutoffs[-5] for forecast in late)
    pending_ids = {forecast.forecast_id for forecast in world.report.pending}
    assert not pending_ids & {row.forecast_id for row in world.resolutions}


# ---------------------------------------------------------------------------
# Total return survived the scripted world
# ---------------------------------------------------------------------------


def test_the_scripted_split_did_not_look_like_a_fifty_percent_crash(world: World) -> None:
    """The defect the whole resolution module exists to prevent. Alpha's raw
    quote genuinely halves at session 18, so a comparison of two raw closes
    would score every arm's forecast of it as catastrophically wrong."""
    split_at = str(world.cutoffs[SPLIT_SESSION - 1])
    spanning = [
        row
        for row in _panel_rows(world)
        if row.instrument_id == world.alpha_id
        and row.status == str(ResolutionStatus.RESOLVED)
        and row.anchor_at < split_at <= row.target_at
    ]
    assert spanning, "no forecast spanned the split, so this check proved nothing"
    assert all(Decimal(row.split_factor) == 2 for row in spanning)
    assert all(Decimal(row.total_return) > Decimal("-0.4") for row in spanning)


def test_the_scripted_dividend_counted_towards_the_return(world: World) -> None:
    ex_date = str(world.cutoffs[DIVIDEND_SESSION - 1])
    spanning = [
        row
        for row in _panel_rows(world)
        if row.instrument_id == world.alpha_id
        and row.status == str(ResolutionStatus.RESOLVED)
        and row.anchor_at < ex_date <= row.target_at
    ]
    assert spanning, "no forecast spanned the dividend"
    assert all(Decimal(row.dividends) > 0 for row in spanning)


def test_every_arm_was_told_the_same_thing_about_what_happened(world: World) -> None:
    """The realised direction is a property of the world. Six conditions
    disagreeing about it would mean resolution read the condition, not the
    market."""
    outcomes: dict[tuple[str, str, int], set[bool | None]] = {}
    for row in _panel_rows(world):
        key = (row.anchor_at, row.instrument_id, row.horizon_sessions)
        outcomes.setdefault(key, set()).add(row.outcome_up)
    assert all(len(values) == 1 for values in outcomes.values())


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------


def test_pairing_keeps_the_whole_panel(world: World) -> None:
    sample = pair_scores(list(world.resolutions), arms=(str(ArmId.A), str(ArmId.C)))
    assert sample.items
    assert sample.completeness == 1.0


def test_the_free_decision_forecasts_are_resolved_but_excluded_from_pairing(
    world: World,
) -> None:
    """Resolved for per-arm calibration; excluded from the comparison, because
    two arms that chose different instruments produce numbers that do not mean
    the same thing."""
    decision_rows = [row for row in world.resolutions if row.source == str(ForecastSource.DECISION)]
    assert decision_rows, "the arms made no free forecasts, so this proves nothing"

    # Handed nothing but free forecasts, the default pairing finds no cell at
    # all — the exclusion is in the pairing rule, not in what happened to be
    # passed to it.
    assert pair_scores(decision_rows, arms=(str(ArmId.A), str(ArmId.C))).items == ()


def test_conditions_that_decided_identically_come_out_equivalent(world: World) -> None:
    """The end-to-end statement, and the one worth reading carefully.

    The shipped fake ignores its injected context, so all six conditions
    answer the panel identically and every paired difference is exactly zero.
    A correct pipeline must therefore reach EQUIVALENT — and reaching it at
    all is what §21.7 requires of the analysis: "no practically useful effect"
    has to be a conclusion the study can arrive at, not merely a failure to
    reject.

    It says nothing whatever about memory or reflection. See
    ``docs/ROADMAP.md``: no arm comparison run against the fake does.
    """
    report = AnalysisPlan(
        rope=Rope(-0.01, 0.01), horizons=(1, 5), resamples=400, block_length=2
    ).run(list(world.resolutions))

    assert len(report.comparisons) == len(PRIMARY_CONTRASTS) * 2
    for comparison in report.comparisons:
        assert comparison.equivalence.estimate == 0.0, comparison.label
        assert comparison.equivalence.verdict is EquivalenceVerdict.EQUIVALENT, comparison.label


def test_the_analysis_reports_how_much_data_each_comparison_had(world: World) -> None:
    report = AnalysisPlan(
        rope=Rope(-0.01, 0.01), horizons=(1, 5), resamples=200, block_length=2
    ).run(list(world.resolutions))
    for comparison in report.comparisons:
        assert comparison.dates > 1
        assert comparison.items >= comparison.dates
        assert comparison.completeness == 1.0


def test_the_multiplicity_family_is_the_whole_set_of_comparisons(world: World) -> None:
    report = AnalysisPlan(
        rope=Rope(-0.01, 0.01), horizons=(1, 5), resamples=200, block_length=2
    ).run(list(world.resolutions))
    assert report.family_size == len(report.comparisons)
    assert {entry.label for entry in report.adjusted} == {c.label for c in report.comparisons}
