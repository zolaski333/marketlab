"""Persisting the imposed forecast panel (§15.4, §20.1).

Why this exists at all, and why it exists *here*
------------------------------------------------
:mod:`marketlab.forecasting.panel` builds the questions and
:mod:`marketlab.agents.panel` elicits the answers, and neither may import
SQLAlchemy — they are on the decision path, and
``tests/security/test_decision_path_isolation.py`` enforces that. So the panel
had no way to be written down, and until this module existed it was a thing the
tests exercised rather than a thing a study produced.

That gap mattered more than it looks. The panel is the *only* artefact on which
arms can be paired: two conditions asked the same question at the same instant
about the same instrument give two numbers that mean the same thing. Free-form
forecasts do not — arm C may forecast three instruments and arm A one, and a
comparison of their scores would then be a comparison of which questions each
chose to answer. §21's paired analysis is built on this table.

An unanswered item is stored, not dropped
-----------------------------------------
``unanswered_count`` and the ``MISSING_PANEL_ITEM`` failures behind it are part
of the record. A condition that answers eleven of twelve questions has *not*
produced a shorter panel; it has produced a panel with a hole in it, and
:mod:`marketlab.analysis.pairing` refuses to average over the hole rather than
quietly scoring eleven items against another arm's twelve.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.agents.panel import PanelAnswer, PanelOutcome
from marketlab.core.canonical import canonical_bytes, canonical_hash
from marketlab.core.clock import Clock
from marketlab.core.failures import AgentFailureKind, ObservedAgentFailure
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant
from marketlab.storage.base import Base, HashStr, InstantStr, ShortStr
from marketlab.storage.blobs import BlobStore

__all__ = [
    "PanelBundleRow",
    "PanelRecord",
    "PanelStore",
    "panel_bundle_id_for",
    "panel_content_hash",
]


def panel_bundle_id_for(decision_bundle_id: str) -> str:
    """One panel per sealed decision, derived from it.

    Deriving from the decision bundle rather than from (run, cycle, arm,
    repetition) again means the two can never disagree about which condition
    they belong to: a panel with no decision behind it is unconstructible.
    """
    return derive_id(IdKind.PANEL_BUNDLE, decision_bundle_id=decision_bundle_id)


class PanelBundleRow(Base):
    """One condition's answers to one cycle's imposed panel."""

    __tablename__ = "panel_bundles"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "cycle_id", "arm_id", "repetition", name="uq_panel_bundles_condition"
        ),
    )

    panel_bundle_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    decision_bundle_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    cycle_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    arm_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)

    as_of: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    sealed_at: Mapped[str] = mapped_column(InstantStr, nullable=False)
    model_id: Mapped[str] = mapped_column(ShortStr, nullable=False)

    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    answered_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unanswered_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_calls_made: Mapped[int] = mapped_column(Integer, nullable=False)
    model_turns: Mapped[int] = mapped_column(Integer, nullable=False)

    content_hash: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    payload_blob_hash: Mapped[str] = mapped_column(HashStr, nullable=False)


@dataclass(frozen=True, slots=True)
class PanelRecord:
    """A sealed panel, as the analysis reads it back."""

    panel_bundle_id: str
    decision_bundle_id: str
    run_id: str
    cycle_id: str
    snapshot_id: str
    arm_id: str
    repetition: int
    as_of: Instant
    model_id: str
    item_count: int
    content_hash: str
    outcome: PanelOutcome

    @property
    def unanswered_count(self) -> int:
        return self.outcome.unanswered_count


def panel_content_hash(outcome: PanelOutcome, *, item_count: int) -> str:
    """Fingerprint of what a condition answered.

    ``item_count`` is inside the hash so that "answered eight of twelve" and
    "answered eight of eight" cannot fingerprint identically — the second is a
    complete panel and the first is not, and an arm comparison keyed on this
    hash must be able to tell them apart.
    """
    return canonical_hash(
        {
            "item_count": item_count,
            "answers": [_answer_to_payload(answer) for answer in outcome.answers],
        }
    )


class PanelStore:
    """Records and reloads sealed panels."""

    __slots__ = ("_blobs", "_clock", "_session")

    def __init__(self, session: Session, clock: Clock, blobs: BlobStore) -> None:
        self._session = session
        self._clock = clock
        self._blobs = blobs

    # -- writing -------------------------------------------------------------

    def record(
        self,
        outcome: PanelOutcome,
        *,
        decision_bundle_id: str,
        run_id: str,
        cycle_id: str,
        arm_id: str,
        repetition: int,
        as_of: Instant,
        model_id: str,
        item_count: int,
    ) -> PanelRecord:
        """Seal one condition's panel. Idempotent by derived id (§30.6)."""
        panel_bundle_id = panel_bundle_id_for(decision_bundle_id)
        existing = self.load(panel_bundle_id)
        if existing is not None:
            return existing

        payload_blob = self._blobs.put(canonical_bytes(_outcome_to_payload(outcome)))
        content_hash = panel_content_hash(outcome, item_count=item_count)
        self._session.add(
            PanelBundleRow(
                panel_bundle_id=panel_bundle_id,
                decision_bundle_id=decision_bundle_id,
                run_id=run_id,
                cycle_id=cycle_id,
                snapshot_id=outcome.snapshot_id,
                arm_id=arm_id,
                repetition=repetition,
                as_of=str(as_of),
                sealed_at=str(self._clock.now_instant()),
                model_id=model_id,
                item_count=item_count,
                answered_count=len(outcome.answers),
                unanswered_count=outcome.unanswered_count,
                tool_calls_made=outcome.tool_calls_made,
                model_turns=outcome.model_turns,
                content_hash=content_hash,
                payload_blob_hash=payload_blob.digest,
            )
        )
        self._session.flush()
        return PanelRecord(
            panel_bundle_id=panel_bundle_id,
            decision_bundle_id=decision_bundle_id,
            run_id=run_id,
            cycle_id=cycle_id,
            snapshot_id=outcome.snapshot_id,
            arm_id=arm_id,
            repetition=repetition,
            as_of=as_of,
            model_id=model_id,
            item_count=item_count,
            content_hash=content_hash,
            outcome=outcome,
        )

    # -- reading -------------------------------------------------------------

    def load(self, panel_bundle_id: str) -> PanelRecord | None:
        row = self._session.get(PanelBundleRow, panel_bundle_id)
        return self._rehydrate(row) if row is not None else None

    def for_run(self, run_id: str) -> tuple[PanelRecord, ...]:
        """Every sealed panel of one run, in cycle then arm order."""
        rows = self._session.execute(
            select(PanelBundleRow)
            .where(PanelBundleRow.run_id == run_id)
            .order_by(
                PanelBundleRow.as_of.asc(),
                PanelBundleRow.arm_id.asc(),
                PanelBundleRow.repetition.asc(),
            )
        ).scalars()
        return tuple(self._rehydrate(row) for row in rows)

    def _rehydrate(self, row: PanelBundleRow) -> PanelRecord:
        payload = json.loads(self._blobs.get(row.payload_blob_hash))
        return PanelRecord(
            panel_bundle_id=row.panel_bundle_id,
            decision_bundle_id=row.decision_bundle_id,
            run_id=row.run_id,
            cycle_id=row.cycle_id,
            snapshot_id=row.snapshot_id,
            arm_id=row.arm_id,
            repetition=row.repetition,
            as_of=Instant(row.as_of),
            model_id=row.model_id,
            item_count=row.item_count,
            content_hash=row.content_hash,
            outcome=_payload_to_outcome(row.snapshot_id, payload),
        )


