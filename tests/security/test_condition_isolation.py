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

from marketlab.agents.decision import ConditionContext, DecisionAgent, DecisionOutcome
from marketlab.models.types import LanguageModel, ModelRequest, ModelResponse

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
