"""The imposed forecast panel (§15).

Why a panel exists at all
-------------------------
Left to itself, each condition forecasts whatever it chose to look at. Arm C
might produce eight forecasts on the instrument it finds interesting and arm A
two on a different one — and then there is nothing to compare, because the
questions differ. Worse, an arm could look better calibrated purely by
answering only the easy questions, which is selection, not skill.

The panel removes the choice. It is a fixed set of questions, derived
deterministically from the *shared frozen snapshot*, that every condition must
answer. Because it is built from the snapshot rather than per arm, the panel
is identical across arms by construction — the same guarantee, and for the
same reason, as the shared :class:`~marketlab.retrieval.types.RetrievalIndex`
itself.

An unanswered item is data
--------------------------
A condition that produces no probability for a panel item has not simply
returned less; it has failed to answer a question it was required to answer.
That is recorded as ``MISSING_PANEL_ITEM`` (§15.5) rather than dropped, so
"how often did this condition refuse or forget to answer" is measurable
instead of invisible.

This module holds no database session and imports no SQLAlchemy — it is one of
the three decision-path packages ``tests/security/test_decision_path_isolation.py``
enforces that on, and until now the only one that was still empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from marketlab.core.ids import IdKind, derive_id
from marketlab.instruments.types import InstrumentStatus
from marketlab.retrieval.types import EvidenceKind, RetrievalIndex

__all__ = ["DEFAULT_HORIZONS", "PanelItem", "build_panel"]

DEFAULT_HORIZONS: Final[tuple[int, ...]] = (1, 5, 20)
"""Horizons every panel asks about, in sessions.

Three rather than one because horizon is a variable the analysis plan cares
about (open question 1 in ``docs/ROADMAP.md`` lists Brier at 1/5/20 as
candidate primary metrics), and asking only about the one that turns out to
have the least power would be a choice made after the fact.
"""


@dataclass(frozen=True, slots=True)
class PanelItem:
    """One question every condition must answer."""

    item_id: str
    instrument_id: str
    horizon_sessions: int
    question: str

    @property
    def key(self) -> tuple[str, int]:
        """What a model's answer is matched on."""
        return (self.instrument_id, self.horizon_sessions)


def build_panel(
    index: RetrievalIndex, *, horizons: tuple[int, ...] = DEFAULT_HORIZONS
) -> tuple[PanelItem, ...]:
    """Every panel question implied by one frozen snapshot.

    Covers each **active, priced** instrument at each horizon. An instrument
    with no fresh price is excluded rather than asked about: a question nobody
    could answer from the snapshot would be scored as a failure to answer,
    which would measure the data feed rather than the condition.

    Deterministic and order-stable, so two arms — and a replay — receive the
    identical panel.
    """
    priced = {
        subject_id
        for evidence in index.evidence_of_kind(EvidenceKind.PRICE_BAR)
        if evidence.as_of == index.cutoff
        for subject_id in evidence.subject_ids
    }
    items: list[PanelItem] = []
    for view in sorted(index.universe, key=lambda candidate: candidate.instrument_id):
        if view.status is not InstrumentStatus.ACTIVE or view.instrument_id not in priced:
            continue
        for horizon in sorted(horizons):
            items.append(
                PanelItem(
                    item_id=derive_id(
                        IdKind.PANEL_ITEM,
                        snapshot_id=index.snapshot_id,
                        instrument_id=view.instrument_id,
                        horizon_sessions=horizon,
                    ),
                    instrument_id=view.instrument_id,
                    horizon_sessions=horizon,
                    question=(
                        f"Will {view.ticker} ({view.instrument_id}) close higher in "
                        f"{horizon} session(s) than it does now?"
                    ),
                )
            )
    return tuple(items)
