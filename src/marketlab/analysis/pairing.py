"""Pairing conditions on identical questions (§21.2, §23.4).

The unit of comparison is a **cell**: one instrument, one horizon, one decision
instant. Every condition was asked exactly that question by the imposed panel,
so their answers are comparable in the only sense that matters — same
question, same evidence, same moment, different treatment.

Complete case, and nothing else
-------------------------------
A cell enters the analysis only if *every* compared arm has a resolved score
for it. There is no imputation option and there will not be one: filling a
missing cell with a mean, a 0.5, or the other arm's value invents an
observation, and §23.4's paired policy exists precisely so that missingness is
handled by a rule fixed in advance rather than by whatever the code happened
to do.

What is dropped is therefore *counted*. :class:`PairedSample` carries every
excluded cell with the arms that were missing from it, because an analysis
that cannot say how much data it discarded cannot be checked — and because a
treatment that fails to answer more often is itself a finding, not a nuisance.

Repetitions are averaged, not stacked
-------------------------------------
Two repetitions of arm B in one cell are two draws from the same condition,
not two independent observations of the world. They are averaged into one
number for that arm, which is what makes independent repetitions useful
(they shrink within-condition noise) without letting them inflate the sample
size the bootstrap thinks it has.

...unless stability is the question
------------------------------------
:class:`RepetitionStatistic` lets a cell summarise its repetitions by their
**dispersion** instead of their mean. That turns the same pipeline into a test
of *decision stability under identical bundles* — one of the candidate primary
metrics (open question 1) — rather than of accuracy. Both are loss-oriented,
so a negative paired difference means the treatment did better either way and
no sign convention changes underneath the reader.

Stability needs at least two repetitions and says so: measured from a single
draw it is identically zero for every arm, which would report a perfect tie
rather than an unanswerable question.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import fmean

from marketlab.core.failures import ConfigurationError, IntegrityError
from marketlab.core.instants import Instant
from marketlab.evaluation.resolution import (
    ForecastResolutionRow,
    ForecastSource,
    ResolutionStatus,
)
from marketlab.evaluation.scoring import ScoringRule, score

__all__ = [
    "Cell",
    "DropReason",
    "DroppedCell",
    "PairedItem",
    "PairedSample",
    "RepetitionStatistic",
    "pair_scores",
]


class RepetitionStatistic(StrEnum):
    """How a cell's repetitions are summarised into one number per arm."""

    MEAN = "MEAN"
    """Average score. Measures accuracy. The default."""

    DISPERSION = "DISPERSION"
    """Standard deviation of the reported probabilities. Measures decision
    stability under identical bundles — a different question about the same
    data, and a candidate primary metric in its own right."""


@dataclass(frozen=True, slots=True, order=True)
class Cell:
    """One question, asked of every condition at one instant."""

    anchor_at: Instant
    instrument_id: str
    horizon_sessions: int


class DropReason(StrEnum):
    """Why a cell did not enter the paired analysis."""

    MISSING_ARM = "MISSING_ARM"
    """At least one compared arm has no resolved score here."""

    UNEQUAL_REPETITIONS = "UNEQUAL_REPETITIONS"
    """The arms are not represented by the same number of repetitions, so
    averaging them would weight the conditions unequally."""


@dataclass(frozen=True, slots=True)
class DroppedCell:
    """One excluded cell, and what was wrong with it."""

    cell: Cell
    reason: DropReason
    detail: str


@dataclass(frozen=True, slots=True)
class PairedItem:
    """One cell, with every compared arm's score and what actually happened."""

    cell: Cell
    scores: Mapping[str, float]
    outcome_up: bool


@dataclass(frozen=True, slots=True)
class PairedSample:
    """Every cell on which the compared arms can be set against each other."""

    arms: tuple[str, ...]
    rule: ScoringRule
    items: tuple[PairedItem, ...]
    dropped: tuple[DroppedCell, ...]
    statistic: RepetitionStatistic = RepetitionStatistic.MEAN

    @property
    def completeness(self) -> float:
        """Share of candidate cells that survived pairing."""
        total = len(self.items) + len(self.dropped)
        if total == 0:
            raise ConfigurationError(
                "No candidate cells at all: there is nothing to report a completeness "
                "rate over, and returning 1.0 would claim a perfect one."
            )
        return len(self.items) / total

    def dropped_by_reason(self) -> dict[DropReason, int]:
        tally = dict.fromkeys(DropReason, 0)
        for dropped in self.dropped:
            tally[dropped.reason] += 1
        return tally

    def horizons(self) -> tuple[int, ...]:
        return tuple(sorted({item.cell.horizon_sessions for item in self.items}))

    def restricted_to(self, horizon_sessions: int) -> PairedSample:
        """The same sample narrowed to one horizon.

        Horizons are analysed separately rather than pooled: a 1-session and a
        20-session forecast are different questions with different base rates,
        and averaging their scores would produce a number that answers
        neither.
        """
        return PairedSample(
            arms=self.arms,
            rule=self.rule,
            items=tuple(
                item for item in self.items if item.cell.horizon_sessions == horizon_sessions
            ),
            dropped=tuple(
                drop for drop in self.dropped if drop.cell.horizon_sessions == horizon_sessions
            ),
            statistic=self.statistic,
        )