# -- serialisation -------------------------------------------------------------


def _answer_to_payload(answer: PanelAnswer) -> dict[str, Any]:
    return {
        "item_id": answer.item_id,
        "instrument_id": answer.instrument_id,
        "horizon_sessions": answer.horizon_sessions,
        "probability_up": answer.probability_up,
        "cited_evidence_ids": list(answer.cited_evidence_ids),
    }


def _outcome_to_payload(outcome: PanelOutcome) -> dict[str, Any]:
    return {
        "snapshot_id": outcome.snapshot_id,
        "answers": [_answer_to_payload(answer) for answer in outcome.answers],
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


def _payload_to_outcome(snapshot_id: str, payload: Mapping[str, Any]) -> PanelOutcome:
    answers: Sequence[Mapping[str, Any]] = payload["answers"]
    failures: Sequence[Mapping[str, Any]] = payload["failures"]
    return PanelOutcome(
        snapshot_id=snapshot_id,
        answers=tuple(
            PanelAnswer(
                item_id=str(entry["item_id"]),
                instrument_id=str(entry["instrument_id"]),
                horizon_sessions=int(entry["horizon_sessions"]),
                probability_up=float(entry["probability_up"]),
                cited_evidence_ids=tuple(str(e) for e in entry["cited_evidence_ids"]),
            )
            for entry in answers
        ),
        failures=tuple(
            ObservedAgentFailure(
                kind=AgentFailureKind(entry["kind"]),
                detail=str(entry["detail"]),
                occurred_at=Instant(str(entry["occurred_at"])),
                context=dict(entry["context"]),
            )
            for entry in failures
        ),
        tool_calls_made=int(payload["tool_calls_made"]),
        model_turns=int(payload["model_turns"]),
    )
