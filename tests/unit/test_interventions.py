"""Tests for manual intervention recording (§P6)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from marketlab.audit.interventions import (
    INTERVENTION_EVENT_TYPE,
    InterventionKind,
    InterventionRecorder,
)
from marketlab.core.clock import FrozenClock
from marketlab.core.failures import ConfigurationError, FailureScope
from marketlab.storage.events import EventStore


@pytest.fixture
def recorder(session: Session, clock: FrozenClock) -> InterventionRecorder:
    return InterventionRecorder(EventStore(session, clock), clock)


def valid_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": InterventionKind.RESTART,
        "author": "operator@example.org",
        "reason": "cycle 12 stalled on a provider timeout",
        "affected": ("cycle-12",),
        "effect": "re-ran cycle 12 from the sealed snapshot",
        "scientific_scope": FailureScope.DEGRADED_VALID,
    }
    base.update(overrides)
    return base


def test_intervention_is_recorded_in_the_hash_chain(
    recorder: InterventionRecorder, session: Session, clock: FrozenClock
) -> None:
    """It is an ordinary event, so it cannot be back-dated without breaking the
    chain — which is what makes the audit trail worth anything."""
    event = recorder.record(**valid_kwargs())  # type: ignore[arg-type]

    assert event.event_type == INTERVENTION_EVENT_TYPE
    assert EventStore(session, clock).verify_chain() == 1


def test_all_required_fields_are_captured(recorder: InterventionRecorder) -> None:
    event = recorder.record(**valid_kwargs())  # type: ignore[arg-type]

    assert event.payload["author"] == "operator@example.org"
    assert event.payload["reason"] == "cycle 12 stalled on a provider timeout"
    assert event.payload["affected"] == ["cycle-12"]
    assert event.payload["effect"] == "re-ran cycle 12 from the sealed snapshot"
    assert event.payload["scientific_scope"] == "DEGRADED_VALID"
    assert event.payload["kind"] == "RESTART"


@pytest.mark.parametrize("field", ["author", "reason", "effect"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_required_fields_are_refused(
    recorder: InterventionRecorder, field: str, blank: str
) -> None:
    """A blank field documents nothing while looking like an audit trail."""
    with pytest.raises(ConfigurationError, match=f"non-empty {field}"):
        recorder.record(**valid_kwargs(**{field: blank}))  # type: ignore[arg-type]


def test_an_intervention_must_name_what_it_touched(recorder: InterventionRecorder) -> None:
    with pytest.raises(ConfigurationError, match="at least one affected object"):
        recorder.record(**valid_kwargs(affected=()))  # type: ignore[arg-type]


def test_interventions_are_listed_in_chain_order(recorder: InterventionRecorder) -> None:
    recorder.record(**valid_kwargs(reason="first"))  # type: ignore[arg-type]
    recorder.record(**valid_kwargs(reason="second"))  # type: ignore[arg-type]

    reasons = [e.payload["reason"] for e in recorder.all_interventions()]
    assert reasons == ["first", "second"]


def test_scientific_scope_is_explicit_not_inferred(recorder: InterventionRecorder) -> None:
    """A reader needs to know whether an intervention invalidated anything."""
    event = recorder.record(
        **valid_kwargs(  # type: ignore[arg-type]
            kind=InterventionKind.EXCLUSION,
            scientific_scope=FailureScope.CYCLE_INVALID,
        )
    )
    assert event.payload["scientific_scope"] == "CYCLE_INVALID"
