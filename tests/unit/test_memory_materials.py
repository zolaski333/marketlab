"""Tests for episodic memory, reflection and matched placebos (§13, §18, §19)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SqlIntegrityError
from sqlalchemy.orm import Session

from marketlab.agents.decision import DecisionOutcome, Forecast, TradeIntent
from marketlab.core.clock import FrozenClock
from marketlab.core.failures import AgentFailureKind, ObservedAgentFailure
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.core.money import Money
from marketlab.experiments.arms import ArmId, spec_for
from marketlab.experiments.materials import GrantedMaterialsProvider, MemoryRecorder
from marketlab.memory.rendering import MEMORY_HEADING, REFLECTION_HEADING, render_memory
from marketlab.memory.store import MemoryStore, memory_scope_id
from marketlab.models.types import TradeSide
from marketlab.reflection.engine import ReflectionEngine, derive_rules
from marketlab.storage.blobs import BlobStore
from marketlab.storage.database import Database

RUN_ID = "MEMORY_RUN"
ALPHA = "a" * 64
BETA = "b" * 64


def at(day: int) -> Instant:
    return instant_from_datetime(datetime(2026, 8, day, 20, 0, tzinfo=UTC))


@dataclass
class Rig:
    memory: MemoryStore
    reflection: ReflectionEngine
    recorder: MemoryRecorder
    provider: GrantedMaterialsProvider
    session: Session


@pytest.fixture
def rig(session: Session, clock: FrozenClock, blob_store: BlobStore) -> Rig:
    memory = MemoryStore(session, clock, blob_store)
    reflection = ReflectionEngine(session, clock, blob_store)
    return Rig(
        memory=memory,
        reflection=reflection,
        recorder=MemoryRecorder(run_id=RUN_ID, memory=memory, reflection=reflection),
        provider=GrantedMaterialsProvider(run_id=RUN_ID, memory=memory, reflection=reflection),
        session=session,
    )


def _outcome(
    *,
    probability: float = 0.6,
    side: TradeSide = TradeSide.BUY,
    instrument: str = ALPHA,
    failures: tuple[ObservedAgentFailure, ...] = (),
) -> DecisionOutcome:
    return DecisionOutcome(
        snapshot_id="snap",
        forecasts=(Forecast(instrument, 5, probability, ("ev-1",)),),
        trade_intents=(TradeIntent(instrument, side, "because", ("ev-1",)),),
        failures=failures,
        tool_calls_made=3,
        model_turns=4,
    )


def _remember(
    rig: Rig,
    day: int,
    *,
    arm: ArmId = ArmId.B,
    repetition: int = 0,
    probability: float = 0.6,
    side: TradeSide = TradeSide.BUY,
    instrument: str = ALPHA,
    failures: tuple[ObservedAgentFailure, ...] = (),
) -> None:
    rig.recorder.record(
        arm_id=arm,
        repetition=repetition,
        cycle_id=f"cycle-{day}",
        bundle_id=f"bundle-{arm}-{repetition}-{day}",
        as_of=at(day),
        outcome=_outcome(
            probability=probability, side=side, instrument=instrument, failures=failures
        ),
        equity=Money(Decimal("100000.00"), "USD"),
    )


def _scope(arm: ArmId = ArmId.B, repetition: int = 0) -> str:
    return memory_scope_id(RUN_ID, str(arm), repetition)


# ---------------------------------------------------------------------------
# Recall and its cutoff
# ---------------------------------------------------------------------------


def test_a_recorded_episode_can_be_recalled_later(rig: Rig) -> None:
    _remember(rig, 3)
    episodes = rig.memory.recall(_scope(), before=at(4))
    assert len(episodes) == 1
    assert episodes[0].forecasts[0].probability_up == 0.6


def test_recall_is_strictly_before_the_cutoff(rig: Rig) -> None:
    """The failure this prevents is reachable, not theoretical: a cycle
    interrupted after its episode was written and then resumed would recall its
    own decision as prior history and be asked to decide again with the answer
    in hand."""
    _remember(rig, 3)
    assert rig.memory.recall(_scope(), before=at(3)) == ()
    assert len(rig.memory.recall(_scope(), before=at(4))) == 1


def test_recall_returns_a_chronology_oldest_first(rig: Rig) -> None:
    for day in (3, 4, 5):
        _remember(rig, day)
    episodes = rig.memory.recall(_scope(), before=at(6))
    assert [str(episode.as_of) for episode in episodes] == [str(at(3)), str(at(4)), str(at(5))]


def test_recall_keeps_the_most_recent_episodes_when_limited(rig: Rig) -> None:
    for day in (3, 4, 5, 6):
        _remember(rig, day)
    episodes = rig.memory.recall(_scope(), before=at(7), limit=2)
    assert [str(episode.as_of) for episode in episodes] == [str(at(5)), str(at(6))]


def test_recording_the_same_decision_twice_remembers_it_once(rig: Rig) -> None:
    _remember(rig, 3)
    _remember(rig, 3)
    assert len(rig.memory.recall(_scope(), before=at(4))) == 1


def test_memory_episodes_are_append_only(rig: Rig, session: Session, database: Database) -> None:
    _remember(rig, 3)
    session.commit()
    with pytest.raises(SqlIntegrityError, match="append-only"), database.engine.begin() as conn:
        conn.execute(text("UPDATE memory_episodes SET as_of = '2020-01-01T00:00:00.000000Z'"))


# ---------------------------------------------------------------------------
# Isolation between conditions and repetitions
# ---------------------------------------------------------------------------


def test_one_condition_cannot_recall_anothers_history(rig: Rig) -> None:
    _remember(rig, 3, arm=ArmId.B)
    assert rig.memory.recall(_scope(ArmId.C), before=at(4)) == ()
    assert len(rig.memory.recall(_scope(ArmId.B), before=at(4))) == 1


def test_two_repetitions_of_one_arm_accumulate_separate_histories(rig: Rig) -> None:
    """§30.3: sharing one history would let the second replicate start with the
    first's experience, collapsing the within-condition variance the design
    needs to measure."""
    _remember(rig, 3, arm=ArmId.B, repetition=0)
    _remember(rig, 3, arm=ArmId.B, repetition=1)
    _remember(rig, 4, arm=ArmId.B, repetition=0)

    assert len(rig.memory.recall(_scope(ArmId.B, 0), before=at(5))) == 2
    assert len(rig.memory.recall(_scope(ArmId.B, 1), before=at(5))) == 1


# ---------------------------------------------------------------------------
# Reflection
# ---------------------------------------------------------------------------


def test_too_little_history_produces_no_reflection(rig: Rig) -> None:
    """An empty reflection would still be *material*; handing a condition a
    page saying nothing would confound reflection with more text."""
    _remember(rig, 3)
    episodes = rig.memory.recall(_scope(), before=at(4))
    assert rig.reflection.reflect(_scope(), episodes, as_of=at(4)) is None


def test_a_persistent_side_is_named(rig: Rig) -> None:
    for day in (3, 4, 5, 6):
        _remember(rig, day, side=TradeSide.BUY)
    reflection = rig.reflection.reflect(
        _scope(), rig.memory.recall(_scope(), before=at(7)), as_of=at(7)
    )
    assert reflection is not None
    assert any("BUY" in rule.statement and ALPHA in rule.statement for rule in reflection.rules)


def test_constant_probabilities_are_named_as_uninformative(rig: Rig) -> None:
    for day in (3, 4, 5, 6):
        _remember(rig, day, probability=0.5)
    rules = derive_rules(rig.memory.recall(_scope(), before=at(7)))
    assert any("never moves" in rule.statement for rule in rules)


def test_repeated_failures_are_named(rig: Rig) -> None:
    failure = (
        ObservedAgentFailure(kind=AgentFailureKind.MALFORMED_JSON, detail="bad", occurred_at=at(3)),
    )
    for day in (3, 4, 5):
        _remember(rig, day, failures=failure)
    rules = derive_rules(rig.memory.recall(_scope(), before=at(6)))
    assert any("MALFORMED_JSON" in rule.statement for rule in rules)


def test_rules_say_nothing_about_whether_forecasts_came_true(rig: Rig) -> None:
    """Forecast resolution is task 12. A rule claiming a hit rate today would
    be fabricated - the exact failure this project was rebuilt to avoid."""
    for day in (3, 4, 5, 6):
        _remember(rig, day)
    rules = derive_rules(rig.memory.recall(_scope(), before=at(7)))
    forbidden = ("correct", "accura", "hit rate", "right", "wrong", "brier")
    for rule in rules:
        assert not any(word in rule.statement.lower() for word in forbidden), rule.statement


def test_reflection_is_deterministic(rig: Rig) -> None:
    for day in (3, 4, 5, 6):
        _remember(rig, day)
    episodes = rig.memory.recall(_scope(), before=at(7))
    assert derive_rules(episodes) == derive_rules(episodes)


def test_a_reflection_is_visible_only_after_the_cycle_that_produced_it(rig: Rig) -> None:
    for day in (3, 4, 5, 6):
        _remember(rig, day)
    rig.reflection.reflect(_scope(), rig.memory.recall(_scope(), before=at(7)), as_of=at(7))
    assert rig.reflection.latest(_scope(), before=at(7)) is None
    assert rig.reflection.latest(_scope(), before=at(8)) is not None


def test_reflection_runs_only_on_its_cadence(rig: Rig) -> None:
    for day in (3, 4, 5, 6, 7):
        _remember(rig, day)
    recorder = MemoryRecorder(
        run_id=RUN_ID, memory=rig.memory, reflection=rig.reflection, reflection_interval=5
    )
    produced = [
        recorder.maybe_reflect(arm_id=ArmId.B, repetition=0, as_of=at(8), cycle_index=index)
        is not None
        for index in range(10)
    ]
    assert produced.count(True) == 2  # cycle_index 4 and 9


# ---------------------------------------------------------------------------
# What each arm actually receives
# ---------------------------------------------------------------------------


def _materials(rig: Rig, arm: ArmId, *, day: int = 8, repetition: int = 0) -> str | None:
    return rig.provider.materials_for(
        spec_for(arm), cycle_id="cycle", as_of=at(day), repetition=repetition
    )


def _seed_all_arms(rig: Rig) -> None:
    for arm in ArmId:
        for day in (3, 4, 5, 6):
            _remember(rig, day, arm=arm)
        rig.reflection.reflect(
            _scope(arm), rig.memory.recall(_scope(arm), before=at(7)), as_of=at(7)
        )


def test_the_control_arm_is_granted_nothing_even_with_a_history(rig: Rig) -> None:
    _seed_all_arms(rig)
    assert _materials(rig, ArmId.A) is None


def test_memory_only_arms_get_memory_and_no_strategy_notes(rig: Rig) -> None:
    _seed_all_arms(rig)
    material = _materials(rig, ArmId.B)
    assert material is not None
    assert MEMORY_HEADING in material
    assert REFLECTION_HEADING not in material


def test_the_reflection_only_arm_gets_notes_and_no_history(rig: Rig) -> None:
    """Arm D is the cell that separates being told what works from being able
    to look up what happened."""
    _seed_all_arms(rig)
    material = _materials(rig, ArmId.D)
    assert material is not None
    assert REFLECTION_HEADING in material
    assert MEMORY_HEADING not in material


def test_the_full_arm_gets_both(rig: Rig) -> None:
    _seed_all_arms(rig)
    material = _materials(rig, ArmId.C)
    assert material is not None
    assert MEMORY_HEADING in material
    assert REFLECTION_HEADING in material


def test_an_arm_with_no_history_yet_is_granted_nothing(rig: Rig) -> None:
    """First cycle of a run: memory exists as a mechanism but has nothing in
    it, and inventing filler would make arm B differ from A before it could
    possibly have learned anything."""
    assert _materials(rig, ArmId.B, day=3) is None


# ---------------------------------------------------------------------------
# The placebos
# ---------------------------------------------------------------------------


def test_a_placebo_arm_receives_material_of_matched_shape(rig: Rig) -> None:
    _seed_all_arms(rig)
    genuine = _materials(rig, ArmId.B)
    placebo = _materials(rig, ArmId.B_PRIME)
    assert genuine is not None
    assert placebo is not None
    assert MEMORY_HEADING in placebo
    assert placebo.count("\n") == genuine.count("\n")


def test_a_placebo_is_close_in_length_to_the_genuine_article(rig: Rig) -> None:
    """A placebo that is much shorter or longer leaves the comparison
    confounded by volume of text - the very thing it exists to rule out."""
    _seed_all_arms(rig)
    genuine = _materials(rig, ArmId.B)
    placebo = _materials(rig, ArmId.B_PRIME)
    assert genuine is not None and placebo is not None
    assert abs(len(placebo) - len(genuine)) / len(genuine) < 0.02


def test_a_placebo_contains_no_instrument_the_condition_actually_traded(rig: Rig) -> None:
    _seed_all_arms(rig)
    placebo = _materials(rig, ArmId.B_PRIME)
    assert placebo is not None
    assert ALPHA not in placebo


def test_a_placebo_contains_no_probability_the_condition_actually_stated(rig: Rig) -> None:
    _seed_all_arms(rig)
    placebo = _materials(rig, ArmId.B_PRIME)
    assert placebo is not None
    assert "0.6000" not in placebo


def test_the_placebo_reflection_carries_no_claim_about_its_own_record(rig: Rig) -> None:
    _seed_all_arms(rig)
    genuine = _materials(rig, ArmId.C)
    placebo = _materials(rig, ArmId.C_PRIME)
    assert genuine is not None and placebo is not None
    assert REFLECTION_HEADING in placebo
    assert ALPHA not in placebo


def test_a_placebo_differs_from_its_genuine_counterpart(rig: Rig) -> None:
    """The whole point: same shape, different content. If these ever matched,
    B versus B' would be measuring nothing."""
    _seed_all_arms(rig)
    assert _materials(rig, ArmId.B) != _materials(rig, ArmId.B_PRIME)
    assert _materials(rig, ArmId.C) != _materials(rig, ArmId.C_PRIME)


def test_a_placebo_is_reproducible(rig: Rig) -> None:
    """§12.5: a replay must reconstruct what the condition was actually shown."""
    _seed_all_arms(rig)
    assert _materials(rig, ArmId.B_PRIME) == _materials(rig, ArmId.B_PRIME)


def test_two_placebo_arms_do_not_receive_identical_text(rig: Rig) -> None:
    _seed_all_arms(rig)
    assert _materials(rig, ArmId.B_PRIME) != _materials(rig, ArmId.C_PRIME)


def test_a_placebo_arm_with_no_history_is_granted_nothing(rig: Rig) -> None:
    assert _materials(rig, ArmId.B_PRIME, day=3) is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_rendered_memory_frames_itself_as_context_not_instruction(rig: Rig) -> None:
    """§11.2 again: injected text is injected text, whatever produced it."""
    _remember(rig, 3)
    rendered = render_memory(rig.memory.recall(_scope(), before=at(4)))
    assert "not instructions" in rendered
