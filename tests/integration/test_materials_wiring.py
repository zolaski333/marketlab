"""End-to-end wiring test for task 11: six conditions that are finally
distinguishable, over the synthetic world.

Everything before this task built a platform that *could* measure a difference
between arms while every arm received nothing. This checks the difference now
exists and lands where it should: arm A still granted nothing, B and C
accumulating their own histories, D handed distilled notes without the
episodes behind them, and B'/C' handed matched placebos.

One property here needs stating rather than discovering
--------------------------------------------------------
``DeterministicPolicyModel`` is a closed-form function of the closing price and
**ignores** ``injected_context`` entirely. So the arms are shown different
things and still decide identically. That is correct and deliberate: a fake
that branched on its injected context would manufacture a memory effect out of
nothing, which is precisely the defect the original audit found in this
project's predecessor ("mock LLM branching on condition_id"). Demonstrating
that granted material *changes decisions* needs a real model, and is Phase 3.

What can be shown now is that the channel is live: a test model that does read
its context produces different decisions per arm through the same pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from marketlab.agents.decision import DecisionAgent
from marketlab.core.clock import FrozenClock
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.experiments.arms import ArmId
from marketlab.experiments.materials import GrantedMaterialsProvider, MemoryRecorder
from marketlab.experiments.runner import CycleResult, CycleRunner, RunConfig
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.synthetic import SyntheticMarketDataProvider, admit_synthetic_universe
from marketlab.instruments.calendars import CalendarRegistry
from marketlab.instruments.repository import InstrumentRepository
from marketlab.memory.rendering import MEMORY_HEADING, REFLECTION_HEADING
from marketlab.memory.store import MemoryStore, memory_scope_id
from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.models.types import (
    ModelRequest,
    ModelResponse,
    RawDecision,
    RawForecast,
    ToolCallRequest,
)
from marketlab.reflection.engine import ReflectionEngine
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

START_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
NUM_SESSIONS = 12
RUN_ID = "MATERIALS_WIRING_RUN"
REFLECTION_INTERVAL = 4


@dataclass
class World:
    session: Session
    blobs: BlobStore
    memory: MemoryStore
    reflection: ReflectionEngine
    cycles: tuple[CycleResult, ...]
    cutoffs: tuple[Instant, ...]


def _build_world(
    session: Session,
    clock: FrozenClock,
    blob_store: BlobStore,
    *,
    model_factory: object = DeterministicPolicyModel,
) -> World:
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
    memory = MemoryStore(session, clock, blob_store)
    reflection = ReflectionEngine(session, clock, blob_store)
    materials = GrantedMaterialsProvider(run_id=RUN_ID, memory=memory, reflection=reflection)
    recorder = MemoryRecorder(
        run_id=RUN_ID,
        memory=memory,
        reflection=reflection,
        reflection_interval=REFLECTION_INTERVAL,
    )
    runner = CycleRunner(
        session=session,
        clock=clock,
        blobs=blob_store,
        events=events,
        builder=builder,
        model_factory=model_factory,  # type: ignore[arg-type]
        materials=materials,
        config=RunConfig(run_id=RUN_ID),
        agent=DecisionAgent(),
    )

    candidates: list[SnapshotCandidate] = []
    cycles: list[CycleResult] = []
    cutoffs: list[Instant] = []
    for index_number, cutoff in enumerate(provider.session_cutoffs()):
        for bar in provider.fetch_price_bars(cutoff):
            candidates.append(SnapshotCandidate("PRICE_BAR", pipeline.ingest_price_bar(bar)))
        for item in provider.fetch_news(cutoff):
            candidates.append(SnapshotCandidate("NEWS_ITEM", pipeline.ingest_news_item(item)))
        manifest = builder.build(candidates, as_of=cutoff, run_id=RUN_ID)

        cycle = runner.run_cycle(
            cycle_index=index_number, snapshot_id=manifest.snapshot_id, as_of=cutoff
        )
        for execution in cycle.executions:
            recorder.record(
                arm_id=execution.arm_id,
                repetition=execution.repetition,
                cycle_id=cycle.cycle_id,
                bundle_id=execution.bundle_id,
                as_of=cutoff,
                outcome=execution.outcome,
            )
            recorder.maybe_reflect(
                arm_id=execution.arm_id,
                repetition=execution.repetition,
                as_of=cutoff,
                cycle_index=index_number,
            )
        cycles.append(cycle)
        cutoffs.append(cutoff)

    return World(
        session=session,
        blobs=blob_store,
        memory=memory,
        reflection=reflection,
        cycles=tuple(cycles),
        cutoffs=tuple(cutoffs),
    )


@pytest.fixture
def world(session: Session, clock: FrozenClock, blob_store: BlobStore) -> World:
    return _build_world(session, clock, blob_store)


def _context(world: World, cycle_index: int, arm: ArmId) -> str | None:
    execution = world.cycles[cycle_index].execution_for(arm)
    assert execution is not None
    if execution.context_blob_hash is None:
        return None
    return world.blobs.get(execution.context_blob_hash).decode("utf-8")


# ---------------------------------------------------------------------------
# The arms are finally distinguishable
# ---------------------------------------------------------------------------


def test_the_first_cycle_grants_nothing_to_anyone(world: World) -> None:
    """Before any history exists, inventing filler would make arm B differ
    from A before it could possibly have learned anything."""
    for arm in ArmId:
        assert _context(world, 0, arm) is None


def test_by_the_end_of_the_run_only_the_control_arm_is_granted_nothing(world: World) -> None:
    last = len(world.cycles) - 1
    assert _context(world, last, ArmId.A) is None
    for arm in (ArmId.B, ArmId.C, ArmId.D, ArmId.B_PRIME, ArmId.C_PRIME):
        assert _context(world, last, arm), arm


def test_each_arm_receives_exactly_the_channels_its_condition_grants(world: World) -> None:
    last = len(world.cycles) - 1
    expected = {
        ArmId.B: (True, False),
        ArmId.C: (True, True),
        ArmId.D: (False, True),
        ArmId.B_PRIME: (True, False),
        ArmId.C_PRIME: (True, True),
    }
    for arm, (wants_memory, wants_reflection) in expected.items():
        material = _context(world, last, arm)
        assert material is not None
        assert (MEMORY_HEADING in material) is wants_memory, arm
        assert (REFLECTION_HEADING in material) is wants_reflection, arm


def test_a_placebo_arm_is_handed_something_matched_but_different(world: World) -> None:
    last = len(world.cycles) - 1
    genuine = _context(world, last, ArmId.B)
    placebo = _context(world, last, ArmId.B_PRIME)
    assert genuine is not None and placebo is not None
    assert genuine != placebo
    assert abs(len(placebo) - len(genuine)) / len(genuine) < 0.02


def test_memory_grows_with_the_run(world: World) -> None:
    scope = memory_scope_id(RUN_ID, str(ArmId.B), 0)
    early = world.memory.episode_count(scope, before=world.cutoffs[2])
    late = world.memory.episode_count(scope, before=world.cutoffs[-1])
    assert 0 < early < late


def test_each_condition_accumulates_its_own_history(world: World) -> None:
    counts = {
        arm: world.memory.episode_count(
            memory_scope_id(RUN_ID, str(arm), 0), before=world.cutoffs[-1]
        )
        for arm in ArmId
    }
    assert all(count == len(world.cycles) - 1 for count in counts.values())


def test_reflections_are_produced_on_their_cadence_for_every_arm(world: World) -> None:
    """Produced for everyone and *withheld* from arms that are not granted it,
    so the difference between conditions is what they are shown rather than how
    much work was done on their behalf."""
    for arm in ArmId:
        scope = memory_scope_id(RUN_ID, str(arm), 0)
        assert world.reflection.latest(scope, before=world.cutoffs[-1]) is not None


# ---------------------------------------------------------------------------
# What that does, and does not, do to the decisions
# ---------------------------------------------------------------------------


def test_the_deterministic_fake_still_decides_identically_across_arms(world: World) -> None:
    """Deliberate, and the most important thing on this page to read correctly.

    The shipped fake is a closed-form function of price and ignores its
    injected context, so different material produces identical decisions. A
    fake that branched on that context would manufacture a memory effect out
    of nothing - the exact defect found in this project's predecessor. This
    test exists so that behaviour is pinned rather than assumed.
    """
    last = len(world.cycles) - 1
    hashes = {execution.content_hash for execution in world.cycles[last].executions}
    assert len(hashes) == 1


def test_the_channel_is_live_when_a_model_actually_reads_it(
    session: Session, clock: FrozenClock, blob_store: BlobStore
) -> None:
    """The positive half: a model that does read its context produces
    different decisions per arm through exactly the same pipeline. Written as
    a test double rather than shipped, so the real fake stays honest."""

    class _ContextSensitiveModel:
        """Forecasts from the *length* of its granted context - a stand-in for
        a real model being influenced by what it was told."""

        @property
        def model_id(self) -> str:
            return "context-sensitive-test-model"

        def generate(self, request: ModelRequest) -> ModelResponse:
            universe = [
                result
                for result in request.tool_results
                if result.tool_name == "search_instruments"
            ]
            if not universe:
                return ModelResponse(
                    tool_calls=(ToolCallRequest("search_instruments", {"query": ""}),)
                )
            instrument_id = str((universe[0].result or [{}])[0]["instrument_id"])
            granted = len(request.injected_context or "")
            probability = 0.5 + min(granted, 4000) / 100_000
            return ModelResponse(
                decision=RawDecision(
                    forecasts=(RawForecast(instrument_id, 5, round(probability, 6), ()),),
                    trade_intents=(),
                    narrative="",
                )
            )

    world = _build_world(session, clock, blob_store, model_factory=_ContextSensitiveModel)
    last = len(world.cycles) - 1
    hashes = {execution.content_hash for execution in world.cycles[last].executions}
    assert len(hashes) > 1

    # And the control arm, granted nothing, is the one at the baseline.
    control = world.cycles[last].execution_for(ArmId.A)
    granted = world.cycles[last].execution_for(ArmId.C)
    assert control is not None and granted is not None
    assert control.content_hash != granted.content_hash
