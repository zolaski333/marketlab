"""Runs every arm of one cycle against one frozen snapshot (§13, §23.4, §30.3).

This is the layer that finally holds ``run_id``, ``arm_id`` and ``repetition``
— the identifiers :mod:`marketlab.agents.decision` structurally refuses to
know. It sits *above* the decision path, so it can route, identify and persist
results without any of that reaching the model.

The three guarantees this module exists to provide
--------------------------------------------------
**Identical evidence.** The
:class:`~marketlab.retrieval.types.RetrievalIndex` is loaded exactly once per
cycle and the same object is handed to every arm. Not "rebuilt identically" —
the same object, so no rebuild can drift. A difference between two arms'
decisions therefore cannot be a difference in what they were shown.

**Identical allowance.** Every unit gets a *fresh*
:class:`~marketlab.retrieval.budget.ToolBudget` and a *fresh* model instance
from the factory, both with the same limits, and the runner — not the
materials provider — sets ``max_model_turns``. An arm cannot win by being
allowed more calls, more characters, more turns, or by inheriting a warmed-up
model that already saw another arm's decision.

**Total identification.** ``bundle_id`` is derived from run, cycle, arm *and*
repetition. :func:`marketlab.core.ids.derive_id`'s docstring records why that
last part is spelled out: a previous implementation omitted ``repetition`` and
would have collapsed every repetition of an arm onto a single bundle. A unique
constraint on ``(run_id, cycle_id, arm_id, repetition)`` backs the derivation
up, so even a wrong derivation could not silently produce two bundles for one
condition.

Resuming, not re-deciding
-------------------------
:meth:`CycleRunner.run_cycle` is idempotent (§30.6). A unit whose bundle is
already recorded is rehydrated from its stored payload instead of being run
again — a resumed cycle must not spend a second model call, and more
importantly must not produce a *second, different* decision for a condition
that already has one.

The panel, when one is configured
---------------------------------
A condition's free decision is followed by the imposed forecast panel (§15),
run through :class:`~marketlab.agents.panel.PanelAgent` with a **second fresh
model instance and a second fresh budget**. Isolated from the decision, not
from the condition: the panel receives the same granted material, because it
is where the treatment is measured on questions every arm was asked. The panel
is optional here only so that the many tests that care solely about decisions
need not pay for it; a real run configures one, because
:mod:`marketlab.analysis.pairing` has nothing to pair without it.

What is deliberately not here
-----------------------------
Turning a :class:`~marketlab.agents.decision.TradeIntent` into an order,
sizing it, and touching a ledger is task 10. This module seals what each
condition decided; it does not act on it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from sqlalchemy import Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.agents.decision import (
    DEFAULT_MAX_MODEL_TURNS,
    ConditionContext,
    DecisionAgent,
    DecisionOutcome,
    Forecast,
    TradeIntent,
)
from marketlab.agents.panel import PanelAgent
from marketlab.core.canonical import canonical_bytes, canonical_hash
from marketlab.core.clock import Clock
from marketlab.core.failures import (
    AgentFailureKind,
    ConfigurationError,
    ModelProviderError,
    ObservedAgentFailure,
)
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant
from marketlab.evaluation.panels import PanelRecord, PanelStore, panel_bundle_id_for
from marketlab.experiments.arms import ARMS, DEFAULT_ARMS, ArmId, spec_for
from marketlab.experiments.context import ConditionMaterialsProvider
from marketlab.experiments.ordering import OrderPolicy, execution_order
from marketlab.forecasting.panel import DEFAULT_HORIZONS, build_panel
from marketlab.models.types import LanguageModel, TradeSide
from marketlab.retrieval.budget import (
    DEFAULT_MAX_EVIDENCE_CHARS,
    DEFAULT_MAX_TOOL_CALLS,
    ToolBudget,
)
from marketlab.retrieval.tools import RetrievalToolkit
from marketlab.retrieval.types import RetrievalIndex
from marketlab.snapshots.builder import SnapshotBuilder
from marketlab.storage.base import Base, HashStr, InstantStr, ShortStr
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

__all__ = [
    "ArmExecution",
    "CycleResult",
    "CycleRunner",
    "DecisionBundleRow",
    "ExecutionUnit",
    "MissingCondition",
    "RunConfig",
    "outcome_from_payload",
]

_DEFAULT_SEED: Final = "marketlab"


class DecisionBundleRow(Base):
    """One condition's sealed decision for one cycle.

    The decision *content* lives in a content-addressed blob;
    ``content_hash`` is the fingerprint analyses compare arms on. Keeping the
    payload out of the row means two arms that decided identically are
    detectable by an equality test on 64 characters rather than by diffing
    documents.
    """

    __tablename__ = "decision_bundles"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "cycle_id",
            "arm_id",
            "repetition",
            name="uq_decision_bundles_condition",
        ),
    )

    bundle_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    run_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    cycle_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    arm_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)

    position: Mapped[int] = mapped_column(Integer, nullable=False)
    """0-based execution position within the cycle, kept so an order effect can
    be tested for rather than assumed absent."""

    as_of: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    sealed_at: Mapped[str] = mapped_column(InstantStr, nullable=False)
    model_id: Mapped[str] = mapped_column(ShortStr, nullable=False)

    content_hash: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    payload_blob_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    context_blob_hash: Mapped[str | None] = mapped_column(HashStr, nullable=True)
    """Blob holding the exact injected material, or NULL when the arm was
    granted none. Content addressing means two arms that were handed identical
    material share a hash — which is how a placebo generator that failed to
    differ from the genuine article gets caught."""

    forecast_count: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_intent_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_calls_made: Mapped[int] = mapped_column(Integer, nullable=False)
    model_turns: Mapped[int] = mapped_column(Integer, nullable=False)


@dataclass(frozen=True, slots=True)
class ExecutionUnit:
    """One (arm, repetition) pair — the thing that is actually ordered and run."""

    arm_id: ArmId
    repetition: int


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Pre-registered parameters of one run.

    Every field is a decision that must be fixed *before* the run, not tuned
    while looking at results: which conditions are compared, how many
    independent repetitions each gets, how their order is balanced, and what
    each is allowed to spend.
    """

    run_id: str
    arms: tuple[ArmId, ...] = DEFAULT_ARMS
    repetitions: int = 1
    seed: str = _DEFAULT_SEED
    order_policy: OrderPolicy = OrderPolicy.LATIN_SQUARE
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_evidence_chars: int = DEFAULT_MAX_EVIDENCE_CHARS
    max_model_turns: int = DEFAULT_MAX_MODEL_TURNS
    panel_horizons: tuple[int, ...] = DEFAULT_HORIZONS
    """Horizons the imposed panel asks about, in sessions. Pre-registered
    before the run: choosing them afterwards would be choosing which horizon
    the study reports on after seeing which one worked."""

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ConfigurationError("run_id must not be empty.")
        if not self.arms:
            raise ConfigurationError("A run needs at least one arm.")
        if len(set(self.arms)) != len(self.arms):
            raise ConfigurationError(
                f"Duplicate arms in {[str(a) for a in self.arms]}: an arm listed twice "
                "would be run twice per cycle under one identity, silently "
                "double-weighting it in the analysis. Use `repetitions` instead."
            )
        unknown = [str(arm) for arm in self.arms if arm not in ARMS]
        if unknown:
            raise ConfigurationError(f"Unknown arms: {unknown}", unknown=unknown)
        if self.repetitions < 1:
            raise ConfigurationError(f"repetitions must be >= 1, got {self.repetitions}")
        if self.max_model_turns < 1:
            raise ConfigurationError(f"max_model_turns must be >= 1, got {self.max_model_turns}")

    def units(self) -> tuple[ExecutionUnit, ...]:
        """Every (arm, repetition) unit in canonical declaration order."""
        return tuple(
            ExecutionUnit(arm_id=arm, repetition=repetition)
            for arm in self.arms
            for repetition in range(self.repetitions)
        )


