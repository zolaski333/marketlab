"""Tests for sealing the imposed panel (§15.4, §20.1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from marketlab.agents.panel import PanelAnswer, PanelOutcome
from marketlab.core.clock import FrozenClock
from marketlab.core.failures import AgentFailureKind, ObservedAgentFailure
from marketlab.core.instants import instant_from_datetime
from marketlab.evaluation.panels import (
    PanelRecord,
    PanelStore,
    panel_bundle_id_for,
    panel_content_hash,
)
from marketlab.storage.blobs import BlobStore

AS_OF = instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC))
BUNDLE = "b" * 64
SNAPSHOT = "s" * 64


def _answer(instrument_id: str = "id-alpha", horizon: int = 5, p: float = 0.6) -> PanelAnswer:
    return PanelAnswer(
        item_id=f"item-{instrument_id}-{horizon}",
        instrument_id=instrument_id,
        horizon_sessions=horizon,
        probability_up=p,
        cited_evidence_ids=("ev-1",),
    )


def _outcome(*answers: PanelAnswer, missing: int = 0) -> PanelOutcome:
    failures = tuple(
        ObservedAgentFailure(
            kind=AgentFailureKind.MISSING_PANEL_ITEM,
            detail=f"no probability for item {index}",
            occurred_at=AS_OF,
            context={"item_id": f"unanswered-{index}"},
        )
        for index in range(missing)
    )
    return PanelOutcome(
        snapshot_id=SNAPSHOT,
        answers=answers,
        failures=failures,
        tool_calls_made=3,
        model_turns=2,
    )


@pytest.fixture
def store(session: Session, clock: FrozenClock, blob_store: BlobStore) -> PanelStore:
    return PanelStore(session, clock, blob_store)


def _record(
    store: PanelStore, outcome: PanelOutcome, *, item_count: int = 2, arm_id: str = "B"
) -> PanelRecord:
    return store.record(
        outcome,
        decision_bundle_id=BUNDLE,
        run_id="RUN",
        cycle_id="c" * 64,
        arm_id=arm_id,
        repetition=0,
        as_of=AS_OF,
        model_id="test-model",
        item_count=item_count,
    )


def test_a_sealed_panel_reads_back_exactly(store: PanelStore) -> None:
    outcome = _outcome(_answer(), _answer("id-beta", 1, 0.42))
    record = _record(store, outcome)

    reloaded = store.load(record.panel_bundle_id)
    assert reloaded is not None
    assert reloaded.outcome == outcome
    assert reloaded.arm_id == "B"
    assert reloaded.as_of == AS_OF


def test_recording_twice_seals_once(store: PanelStore) -> None:
    """§30.6: a resumed cycle must not produce a second panel for a condition
    that already has one."""
    first = _record(store, _outcome(_answer()))
    second = _record(store, _outcome(_answer("id-beta")))
    assert first.panel_bundle_id == second.panel_bundle_id
    assert second.outcome.answers[0].instrument_id == "id-alpha"


def test_the_panel_is_identified_by_the_decision_it_followed(store: PanelStore) -> None:
    record = _record(store, _outcome(_answer()))
    assert record.panel_bundle_id == panel_bundle_id_for(BUNDLE)
    assert record.decision_bundle_id == BUNDLE


def test_unanswered_items_are_recorded_not_dropped(store: PanelStore) -> None:
    record = _record(store, _outcome(_answer(), missing=3), item_count=4)
    reloaded = store.load(record.panel_bundle_id)
    assert reloaded is not None
    assert reloaded.item_count == 4
    assert reloaded.unanswered_count == 3


def test_an_incomplete_panel_does_not_fingerprint_as_a_complete_one() -> None:
    """Two arms that gave the same eight answers are not equivalent if one was
    asked eight questions and the other twelve."""
    outcome = _outcome(_answer())
    assert panel_content_hash(outcome, item_count=1) != panel_content_hash(outcome, item_count=12)


def test_two_arms_answering_identically_share_a_content_hash() -> None:
    assert panel_content_hash(_outcome(_answer()), item_count=3) == panel_content_hash(
        _outcome(_answer()), item_count=3
    )


def test_a_sealed_panel_cannot_be_edited(session: Session, store: PanelStore) -> None:
    record = _record(store, _outcome(_answer()))
    session.commit()
    with pytest.raises(Exception, match="append-only"):
        session.execute(
            text("UPDATE panel_bundles SET answered_count = 99 WHERE panel_bundle_id = :i"),
            {"i": record.panel_bundle_id},
        )


def test_a_run_reads_back_its_panels_in_a_stable_order(store: PanelStore) -> None:
    for arm in ("D", "A", "C"):
        store.record(
            _outcome(_answer()),
            decision_bundle_id=f"bundle-{arm}".ljust(64, "0"),
            run_id="RUN",
            cycle_id="c" * 64,
            arm_id=arm,
            repetition=0,
            as_of=AS_OF,
            model_id="test-model",
            item_count=1,
        )
    assert [record.arm_id for record in store.for_run("RUN")] == ["A", "C", "D"]