def pair_scores(
    resolutions: Sequence[ForecastResolutionRow],
    *,
    arms: Sequence[str],
    rule: ScoringRule = ScoringRule.BRIER,
    source: ForecastSource = ForecastSource.PANEL,
    statistic: RepetitionStatistic = RepetitionStatistic.MEAN,
) -> PairedSample:
    """Turn resolved forecasts into cells every compared arm answered.

    Only ``RESOLVED`` rows of ``source`` are considered. ``UNRESOLVABLE``,
    ``CENSORED_BY_DELISTING`` and ``INVALID_SOURCE_DATA`` rows carry no
    outcome, so they cannot be scored — they leave the sample as dropped
    cells, which is how they stay countable.

    Raises:
        ConfigurationError: if fewer than two arms are named. A "paired"
            sample of one arm is not a comparison.
        IntegrityError: if two arms disagree about what happened at one cell.
            The realised direction is a property of the world, identical for
            every condition; a disagreement means resolution itself is broken
            and must not be averaged over.
    """
    if statistic is RepetitionStatistic.DISPERSION:
        counts = {row.repetition for row in resolutions}
        if len(counts) < 2:
            raise ConfigurationError(
                "Decision stability cannot be measured from a single repetition: the "
                "dispersion of one number is zero for every arm, which would report a "
                "perfect tie rather than an unanswerable question.",
                repetitions=len(counts),
            )
    if len(set(arms)) < 2:
        raise ConfigurationError(
            f"Pairing needs at least two distinct arms, got {list(arms)}.", arms=list(arms)
        )
    wanted = tuple(dict.fromkeys(arms))

    by_cell: dict[Cell, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    probabilities: dict[Cell, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    outcomes: dict[Cell, bool] = {}
    candidates: set[Cell] = set()

    for row in resolutions:
        if row.source != str(source) or row.arm_id not in wanted:
            continue
        cell = Cell(Instant(row.anchor_at), row.instrument_id, row.horizon_sessions)
        candidates.add(cell)
        if row.status != str(ResolutionStatus.RESOLVED) or row.outcome_up is None:
            continue
        recorded = outcomes.setdefault(cell, row.outcome_up)
        if recorded != row.outcome_up:
            raise IntegrityError(
                f"Arms disagree about what happened at {cell}: resolution recorded both "
                "outcomes for one instrument, horizon and instant. The realised "
                "direction is a property of the world, not of the condition.",
                instrument_id=cell.instrument_id,
                anchor_at=str(cell.anchor_at),
            )
        by_cell[cell][row.arm_id].append(score(rule, row.probability_up, row.outcome_up))
        probabilities[cell][row.arm_id].append(row.probability_up)

    items: list[PairedItem] = []
    dropped: list[DroppedCell] = []
    for cell in sorted(candidates):
        present = by_cell.get(cell, {})
        missing = [arm for arm in wanted if arm not in present]
        if missing:
            dropped.append(
                DroppedCell(
                    cell,
                    DropReason.MISSING_ARM,
                    f"no resolved score for {', '.join(missing)}",
                )
            )
            continue
        counts = {len(present[arm]) for arm in wanted}
        if len(counts) > 1:
            dropped.append(
                DroppedCell(
                    cell,
                    DropReason.UNEQUAL_REPETITIONS,
                    f"repetitions per arm differ: {sorted(counts)}",
                )
            )
            continue
        if statistic is RepetitionStatistic.MEAN:
            scores = {arm: fmean(present[arm]) for arm in wanted}
        else:
            scores = {arm: _dispersion(probabilities[cell][arm]) for arm in wanted}
        items.append(PairedItem(cell=cell, scores=scores, outcome_up=outcomes[cell]))

    return PairedSample(
        arms=wanted,
        rule=rule,
        items=tuple(items),
        dropped=tuple(dropped),
        statistic=statistic,
    )


def _dispersion(values: Sequence[float]) -> float:
    """Population standard deviation of one arm's answers to one cell."""
    mean = fmean(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return float(variance**0.5)
