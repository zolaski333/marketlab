"""Structural enforcement: nothing on the model-call path can see which
experimental condition it is running under.

The prior implementation's audit found a mock LLM branching on
``condition_id`` (see ``docs/ROADMAP.md``'s origin story) — a defect that a
code review can miss once, because nothing forces the reviewer to check every
new field on every new type forever. This inspects the actual dataclass
fields and the actual :meth:`LanguageModel.generate` signature directly, the
same way ``test_decision_path_isolation.py`` inspects imports rather than
trusting a convention: it fails the moment a future change adds a
condition/arm/repetition-identifying field anywhere on this path, before that
field could ever be read by a policy.

A bare ``condition`` name is deliberately *not* forbidden: the parameter that
carries :class:`~marketlab.agents.decision.ConditionContext` — the materials
a condition grants — is legitimately named that. What must never exist is a
field that lets code learn *which* condition or arm it is.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from marketlab.agents.decision import ConditionContext, DecisionAgent, DecisionOutcome
from marketlab.core.canonical import canonical_json
from marketlab.core.clock import FrozenClock
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.evaluation.panels import PanelStore
from marketlab.experiments.arms import ARMS, ArmSpec
from marketlab.experiments.runner import ArmExecution, CycleRunner, RunConfig
from marketlab.ingestion.pipeline import IngestionPipeline
from marketlab.ingestion.types import RawPriceBar
from marketlab.instruments.repository import InstrumentRepository
from marketlab.instruments.types import AssetClass, ExecutionModel
from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.models.types import LanguageModel, ModelRequest, ModelResponse
from marketlab.snapshots.builder import SnapshotBuilder, SnapshotCandidate
from marketlab.storage.blobs import BlobStore
from marketlab.storage.events import EventStore

ADMITTED_AT = instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))
SESSION_1 = instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC))

_FORBIDDEN_NORMALIZED_NAMES = frozenset(
    {
        "conditionid",
        "conditionlabel",
        "conditionname",
        "arm",
        "armid",
        "armlabel",
        "armname",
        "repetition",
        "repetitionid",
        "repetitionnumber",
    }
)


def _normalize(name: str) -> str:
    return name.lower().replace("_", "")


def _offending_field_names(cls: type) -> set[str]:
    return {
        field.name
        for field in dataclasses.fields(cls)  # type: ignore[arg-type]
        if _normalize(field.name) in _FORBIDDEN_NORMALIZED_NAMES
    }


def _offending_parameter_names(signature: inspect.Signature) -> set[str]:
    return {
        name for name in signature.parameters if _normalize(name) in _FORBIDDEN_NORMALIZED_NAMES
    }


def test_the_forbidden_name_matcher_actually_matches_known_bad_names() -> None:
    # Guards against this test module itself passing vacuously if the
    # matcher were ever mistyped.
    assert _normalize("condition_id") in _FORBIDDEN_NORMALIZED_NAMES
    assert _normalize("arm_id") in _FORBIDDEN_NORMALIZED_NAMES
    assert _normalize("repetition") in _FORBIDDEN_NORMALIZED_NAMES


def test_model_request_carries_no_condition_identifying_field() -> None:
    assert _offending_field_names(ModelRequest) == set()


def test_model_response_carries_no_condition_identifying_field() -> None:
    assert _offending_field_names(ModelResponse) == set()


def test_condition_context_carries_no_condition_label_only_granted_materials() -> None:
    assert _offending_field_names(ConditionContext) == set()


def test_decision_outcome_carries_no_condition_identifying_field() -> None:
    assert _offending_field_names(DecisionOutcome) == set()


def test_language_model_generate_signature_has_no_condition_parameter() -> None:
    signature = inspect.signature(LanguageModel.generate)
    assert _offending_parameter_names(signature) == set()


def test_decision_agent_decide_signature_has_no_condition_id_parameter() -> None:
    signature = inspect.signature(DecisionAgent.decide)
    assert _offending_parameter_names(signature) == set()


def test_the_runner_may_name_arms_because_it_sits_above_the_model_path() -> None:
    """The complement of every assertion above, stated so nobody 'fixes' it.

    :class:`~marketlab.experiments.runner.ArmExecution` *does* carry
    ``arm_id`` and ``repetition``: it is the result record, produced strictly
    after the model has finished, and the analysis cannot route anything
    without it. The invariant is not "the string 'arm' appears nowhere" — it
    is that nothing the model can read carries it.
    """
    assert _offending_field_names(ArmExecution) == {"arm_id", "repetition"}


# ---------------------------------------------------------------------------
# Content-level: what the model can actually read
# ---------------------------------------------------------------------------
#
# The checks above prove no *field* names a condition. They cannot prove no
# condition identity is smuggled through a field that legitimately holds text
# — ``injected_context`` most obviously, but ``system_prompt`` too. So this
# runs a real cycle across all six conditions and inspects every
# ``ModelRequest`` that actually reached a model.


class _RequestCapturingModel:
    """Delegates to the real policy, keeping every request it was handed."""

    def __init__(self, captured: list[ModelRequest]) -> None:
        self._captured = captured
        self._inner = DeterministicPolicyModel()

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    def generate(self, request: ModelRequest) -> ModelResponse:
        self._captured.append(request)
        return self._inner.generate(request)


class _LabellingMaterials:
    """A deliberately careless provider: it grants text derived from the arm's
    *grants*, which is legitimate, and this test proves that even so no arm
    *identity* reaches the model."""

    def materials_for(
        self, arm: ArmSpec, *, cycle_id: str, as_of: Instant, repetition: int
    ) -> str | None:
        if not arm.grants_anything:
            return None
        return f"Prior context available: memory={arm.memory}, reflection={arm.reflection}."


def _capture_requests_across_a_full_cycle(
    session: Session, clock: FrozenClock, blob_store: BlobStore
) -> list[ModelRequest]:
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
    bar = RawPriceBar(
        instrument_id=view.instrument_id,
        as_of=SESSION_1,
        bid=Decimal("149.98"),
        ask=Decimal("150.08"),
        close=Decimal("150.03"),
        volume=1000,
        first_seen_at=SESSION_1,
    )
    manifest = builder.build(
        [SnapshotCandidate("PRICE_BAR", pipeline.ingest_price_bar(bar))],
        as_of=SESSION_1,
        run_id="ISOLATION_RUN",
    )

    captured: list[ModelRequest] = []
    CycleRunner(
        session=session,
        clock=clock,
        blobs=blob_store,
        events=events,
        builder=builder,
        model_factory=lambda: _RequestCapturingModel(captured),
        materials=_LabellingMaterials(),
        config=RunConfig(run_id="ISOLATION_RUN"),
        # With the panel configured, so the second model-call path this
        # platform has is inspected too rather than only the first.
        panels=PanelStore(session, clock, blob_store),
    ).run_cycle(cycle_index=0, snapshot_id=manifest.snapshot_id, as_of=SESSION_1)
    return captured


def test_no_arm_identity_reaches_the_model_anywhere_in_a_real_cycle(
    session: Session, clock: FrozenClock, blob_store: BlobStore
) -> None:
    captured = _capture_requests_across_a_full_cycle(session, clock, blob_store)
    assert captured, "the cycle produced no model requests at all"

    # Every arm's identifier and its display label, as whole words. A bare
    # "A"/"B" substring match would fire on ordinary English, so the search is
    # over word tokens rather than raw substrings. It stays case-sensitive and
    # keeps the single-letter ids in the set: a prompt that legitimately says
    # "Option A" would trip this, and that false positive is the cheaper
    # error — it costs one reading, where a missed leak costs the study.
    forbidden = {str(arm_id) for arm_id in ARMS} | {spec.label for spec in ARMS.values()}
    for request in captured:
        text = canonical_json(
            {
                "system_prompt": request.system_prompt,
                "injected_context": request.injected_context,
                "tool_catalogue": [dataclasses.asdict(s) for s in request.tool_catalogue],
                "tool_results": [dataclasses.asdict(r) for r in request.tool_results],
                "required_forecasts": [list(key) for key in request.required_forecasts],
            }
        )
        tokens = set(re.findall(r"[A-Za-z_']+", text))
        assert not (tokens & forbidden), sorted(tokens & forbidden)


def test_the_token_search_would_catch_a_leak_if_one_existed() -> None:
    """Guards the previous test against passing vacuously: the same matcher,
    run over text that does leak, must fail."""
    leaked = canonical_json({"injected_context": "You are running as arm C_PRIME."})
    tokens = set(re.findall(r"[A-Za-z_']+", leaked))
    forbidden = {str(arm_id) for arm_id in ARMS} | {spec.label for spec in ARMS.values()}
    assert tokens & forbidden == {"C_PRIME"}


def test_conditions_differ_in_granted_text_and_in_nothing_else(
    session: Session, clock: FrozenClock, blob_store: BlobStore
) -> None:
    """The positive half of the guarantee. Strip ``injected_context`` and every
    condition's requests become indistinguishable — so the granted material is
    not merely *a* difference between arms, it is the only one."""
    captured = _capture_requests_across_a_full_cycle(session, clock, blob_store)

    stripped = {
        canonical_json(
            {
                "system_prompt": request.system_prompt,
                "tool_catalogue": [dataclasses.asdict(s) for s in request.tool_catalogue],
                "tool_results": [dataclasses.asdict(r) for r in request.tool_results],
                "required_forecasts": [list(key) for key in request.required_forecasts],
            }
        )
        for request in captured
    }
    # One distinct shape per turn of the loop, not per condition: all six arms
    # walk the same turns over the same frozen snapshot.
    turns_per_decision = len(captured) // len(ARMS)
    assert len(stripped) == turns_per_decision
