"""Tests for the multi-arm cycle runner (§13, §23.4, §30.3, §30.6)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError as SqlIntegrityError
from sqlalchemy.orm import Session

from marketlab.agents.decision import DecisionOutcome, Forecast, TradeIntent
from marketlab.core.clock import FrozenClock
from marketlab.core.failures import (
    AgentFailureKind,
    ConfigurationError,
    ModelProviderError,
    ObservedAgentFailure,
)
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.experiments.arms import ArmId, ArmSpec
from marketlab.experiments.context import ConditionMaterialsProvider, NullMaterialsProvider
from marketlab.experiments.ordering import OrderPolicy
from marketlab.experiments.runner import (
    CycleResult,
    CycleRunner,
    DecisionBundleRow,
    RunConfig,
    decision_content_hash,
)
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.types import RawPriceBar
from marketlab.instruments.repository import InstrumentRepository
from marketlab.instruments.types import AssetClass, ExecutionModel
from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.models.types import (
    LanguageModel,
    ModelRequest,
    ModelResponse,
    RawDecision,
    ToolCallRequest,
    TradeSide,
)
from marketlab.retrieval.types import RetrievalIndex
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate
from marketlab.storage.blobs import BlobStore
from marketlab.storage.database import Database
from marketlab.storage.events import EventStore

RUN_ID = "TEST_RUN"
ADMITTED_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
SESSION_1 = instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC))

# The deterministic policy needs exactly three calls for a one-instrument
# universe: search_instruments, get_price_quote, search_news.
CALLS_PER_DECISION = 3


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------


@dataclass
class Rig:
    session: Session
    events: EventStore
    builder: SnapshotBuilder
    blobs: BlobStore
    clock: FrozenClock
    snapshot_id: str
    instrument_id: str


@pytest.fixture
def rig(session: Session, clock: FrozenClock, blob_store: BlobStore) -> Rig:
    repo = InstrumentRepository(session, clock)
    events = EventStore(session, clock)
    pipeline = IngestionPipeline(
        blob_store,
        events,
        session,
        clock,
        source_id="TEST",
        licence="internal-test",
        redistributable=True,
    )
    builder = SnapshotBuilder(session, clock, blob_store, repo, events)

    view = repo.admit(
        asset_class=AssetClass.EQUITY,
        ticker="ALPHA",
        name="Alpha Inc",
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="TEST_CAL",
        settlement_days=2,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        at=ADMITTED_AT,
    )
    price = Decimal("150.03")
    bar = RawPriceBar(
        instrument_id=view.instrument_id,
        as_of=SESSION_1,
        bid=price - Decimal("0.05"),
        ask=price + Decimal("0.05"),
        close=price,
        volume=1000,
        first_seen_at=SESSION_1,
    )
    manifest = builder.build(
        [SnapshotCandidate("PRICE_BAR", pipeline.ingest_price_bar(bar))],
        as_of=SESSION_1,
        run_id=RUN_ID,
    )
    return Rig(
        session=session,
        events=events,
        builder=builder,
        blobs=blob_store,
        clock=clock,
        snapshot_id=manifest.snapshot_id,
        instrument_id=view.instrument_id,
    )


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _CountingFactory:
    """Counts how many model instances a run actually creates."""

    def __init__(self) -> None:
        self.instances = 0

    def __call__(self) -> DeterministicPolicyModel:
        self.instances += 1
        return DeterministicPolicyModel()


class _RecordingModel:
    """Delegates to the real policy while logging every injected context."""

    def __init__(self, log: list[str | None]) -> None:
        self._log = log
        self._inner = DeterministicPolicyModel()

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    def generate(self, request: ModelRequest) -> ModelResponse:
        self._log.append(request.injected_context)
        return self._inner.generate(request)


class _FixedModel:
    """Always returns the same response, whatever it is asked."""

    def __init__(self, response: ModelResponse, model_id: str = "fixed-test-model") -> None:
        self._response = response
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    def generate(self, request: ModelRequest) -> ModelResponse:
        return self._response


class _UnreachableProviderModel:
    """Simulates a provider outage (§23.4) for a chosen arm's turn."""

    def __init__(self, fail_on_instance: int, counter: list[int]) -> None:
        self._fail_on_instance = fail_on_instance
        self._counter = counter
        counter[0] += 1
        self._instance = counter[0]

    @property
    def model_id(self) -> str:
        return "flaky-test-model"

    def generate(self, request: ModelRequest) -> ModelResponse:
        if self._instance == self._fail_on_instance:
            raise ModelProviderError("provider unreachable after retries")
        return DeterministicPolicyModel().generate(request)


