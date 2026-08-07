"""End-to-end wiring test for task 9: running all six conditions against the
frozen synthetic world, session after session.

Like its predecessors (``test_synthetic_universe_wiring.py``,
``test_snapshot_and_retrieval_wiring.py``), this is deliberately not a unit
test of any one component. It exists to catch the mistakes hand-picked
fixtures cannot: an arm that quietly reads a different snapshot than its
neighbours, an ordering policy that stops balancing once real cycle indices
flow through it, a bundle identity that starts colliding across sessions, or a
resume path that behaves differently on a database holding a run's worth of
history rather than one cycle's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketlab.core.clock import FrozenClock
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.experiments.arms import ARMS, ArmId, ArmSpec
from marketlab.experiments.context import NullMaterialsProvider
from marketlab.experiments.runner import CycleResult, CycleRunner, DecisionBundleRow, RunConfig
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.synthetic import SyntheticMarketDataProvider, admit_synthetic_universe
from marketlab.instruments.calendars import CalendarRegistry
from marketlab.instruments.repository import InstrumentRepository
from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

START_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
NUM_SESSIONS = 12
RUN_ID = "MULTI_ARM_WIRING_RUN"
ARM_COUNT = len(ARMS)


@dataclass
class World:
    session: Session
    events: EventStore
    builder: SnapshotBuilder
    blobs: BlobStore
    clock: FrozenClock
    snapshot_ids: tuple[str, ...]
    cutoffs: tuple[Instant, ...]


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

    candidates: list[SnapshotCandidate] = []
    snapshot_ids: list[str] = []
    cutoffs: list[Instant] = []
    for cutoff in provider.session_cutoffs():
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
        snapshot_ids.append(builder.build(candidates, as_of=cutoff, run_id=RUN_ID).snapshot_id)
        cutoffs.append(cutoff)

    return World(
        session=session,
        events=events,
        builder=builder,
        blobs=blob_store,
        clock=clock,
        snapshot_ids=tuple(snapshot_ids),
        cutoffs=tuple(cutoffs),
    )


def _runner(world: World, materials: object | None = None) -> CycleRunner:
    return CycleRunner(
        session=world.session,
        clock=world.clock,
        blobs=world.blobs,
        events=world.events,
        builder=world.builder,
        model_factory=DeterministicPolicyModel,
        materials=materials or NullMaterialsProvider(),  # type: ignore[arg-type]
        config=RunConfig(run_id=RUN_ID),
    )


def _run_all(world: World, runner: CycleRunner) -> list[CycleResult]:
    return [
        runner.run_cycle(cycle_index=index, snapshot_id=snapshot_id, as_of=world.cutoffs[index])
        for index, snapshot_id in enumerate(world.snapshot_ids)
    ]


@pytest.fixture
def cycles(world: World) -> list[CycleResult]:
    return _run_all(world, _runner(world))


# ---------------------------------------------------------------------------
# Every condition, every session
# ---------------------------------------------------------------------------


def test_every_session_runs_every_condition(cycles: list[CycleResult]) -> None:
    assert len(cycles) == NUM_SESSIONS
    for cycle in cycles:
        assert len(cycle.executions) == ARM_COUNT
        assert cycle.missing == ()
        assert {e.arm_id for e in cycle.executions} == set(ARMS)


def test_every_bundle_in_the_whole_run_is_uniquely_identified(
    world: World, cycles: list[CycleResult]
) -> None:
    bundle_ids = [e.bundle_id for cycle in cycles for e in cycle.executions]
    assert len(set(bundle_ids)) == NUM_SESSIONS * ARM_COUNT

    persisted = world.session.execute(
        select(func.count()).select_from(DecisionBundleRow)
    ).scalar_one()
    assert int(persisted) == NUM_SESSIONS * ARM_COUNT


def test_all_conditions_of_one_cycle_read_the_same_snapshot(cycles: list[CycleResult]) -> None:
    """§9.1: a difference between arms cannot be a difference in evidence."""
    for cycle in cycles:
        assert {e.outcome.snapshot_id for e in cycle.executions} == {cycle.snapshot_id}


def test_conditions_that_are_granted_nothing_decide_identically(
    cycles: list[CycleResult],
) -> None:
    """The A/A property. Under the null materials provider every arm is handed
    the same (empty) context against the same snapshot, so any divergence
    would be a leak rather than an effect — including one arriving through
    execution order, which does vary between them."""
    for cycle in cycles:
        assert len({e.content_hash for e in cycle.executions}) == 1


def test_the_decision_still_moves_as_the_world_moves(cycles: list[CycleResult]) -> None:
    """Guards the previous test against passing for the wrong reason: if every
    decision were constant, arms would trivially agree."""
    per_cycle = [cycle.executions[0].content_hash for cycle in cycles]
    assert len(set(per_cycle)) > 1


def test_no_condition_is_ever_recorded_as_missing_on_a_healthy_run(
    world: World, cycles: list[CycleResult]
) -> None:
    missing = list(world.events.iter_events(event_type="CONDITION_MISSING"))
    assert missing == []


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


def test_execution_order_is_exactly_balanced_over_one_full_rotation(
    cycles: list[CycleResult],
) -> None:
    """Six conditions, six cycles: each arm should occupy each position once."""
    positions: dict[ArmId, list[int]] = {arm_id: [] for arm_id in ARMS}
    for cycle in cycles[:ARM_COUNT]:
        for execution in cycle.executions:
            positions[execution.arm_id].append(execution.position)
    for arm_id, seen in positions.items():
        assert sorted(seen) == list(range(ARM_COUNT)), arm_id


def test_position_is_persisted_so_an_order_effect_can_be_tested_for(
    world: World, cycles: list[CycleResult]
) -> None:
    for execution in cycles[3].executions:
        row = world.session.get(DecisionBundleRow, execution.bundle_id)
        assert row is not None
        assert row.position == execution.position


# ---------------------------------------------------------------------------
# The material channel, end to end
# ---------------------------------------------------------------------------


class _PerArmMaterials:
    """Stands in for task 11's memory/reflection providers: distinguishable
    text per arm, so the plumbing between an arm's grants and the model can be
    checked before the real subsystems exist."""

    def materials_for(
        self, arm: ArmSpec, *, cycle_id: str, as_of: Instant, repetition: int
    ) -> str | None:
        if not arm.grants_anything:
            return None
        return f"[{arm.memory}/{arm.reflection}] material for cycle {cycle_id}"


def test_differentiating_material_reaches_every_granted_arm(world: World) -> None:
    cycle = _runner(world, _PerArmMaterials()).run_cycle(
        cycle_index=0, snapshot_id=world.snapshot_ids[0], as_of=world.cutoffs[0]
    )
    granted = [e for e in cycle.executions if e.context_blob_hash is not None]
    ungranted = [e for e in cycle.executions if e.context_blob_hash is None]

    assert {e.arm_id for e in ungranted} == {ArmId.A}
    assert len(granted) == ARM_COUNT - 1
    for execution in granted:
        spec = ARMS[execution.arm_id]
        assert execution.context_blob_hash is not None
        stored = world.blobs.get(execution.context_blob_hash).decode("utf-8")
        assert stored.startswith(f"[{spec.memory}/{spec.reflection}]")


def test_a_placebo_arm_is_handed_different_material_than_its_counterpart(world: World) -> None:
    """The property a real placebo generator has to satisfy, checked here on
    the plumbing that carries it. If B' were ever handed byte-identical
    material to B, content addressing would collapse them onto one hash —
    which is the failure this assertion is written to catch once task 11
    supplies the real generators."""
    cycle = _runner(world, _PerArmMaterials()).run_cycle(
        cycle_index=0, snapshot_id=world.snapshot_ids[0], as_of=world.cutoffs[0]
    )
    hashes = {
        e.arm_id: e.context_blob_hash for e in cycle.executions if e.context_blob_hash is not None
    }
    for placebo_id in (ArmId.B_PRIME, ArmId.C_PRIME):
        counterpart = ARMS[placebo_id].placebo_of
        assert counterpart is not None
        assert hashes[placebo_id] != hashes[counterpart]
    # ...and two genuine arms granted different channels differ too, so the
    # assertion above is not passing merely because every hash is distinct by
    # accident of the cycle id being in the text.
    assert hashes[ArmId.B] != hashes[ArmId.C]


# ---------------------------------------------------------------------------
# Audit trail and resume
# ---------------------------------------------------------------------------


def test_the_event_chain_survives_a_full_multi_arm_run(
    world: World, cycles: list[CycleResult]
) -> None:
    assert world.events.verify_chain() > NUM_SESSIONS * ARM_COUNT


def test_every_sealed_decision_has_an_event_carrying_its_routing_keys(
    world: World, cycles: list[CycleResult]
) -> None:
    sealed = list(world.events.iter_events(event_type="DECISION_SEALED"))
    assert len(sealed) == NUM_SESSIONS * ARM_COUNT
    expected = {e.bundle_id for cycle in cycles for e in cycle.executions}
    assert {record.payload["bundle_id"] for record in sealed} == expected


def test_replaying_the_whole_run_reproduces_it_without_re_deciding(
    world: World, cycles: list[CycleResult]
) -> None:
    """§30.6: an interrupted run resumes to the same state, and §12.5's replay
    starts from the property that identities are reproducible rather than
    freshly minted."""
    before = int(
        world.session.execute(select(func.count()).select_from(DecisionBundleRow)).scalar_one()
    )
    again = _run_all(world, _runner(world))
    after = int(
        world.session.execute(select(func.count()).select_from(DecisionBundleRow)).scalar_one()
    )

    assert after == before
    for original, repeated in zip(cycles, again, strict=True):
        assert repeated.cycle_id == original.cycle_id
        assert {e.bundle_id for e in repeated.executions} == {
            e.bundle_id for e in original.executions
        }
        assert {e.content_hash for e in repeated.executions} == {
            e.content_hash for e in original.executions
        }