@dataclass(frozen=True, slots=True)
class ArmExecution:
    """What one condition decided in one cycle, and how it was identified.

    Unlike anything on the decision path, this record *does* name its arm and
    repetition — it is the routing information the analysis needs, produced
    strictly after the model has finished and can no longer be influenced
    by it.
    """

    bundle_id: str
    arm_id: ArmId
    repetition: int
    position: int
    model_id: str
    content_hash: str
    context_blob_hash: str | None
    outcome: DecisionOutcome
    panel: PanelRecord | None = None
    """The sealed panel, when the runner was configured with one. ``None``
    means no panel was asked for — never "the condition answered nothing",
    which is an empty :class:`~marketlab.agents.panel.PanelOutcome` instead."""


@dataclass(frozen=True, slots=True)
class MissingCondition:
    """One condition that could not be produced at all (§23.4).

    Distinct from an :class:`~marketlab.core.failures.ObservedAgentFailure`:
    an agent failure means the agent did something, and what it did is data. A
    missing condition means there is no observation for this cell, which the
    analysis must handle as missing rather than as a null result.
    """

    arm_id: ArmId
    repetition: int
    reason: str


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Every condition's outcome for one cycle."""

    cycle_id: str
    run_id: str
    cycle_index: int
    snapshot_id: str
    as_of: Instant
    executions: tuple[ArmExecution, ...]
    """In execution order, not declaration order."""

    missing: tuple[MissingCondition, ...] = ()

    def execution_for(self, arm_id: ArmId, *, repetition: int = 0) -> ArmExecution | None:
        """Find one condition's execution, or ``None`` if it is missing."""
        for execution in self.executions:
            if execution.arm_id is arm_id and execution.repetition == repetition:
                return execution
        return None

    @property
    def order(self) -> tuple[ExecutionUnit, ...]:
        """The units in the order they actually ran."""
        return tuple(
            ExecutionUnit(arm_id=e.arm_id, repetition=e.repetition) for e in self.executions
        )

    def content_hashes(self) -> dict[ExecutionUnit, str]:
        """Decision fingerprint per unit — the basis for every arm comparison."""
        return {
            ExecutionUnit(arm_id=e.arm_id, repetition=e.repetition): e.content_hash
            for e in self.executions
        }