class _PerArmMaterials:
    """A provider that grants each arm distinguishable material."""

    def materials_for(
        self, arm: ArmSpec, *, cycle_id: str, as_of: Instant, repetition: int
    ) -> str | None:
        if not arm.grants_anything:
            return None
        return f"material for {arm.arm_id} rep {repetition}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runner(
    rig: Rig,
    *,
    model_factory: Callable[[], LanguageModel] = DeterministicPolicyModel,
    materials: ConditionMaterialsProvider | None = None,
    config: RunConfig | None = None,
) -> CycleRunner:
    return CycleRunner(
        session=rig.session,
        clock=rig.clock,
        blobs=rig.blobs,
        events=rig.events,
        builder=rig.builder,
        model_factory=model_factory,
        materials=materials or NullMaterialsProvider(),
        config=config or RunConfig(run_id=RUN_ID),
    )


def _run(runner: CycleRunner, rig: Rig, *, cycle_index: int = 0) -> CycleResult:
    return runner.run_cycle(cycle_index=cycle_index, snapshot_id=rig.snapshot_id, as_of=SESSION_1)


def _event_types(rig: Rig, event_type: str) -> list[Any]:
    return [record.payload for record in rig.events.iter_events(event_type=event_type)]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_a_duplicated_arm_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="Duplicate arms"):
        RunConfig(run_id=RUN_ID, arms=(ArmId.A, ArmId.B, ArmId.A))


def test_zero_repetitions_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="repetitions"):
        RunConfig(run_id=RUN_ID, repetitions=0)


def test_an_empty_run_id_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="run_id"):
        RunConfig(run_id="   ")


def test_units_cross_every_arm_with_every_repetition() -> None:
    config = RunConfig(run_id=RUN_ID, arms=(ArmId.A, ArmId.B), repetitions=3)
    units = config.units()
    assert len(units) == 6
    assert {u.arm_id for u in units} == {ArmId.A, ArmId.B}
    assert sorted(u.repetition for u in units if u.arm_id is ArmId.A) == [0, 1, 2]


# ---------------------------------------------------------------------------
# One cycle, six conditions
# ---------------------------------------------------------------------------


def test_every_declared_arm_produces_exactly_one_bundle(rig: Rig) -> None:
    result = _run(_runner(rig), rig)
    assert len(result.executions) == 6
    assert result.missing == ()
    assert {e.arm_id for e in result.executions} == set(RunConfig(run_id=RUN_ID).arms)
    assert len({e.bundle_id for e in result.executions}) == 6


def test_repetitions_of_one_arm_get_distinct_bundle_ids(rig: Rig) -> None:
    """The exact defect ``derive_id``'s docstring records: a previous
    implementation derived the bundle id from (snapshot, condition) while
    omitting the repetition, collapsing every repetition of an arm onto one
    bundle."""
    config = RunConfig(run_id=RUN_ID, arms=(ArmId.A,), repetitions=3)
    result = _run(_runner(rig, config=config), rig)
    assert len({e.bundle_id for e in result.executions}) == 3
    assert sorted(e.repetition for e in result.executions) == [0, 1, 2]


def test_the_same_condition_in_two_different_cycles_gets_distinct_bundles(rig: Rig) -> None:
    runner = _runner(rig, config=RunConfig(run_id=RUN_ID, arms=(ArmId.A,)))
    first = _run(runner, rig, cycle_index=0)
    second = _run(runner, rig, cycle_index=1)
    assert first.cycle_id != second.cycle_id
    assert first.executions[0].bundle_id != second.executions[0].bundle_id