@dataclass(slots=True)
class CycleRunner:
    """Executes one cycle across every configured condition."""

    session: Session
    clock: Clock
    blobs: BlobStore
    events: EventStore
    builder: SnapshotBuilder
    model_factory: Callable[[], LanguageModel]
    """Called once per unit. A *factory*, not a model: reusing one instance
    across arms would let a stateful adapter carry one condition's context into
    the next."""

    materials: ConditionMaterialsProvider
    config: RunConfig
    agent: DecisionAgent = field(default_factory=DecisionAgent)
    panels: PanelStore | None = None
    """Set to elicit and seal the imposed panel after each decision. Left
    unset, no panel runs at all — which is honest (nothing is recorded) rather
    than convenient (an empty panel recorded as if it had been asked)."""

    panel_agent: PanelAgent = field(default_factory=PanelAgent)

    def run_cycle(self, *, cycle_index: int, snapshot_id: str, as_of: Instant) -> CycleResult:
        """Run every condition against one frozen snapshot.

        Raises:
            marketlab.core.failures.SnapshotError: if ``snapshot_id`` was never
                built. Deliberately not caught: a cycle with no evidence is not
                a cycle with a missing arm, it is a cycle that cannot happen.
        """
        index = self.builder.load_index(snapshot_id)
        cycle_id = derive_id(
            IdKind.CYCLE,
            run_id=self.config.run_id,
            cycle_index=cycle_index,
            snapshot_id=snapshot_id,
            as_of=str(as_of),
        )
        ordered = execution_order(
            self.config.units(),
            policy=self.config.order_policy,
            cycle_index=cycle_index,
            seed=f"{self.config.seed}|run={self.config.run_id}",
        )

        self.events.append(
            "CYCLE_STARTED",
            {
                "cycle_id": cycle_id,
                "cycle_index": cycle_index,
                "snapshot_id": snapshot_id,
                "snapshot_status": str(index.status),
                "order": [f"{unit.arm_id}#{unit.repetition}" for unit in ordered],
                "order_policy": str(self.config.order_policy),
            },
            occurred_at=as_of,
            run_id=self.config.run_id,
            cycle_id=cycle_id,
        )

        executions: list[ArmExecution] = []
        missing: list[MissingCondition] = []
        for position, unit in enumerate(ordered):
            outcome = self._run_unit(
                unit, position=position, index=index, cycle_id=cycle_id, as_of=as_of
            )
            if isinstance(outcome, MissingCondition):
                missing.append(outcome)
            else:
                executions.append(outcome)

        self.events.append(
            "CYCLE_COMPLETED",
            {
                "cycle_id": cycle_id,
                "executed": len(executions),
                "missing": len(missing),
                "content_hashes": {
                    f"{e.arm_id}#{e.repetition}": e.content_hash for e in executions
                },
            },
            occurred_at=as_of,
            run_id=self.config.run_id,
            cycle_id=cycle_id,
        )

        return CycleResult(
            cycle_id=cycle_id,
            run_id=self.config.run_id,
            cycle_index=cycle_index,
            snapshot_id=snapshot_id,
            as_of=as_of,
            executions=tuple(executions),
            missing=tuple(missing),
        )

    # -- reading -------------------------------------------------------------

    def load_execution(self, bundle_id: str) -> ArmExecution | None:
        """Rehydrate a sealed bundle, or ``None`` if it was never recorded."""
        row = self.session.get(DecisionBundleRow, bundle_id)
        if row is None:
            return None
        payload = json.loads(self.blobs.get(row.payload_blob_hash))
        return ArmExecution(
            bundle_id=row.bundle_id,
            arm_id=ArmId(row.arm_id),
            repetition=row.repetition,
            position=row.position,
            model_id=row.model_id,
            content_hash=row.content_hash,
            context_blob_hash=row.context_blob_hash,
            outcome=outcome_from_payload(payload),
            panel=(
                self.panels.load(panel_bundle_id_for(bundle_id))
                if self.panels is not None
                else None
            ),
        )

    # -- one condition -------------------------------------------------------

    def _run_unit(
        self,
        unit: ExecutionUnit,
        *,
        position: int,
        index: RetrievalIndex,
        cycle_id: str,
        as_of: Instant,
    ) -> ArmExecution | MissingCondition:
        bundle_id = derive_id(
            IdKind.DECISION_BUNDLE,
            run_id=self.config.run_id,
            cycle_id=cycle_id,
            arm_id=str(unit.arm_id),
            repetition=unit.repetition,
        )
        already_sealed = self.load_execution(bundle_id)
        arm = spec_for(unit.arm_id)
        material = self.materials.materials_for(
            arm, cycle_id=cycle_id, as_of=as_of, repetition=unit.repetition
        )
        if already_sealed is not None:
            # §30.6: resuming must not re-decide. Returning the stored result
            # keeps a resumed cycle identical to the interrupted one rather
            # than replacing one condition's decision with a fresh draw. The
            # panel is a separate elicitation, so an interruption between the
            # two leaves a decision with no panel; that hole is filled on
            # resume rather than left for the analysis to trip over.
            if self.panels is None or already_sealed.panel is not None:
                return already_sealed
            return replace(
                already_sealed,
                panel=self._elicit_panel(
                    unit,
                    bundle_id=bundle_id,
                    condition=ConditionContext(
                        injected_context=material, max_model_turns=self.config.max_model_turns
                    ),
                    index=index,
                    cycle_id=cycle_id,
                    as_of=as_of,
                ),
            )

        context_blob_hash = (
            self.blobs.put(material.encode("utf-8")).digest if material is not None else None
        )
        # The runner, not the provider, fixes the turn allowance — see the
        # module docstring on identical allowance.
        condition = ConditionContext(
            injected_context=material, max_model_turns=self.config.max_model_turns
        )
        toolkit = RetrievalToolkit(
            index,
            ToolBudget(
                max_calls=self.config.max_tool_calls,
                max_evidence_chars=self.config.max_evidence_chars,
            ),
        )
        model = self.model_factory()

        try:
            outcome = self.agent.decide(toolkit, model, condition, as_of=as_of)
        except ModelProviderError as exc:
            return self._record_missing(unit, cycle_id=cycle_id, as_of=as_of, reason=str(exc))

        execution = self._seal(
            unit,
            bundle_id=bundle_id,
            position=position,
            cycle_id=cycle_id,
            snapshot_id=index.snapshot_id,
            as_of=as_of,
            model_id=model.model_id,
            context_blob_hash=context_blob_hash,
            outcome=outcome,
        )
        if self.panels is None:
            return execution
        return replace(
            execution,
            panel=self._elicit_panel(
                unit,
                bundle_id=bundle_id,
                condition=condition,
                index=index,
                cycle_id=cycle_id,
                as_of=as_of,
            ),
        )

    def _elicit_panel(
        self,
        unit: ExecutionUnit,
        *,
        bundle_id: str,
        condition: ConditionContext,
        index: RetrievalIndex,
        cycle_id: str,
        as_of: Instant,
    ) -> PanelRecord:
        """Ask this condition the imposed panel, isolated from its decision.

        A *second* fresh model and a *second* fresh budget: an assessment must
        not be cheaper for the arm that spent less deciding, and a stateful
        adapter must not carry the free decision into the answers.
        """
        assert self.panels is not None  # guarded by the only caller
        panel = build_panel(index, horizons=self.config.panel_horizons)
        model = self.model_factory()
        outcome = self.panel_agent.elicit(
            RetrievalToolkit(
                index,
                ToolBudget(
                    max_calls=self.config.max_tool_calls,
                    max_evidence_chars=self.config.max_evidence_chars,
                ),
            ),
            model,
            condition,
            panel,
            as_of=as_of,
        )
        record = self.panels.record(
            outcome,
            decision_bundle_id=bundle_id,
            run_id=self.config.run_id,
            cycle_id=cycle_id,
            arm_id=str(unit.arm_id),
            repetition=unit.repetition,
            as_of=as_of,
            model_id=model.model_id,
            item_count=len(panel),
        )
        self.events.append(
            "PANEL_SEALED",
            {
                "panel_bundle_id": record.panel_bundle_id,
                "decision_bundle_id": bundle_id,
                "snapshot_id": index.snapshot_id,
                "model_id": model.model_id,
                "content_hash": record.content_hash,
                "item_count": record.item_count,
                "answered_count": len(outcome.answers),
                "unanswered_count": outcome.unanswered_count,
                "tool_calls_made": outcome.tool_calls_made,
                "model_turns": outcome.model_turns,
            },
            occurred_at=as_of,
            run_id=self.config.run_id,
            cycle_id=cycle_id,
            arm_id=str(unit.arm_id),
            repetition=unit.repetition,
        )
        return record

    def _seal(
        self,
        unit: ExecutionUnit,
        *,
        bundle_id: str,
        position: int,
        cycle_id: str,
        snapshot_id: str,
        as_of: Instant,
        model_id: str,
        context_blob_hash: str | None,
        outcome: DecisionOutcome,
    ) -> ArmExecution:
        payload_blob = self.blobs.put(canonical_bytes(_outcome_to_payload(outcome)))
        content_hash = decision_content_hash(outcome)

        self.session.add(
            DecisionBundleRow(
                bundle_id=bundle_id,
                run_id=self.config.run_id,
                cycle_id=cycle_id,
                snapshot_id=snapshot_id,
                arm_id=str(unit.arm_id),
                repetition=unit.repetition,
                position=position,
                as_of=str(as_of),
                sealed_at=str(self.clock.now_instant()),
                model_id=model_id,
                content_hash=content_hash,
                payload_blob_hash=payload_blob.digest,
                context_blob_hash=context_blob_hash,
                forecast_count=len(outcome.forecasts),
                trade_intent_count=len(outcome.trade_intents),
                failure_count=len(outcome.failures),
                tool_calls_made=outcome.tool_calls_made,
                model_turns=outcome.model_turns,
            )
        )
        # EventStore.append commits internally, so the row above lands in the
        # same transaction as the event that announces it — the same atomicity
        # SnapshotBuilder.build relies on.
        self.events.append(
            "DECISION_SEALED",
            {
                "bundle_id": bundle_id,
                "snapshot_id": snapshot_id,
                "position": position,
                "model_id": model_id,
                "content_hash": content_hash,
                "payload_blob_hash": payload_blob.digest,
                "context_blob_hash": context_blob_hash,
                "forecast_count": len(outcome.forecasts),
                "trade_intent_count": len(outcome.trade_intents),
                "failure_count": len(outcome.failures),
                "tool_calls_made": outcome.tool_calls_made,
                "model_turns": outcome.model_turns,
            },
            occurred_at=as_of,
            run_id=self.config.run_id,
            cycle_id=cycle_id,
            arm_id=str(unit.arm_id),
            repetition=unit.repetition,
        )

        for sequence, failure in enumerate(outcome.failures):
            # ``sequence`` is in the payload because EventStore.append is
            # idempotent by derived event id: two failures of the same kind,
            # detail and instant under the same routing keys would otherwise
            # derive one id and the second would be silently dropped —
            # under-reporting exactly the observations §23.3 exists to keep.
            self.events.append(
                "AGENT_FAILURE_OBSERVED",
                {
                    "bundle_id": bundle_id,
                    "sequence": sequence,
                    "kind": str(failure.kind),
                    "detail": failure.detail,
                    "scope": str(failure.scope),
                    "context": dict(failure.context),
                },
                occurred_at=failure.occurred_at,
                run_id=self.config.run_id,
                cycle_id=cycle_id,
                arm_id=str(unit.arm_id),
                repetition=unit.repetition,
            )

        return ArmExecution(
            bundle_id=bundle_id,
            arm_id=unit.arm_id,
            repetition=unit.repetition,
            position=position,
            model_id=model_id,
            content_hash=content_hash,
            context_blob_hash=context_blob_hash,
            outcome=outcome,
        )

    def _record_missing(
        self, unit: ExecutionUnit, *, cycle_id: str, as_of: Instant, reason: str
    ) -> MissingCondition:
        self.events.append(
            "CONDITION_MISSING",
            {"arm_id": str(unit.arm_id), "repetition": unit.repetition, "reason": reason},
            occurred_at=as_of,
            run_id=self.config.run_id,
            cycle_id=cycle_id,
            arm_id=str(unit.arm_id),
            repetition=unit.repetition,
        )
        return MissingCondition(arm_id=unit.arm_id, repetition=unit.repetition, reason=reason)


# -- decision fingerprinting ---------------------------------------------------


def decision_content_hash(outcome: DecisionOutcome) -> str:
    """Fingerprint of *what was decided*, ignoring how it was reached.

    Tool call counts, turn counts and recorded failures are deliberately
    excluded: the question every arm comparison starts from is whether two
    conditions reached the same decision, and two identical decisions arrived
    at in a different number of turns are still the same decision. Process
    metrics remain queryable on :class:`DecisionBundleRow` for the analyses
    that do care about them.
    """
    return canonical_hash(
        {
            "forecasts": [_forecast_to_payload(f) for f in outcome.forecasts],
            "trade_intents": [_intent_to_payload(t) for t in outcome.trade_intents],
        }
    )


def _forecast_to_payload(forecast: Forecast) -> dict[str, Any]:
    return {
        "instrument_id": forecast.instrument_id,
        "horizon_sessions": forecast.horizon_sessions,
        "probability_up": forecast.probability_up,
        "cited_evidence_ids": list(forecast.cited_evidence_ids),
    }


def _intent_to_payload(intent: TradeIntent) -> dict[str, Any]:
    return {
        "instrument_id": intent.instrument_id,
        "side": str(intent.side),
        "rationale": intent.rationale,
        "cited_evidence_ids": list(intent.cited_evidence_ids),
    }