def test_the_snapshot_is_loaded_once_and_shared_by_every_arm(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not "rebuilt identically for each arm" — the same object, so no rebuild
    can drift between conditions (§9.1)."""
    calls: list[str] = []
    original = SnapshotBuilder.load_index

    def counting(self: SnapshotBuilder, snapshot_id: str) -> RetrievalIndex:
        calls.append(snapshot_id)
        return original(self, snapshot_id)

    monkeypatch.setattr(SnapshotBuilder, "load_index", counting)
    result = _run(_runner(rig), rig)

    # One load for the cycle. (`load_execution` does not touch the builder.)
    assert calls == [rig.snapshot_id]
    assert {e.outcome.snapshot_id for e in result.executions} == {rig.snapshot_id}


def test_each_unit_gets_its_own_model_instance(rig: Rig) -> None:
    """A reused instance could carry one condition's state into the next."""
    factory = _CountingFactory()
    _run(_runner(rig, model_factory=factory), rig)
    assert factory.instances == 6


def test_each_unit_gets_its_own_tool_budget(rig: Rig) -> None:
    """A budget shared across arms would leave the last arm nothing to spend,
    which is an allowance difference masquerading as a condition effect."""
    config = RunConfig(run_id=RUN_ID, max_tool_calls=CALLS_PER_DECISION)
    result = _run(_runner(rig, config=config), rig)
    assert all(e.outcome.failures == () for e in result.executions)
    assert {e.outcome.tool_calls_made for e in result.executions} == {CALLS_PER_DECISION}


def test_every_arm_gets_the_same_turn_allowance(rig: Rig) -> None:
    """The runner fixes ``max_model_turns``, so no materials provider can hand
    one condition more turns than another."""
    never_decides = ModelResponse(
        tool_calls=(ToolCallRequest("search_instruments", {"query": ""}),)
    )
    config = RunConfig(run_id=RUN_ID, max_model_turns=2, max_tool_calls=50)
    result = _run(
        _runner(rig, model_factory=lambda: _FixedModel(never_decides), config=config), rig
    )
    assert {e.outcome.model_turns for e in result.executions} == {2}
    assert all(
        [f.kind for f in e.outcome.failures] == [AgentFailureKind.TRUNCATED_OUTPUT]
        for e in result.executions
    )


# ---------------------------------------------------------------------------
# Granted material — the only channel a condition may act through
# ---------------------------------------------------------------------------


def test_under_the_null_provider_every_arm_decides_identically(rig: Rig) -> None:
    """An A/A test. With no memory or reflection subsystem yet, every arm is
    handed the identical (empty) context, so any divergence between arms would
    be a leak, not an effect."""
    result = _run(_runner(rig), rig)
    assert len({e.content_hash for e in result.executions}) == 1
    assert all(e.context_blob_hash is None for e in result.executions)


def test_granted_material_actually_reaches_the_model(rig: Rig) -> None:
    seen: list[str | None] = []
    result = _run(
        _runner(rig, model_factory=lambda: _RecordingModel(seen), materials=_PerArmMaterials()), rig
    )
    granted = {
        f"material for {e.arm_id} rep {e.repetition}"
        for e in result.executions
        if e.context_blob_hash is not None
    }
    assert granted <= set(filter(None, seen))
    # A grants nothing, so exactly the five other arms carry material.
    assert len(granted) == 5


def test_the_control_arm_is_granted_nothing_even_under_a_real_provider(rig: Rig) -> None:
    result = _run(_runner(rig, materials=_PerArmMaterials()), rig)
    control = result.execution_for(ArmId.A)
    assert control is not None
    assert control.context_blob_hash is None


def test_granted_material_is_stored_verbatim_and_is_retrievable(rig: Rig) -> None:
    result = _run(_runner(rig, materials=_PerArmMaterials()), rig)
    arm_b = result.execution_for(ArmId.B)
    assert arm_b is not None
    assert arm_b.context_blob_hash is not None
    stored = rig.blobs.get(arm_b.context_blob_hash).decode("utf-8")
    assert stored == f"material for {ArmId.B} rep 0"


def test_two_arms_granted_identical_material_share_a_context_hash(rig: Rig) -> None:
    """Content addressing makes an unmatched placebo detectable: if a placebo
    generator ever returned the genuine article, B and B' would collapse onto
    one context hash rather than differing."""

    class _SameForEveryone:
        def materials_for(
            self, arm: ArmSpec, *, cycle_id: str, as_of: Instant, repetition: int
        ) -> str | None:
            return "identical material"

    result = _run(_runner(rig, materials=_SameForEveryone()), rig)
    assert len({e.context_blob_hash for e in result.executions}) == 1


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


def test_execution_positions_are_dense_and_ordered(rig: Rig) -> None:
    result = _run(_runner(rig), rig)
    assert [e.position for e in result.executions] == list(range(6))


def test_the_order_rotates_between_cycles_and_is_recorded(rig: Rig) -> None:
    runner = _runner(rig)
    first = _run(runner, rig, cycle_index=0)
    second = _run(runner, rig, cycle_index=1)
    assert first.order != second.order

    recorded = [payload["order"] for payload in _event_types(rig, "CYCLE_STARTED")]
    assert recorded[0] == [f"{u.arm_id}#{u.repetition}" for u in first.order]
    assert recorded[1] == [f"{u.arm_id}#{u.repetition}" for u in second.order]


def test_a_randomized_run_still_executes_every_condition(rig: Rig) -> None:
    config = RunConfig(run_id=RUN_ID, order_policy=OrderPolicy.RANDOMIZED, seed="abc")
    result = _run(_runner(rig, config=config), rig, cycle_index=4)
    assert {e.arm_id for e in result.executions} == set(config.arms)


# ---------------------------------------------------------------------------
# Persistence, resume, audit trail
# ---------------------------------------------------------------------------


def _bundle_count(rig: Rig) -> int:
    total = rig.session.execute(select(func.count()).select_from(DecisionBundleRow)).scalar_one()
    return int(total)


def test_every_bundle_is_persisted_and_reloadable(rig: Rig) -> None:
    result = _run(_runner(rig), rig)
    assert _bundle_count(rig) == 6
    for execution in result.executions:
        reloaded = rig.session.get(DecisionBundleRow, execution.bundle_id)
        assert reloaded is not None
        assert reloaded.arm_id == str(execution.arm_id)
        assert reloaded.content_hash == execution.content_hash
        assert reloaded.snapshot_id == rig.snapshot_id


def test_a_reloaded_execution_matches_the_original(rig: Rig) -> None:
    result = _run(_runner(rig), rig)
    original = result.executions[0]
    reloaded = _runner(rig).load_execution(original.bundle_id)
    assert reloaded == original


def test_loading_an_unknown_bundle_returns_none(rig: Rig) -> None:
    assert _runner(rig).load_execution("f" * 64) is None


def test_rerunning_a_cycle_resumes_instead_of_re_deciding(rig: Rig) -> None:
    """§30.6: a resumed cycle must not spend a second model call, and must not
    replace a condition's recorded decision with a fresh draw."""
    factory = _CountingFactory()
    runner = _runner(rig, model_factory=factory)
    first = _run(runner, rig)
    assert factory.instances == 6

    second = _run(runner, rig)
    assert factory.instances == 6  # no model was consulted the second time
    assert _bundle_count(rig) == 6
    assert {e.bundle_id for e in second.executions} == {e.bundle_id for e in first.executions}
    assert {e.content_hash for e in second.executions} == {e.content_hash for e in first.executions}


def test_sealing_emits_one_decision_event_per_condition(rig: Rig) -> None:
    result = _run(_runner(rig), rig)
    sealed = _event_types(rig, "DECISION_SEALED")
    assert len(sealed) == 6
    assert {payload["bundle_id"] for payload in sealed} == {e.bundle_id for e in result.executions}


def test_the_cycle_boundary_events_frame_the_run(rig: Rig) -> None:
    result = _run(_runner(rig), rig)
    started = _event_types(rig, "CYCLE_STARTED")
    completed = _event_types(rig, "CYCLE_COMPLETED")
    assert len(started) == len(completed) == 1
    assert started[0]["cycle_id"] == result.cycle_id
    assert completed[0]["executed"] == 6
    assert completed[0]["missing"] == 0


def test_the_event_chain_stays_intact_across_a_full_cycle(rig: Rig) -> None:
    _run(_runner(rig), rig)
    assert rig.events.verify_chain() > 0


def test_decision_bundles_are_append_only(rig: Rig, database: Database) -> None:
    _run(_runner(rig), rig)
    with (
        pytest.raises(SqlIntegrityError, match="append-only"),
        database.engine.begin() as conn,
    ):
        conn.execute(text("UPDATE decision_bundles SET model_id = 'tampered'"))


# ---------------------------------------------------------------------------
# Failures: observed versus missing
# ---------------------------------------------------------------------------


def test_a_refusal_is_recorded_per_condition_without_stopping_the_cycle(rig: Rig) -> None:
    refusal = ModelResponse(refused=True, refusal_reason="not enough information")
    result = _run(_runner(rig, model_factory=lambda: _FixedModel(refusal)), rig)

    assert len(result.executions) == 6
    assert result.missing == ()
    assert all(
        [f.kind for f in e.outcome.failures] == [AgentFailureKind.REFUSAL]
        for e in result.executions
    )
    assert len(_event_types(rig, "AGENT_FAILURE_OBSERVED")) == 6


def test_two_identical_failures_in_one_bundle_are_both_recorded(rig: Rig) -> None:
    """``EventStore.append`` deduplicates by derived event id, so two failures
    identical in kind, detail, instant and routing keys would collapse into one
    event — under-reporting exactly the observations §23.3 exists to keep. The
    per-bundle sequence number in the payload is what prevents that."""
    twice_unknown = ModelResponse(
        tool_calls=(ToolCallRequest("no_such_tool", {}), ToolCallRequest("no_such_tool", {}))
    )
    config = RunConfig(run_id=RUN_ID, arms=(ArmId.A,), max_model_turns=1)
    result = _run(
        _runner(rig, model_factory=lambda: _FixedModel(twice_unknown), config=config), rig
    )

    execution = result.executions[0]
    kinds = [f.kind for f in execution.outcome.failures]
    assert kinds.count(AgentFailureKind.SCHEMA_VIOLATION) == 2

    observed = _event_types(rig, "AGENT_FAILURE_OBSERVED")
    schema_violations = [p for p in observed if p["kind"] == str(AgentFailureKind.SCHEMA_VIOLATION)]
    assert len(schema_violations) == 2
    assert sorted(int(p["sequence"]) for p in schema_violations) == [0, 1]


def test_a_provider_outage_leaves_one_condition_missing_and_the_rest_intact(rig: Rig) -> None:
    """§23.4: a missing condition is not a null result. It is an empty cell the
    analysis has to treat as missing, so it is recorded as such rather than
    being folded in with the agent failures."""
    counter = [0]
    result = _run(_runner(rig, model_factory=lambda: _UnreachableProviderModel(3, counter)), rig)

    assert len(result.executions) == 5
    assert len(result.missing) == 1
    missing = result.missing[0]
    assert result.execution_for(missing.arm_id, repetition=missing.repetition) is None
    assert "unreachable" in missing.reason

    events = _event_types(rig, "CONDITION_MISSING")
    assert len(events) == 1
    assert events[0]["arm_id"] == str(missing.arm_id)
    assert _bundle_count(rig) == 5


def test_a_missing_condition_is_retried_on_a_resumed_cycle(rig: Rig) -> None:
    counter = [0]
    runner = _runner(rig, model_factory=lambda: _UnreachableProviderModel(3, counter))
    first = _run(runner, rig)
    assert len(first.missing) == 1

    healthy = _runner(rig)
    second = _run(healthy, rig)
    assert second.missing == ()
    assert len(second.executions) == 6
    assert _bundle_count(rig) == 6


# ---------------------------------------------------------------------------
# The decision fingerprint
# ---------------------------------------------------------------------------


_A_FORECAST = (Forecast("id-a", 5, 0.6, ("ev-1",)),)
_AN_INTENT = (TradeIntent("id-a", TradeSide.BUY, "why", ("ev-1",)),)


def _outcome(
    *,
    forecasts: tuple[Forecast, ...] = _A_FORECAST,
    trade_intents: tuple[TradeIntent, ...] = _AN_INTENT,
    failures: tuple[ObservedAgentFailure, ...] = (),
    tool_calls_made: int = 3,
    model_turns: int = 4,
) -> DecisionOutcome:
    return DecisionOutcome(
        snapshot_id="snap",
        forecasts=forecasts,
        trade_intents=trade_intents,
        failures=failures,
        tool_calls_made=tool_calls_made,
        model_turns=model_turns,
    )


def test_the_content_hash_ignores_how_the_decision_was_reached() -> None:
    """Two identical decisions arrived at in a different number of turns are
    still the same decision. Process metrics stay queryable on the row."""
    assert decision_content_hash(_outcome()) == decision_content_hash(
        _outcome(
            tool_calls_made=11,
            model_turns=6,
            failures=(
                ObservedAgentFailure(
                    kind=AgentFailureKind.REFUSAL, detail="x", occurred_at=SESSION_1
                ),
            ),
        )
    )


def test_the_content_hash_changes_when_a_probability_changes() -> None:
    assert decision_content_hash(_outcome()) != decision_content_hash(
        _outcome(forecasts=(Forecast("id-a", 5, 0.61, ("ev-1",)),))
    )


def test_the_content_hash_changes_when_a_trade_side_changes() -> None:
    assert decision_content_hash(_outcome()) != decision_content_hash(
        _outcome(trade_intents=(TradeIntent("id-a", TradeSide.SELL, "why", ("ev-1",)),))
    )


def test_the_content_hash_changes_when_the_citations_change() -> None:
    assert decision_content_hash(_outcome()) != decision_content_hash(
        _outcome(forecasts=(Forecast("id-a", 5, 0.6, ("ev-2",)),))
    )


def test_a_decision_round_trips_through_the_stored_payload(rig: Rig) -> None:
    refusal = ModelResponse(
        decision=RawDecision(forecasts=(), trade_intents=(), narrative="nothing to do")
    )
    result = _run(
        _runner(
            rig,
            model_factory=lambda: _FixedModel(refusal),
            config=RunConfig(run_id=RUN_ID, arms=(ArmId.A,)),
        ),
        rig,
    )
    original = result.executions[0]
    assert _runner(rig).load_execution(original.bundle_id) == original