def _outcome_to_payload(outcome: DecisionOutcome) -> dict[str, Any]:
    return {
        "snapshot_id": outcome.snapshot_id,
        "forecasts": [_forecast_to_payload(f) for f in outcome.forecasts],
        "trade_intents": [_intent_to_payload(t) for t in outcome.trade_intents],
        "failures": [
            {
                "kind": str(failure.kind),
                "detail": failure.detail,
                "occurred_at": str(failure.occurred_at),
                "context": dict(failure.context),
            }
            for failure in outcome.failures
        ],
        "tool_calls_made": outcome.tool_calls_made,
        "model_turns": outcome.model_turns,
    }


def outcome_from_payload(payload: Mapping[str, Any]) -> DecisionOutcome:
    """Rehydrate a sealed decision from its stored blob.

    Public because resolution (§20) reads decision bundles it did not write,
    and re-implementing this parse there would give the study two readers of
    one format that could drift apart.
    """
    return DecisionOutcome(
        snapshot_id=str(payload["snapshot_id"]),
        forecasts=tuple(
            Forecast(
                instrument_id=str(entry["instrument_id"]),
                horizon_sessions=int(entry["horizon_sessions"]),
                probability_up=float(entry["probability_up"]),
                cited_evidence_ids=tuple(str(e) for e in entry["cited_evidence_ids"]),
            )
            for entry in _sequence(payload["forecasts"])
        ),
        trade_intents=tuple(
            TradeIntent(
                instrument_id=str(entry["instrument_id"]),
                side=TradeSide(entry["side"]),
                rationale=str(entry["rationale"]),
                cited_evidence_ids=tuple(str(e) for e in entry["cited_evidence_ids"]),
            )
            for entry in _sequence(payload["trade_intents"])
        ),
        failures=tuple(
            ObservedAgentFailure(
                kind=AgentFailureKind(entry["kind"]),
                detail=str(entry["detail"]),
                occurred_at=Instant(str(entry["occurred_at"])),
                context=dict(entry["context"]),
            )
            for entry in _sequence(payload["failures"])
        ),
        tool_calls_made=int(payload["tool_calls_made"]),
        model_turns=int(payload["model_turns"]),
    )


def _sequence(value: Any) -> Sequence[Mapping[str, Any]]:
    entries: Sequence[Mapping[str, Any]] = value
    return entries
