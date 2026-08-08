"""Scoring resolved forecasts (§21.1).

Kept separate from :mod:`marketlab.evaluation.resolution` on purpose. What
happened to an instrument is a fact about the world and is stored; what a
probability *deserves* for it depends on a scoring rule, which is a choice the
analysis plan makes. Storing the score next to the fact would make swapping
the rule look like altering the data.

Why the default rule is Brier
-----------------------------
It is strictly **proper**: a forecaster minimises its expected score only by
reporting its true belief, so an arm cannot improve by hedging every answer
towards 0.5 or by being confidently wrong less often than it is confidently
right. It is also bounded on [0, 1], which matters here for a mundane reason:
the block bootstrap resamples means, and an unbounded score would let a single
overconfident answer dominate a whole date's average.

Why the log score is not offered
--------------------------------
The log score is proper too, and better at punishing overconfidence — but it
is infinite at ``p ∈ {0, 1}``, which real models do emit. Every implementation
therefore clips, and the clip value silently sets how much a single confident
error is worth. That is a pre-registration decision with real consequences for
the primary metric, and this module will not invent one. If the study owner
wants a log score, the constant belongs in the pre-registered plan, not in a
default argument here (see ``docs/ROADMAP.md``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import Final

from marketlab.core.failures import ConfigurationError

__all__ = [
    "DEFAULT_CALIBRATION_BINS",
    "CalibrationBin",
    "ScoringRule",
    "absolute_error",
    "brier_score",
    "calibration_table",
    "score",
]

DEFAULT_CALIBRATION_BINS: Final[tuple[float, ...]] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
"""Bin edges for the calibration table.

Five equal bins is a round number, like the tool budget and the recall depth:
enough to see gross miscalibration, coarse enough that a short pilot leaves
each bin non-empty. Recorded as a placeholder in ``docs/ROADMAP.md``.
"""


class ScoringRule(StrEnum):
    """How a probability is scored against what happened."""

    BRIER = "BRIER"
    """Squared error. Strictly proper, bounded on [0, 1]. The default."""

    ABSOLUTE_ERROR = "ABSOLUTE_ERROR"
    """|p - y|. **Improper** — reporting 0 or 1 beats reporting one's true
    belief whenever that belief is off 0.5 — and offered only as a robustness
    check against the primary metric, never as one."""


def _check_probability(probability_up: float) -> None:
    if not 0.0 <= probability_up <= 1.0:
        raise ConfigurationError(
            f"probability_up must be in [0, 1], got {probability_up}. A forecast "
            "outside the interval is recorded as PROBABILITY_OUT_OF_RANGE at "
            "elicitation and must never reach scoring.",
            probability_up=probability_up,
        )


def brier_score(probability_up: float, outcome_up: bool) -> float:
    """Squared error of one probabilistic forecast. Lower is better."""
    _check_probability(probability_up)
    return (probability_up - (1.0 if outcome_up else 0.0)) ** 2


def absolute_error(probability_up: float, outcome_up: bool) -> float:
    """Absolute error of one probabilistic forecast. Lower is better."""
    _check_probability(probability_up)
    return abs(probability_up - (1.0 if outcome_up else 0.0))


def score(rule: ScoringRule, probability_up: float, outcome_up: bool) -> float:
    """Apply a named scoring rule.

    Named rather than defaulted at every call site: which rule produced a
    number is part of the result, and an analysis that cannot say which one it
    used has not measured anything reproducible.
    """
    if rule is ScoringRule.BRIER:
        return brier_score(probability_up, outcome_up)
    return absolute_error(probability_up, outcome_up)


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """One row of a calibration table."""

    lower: float
    upper: float
    count: int
    mean_probability: float
    observed_rate: float

    @property
    def gap(self) -> float:
        """Mean forecast minus realised frequency. Positive is overconfident
        on the upside."""
        return self.mean_probability - self.observed_rate


def calibration_table(
    forecasts: Sequence[tuple[float, bool]],
    *,
    edges: Sequence[float] = DEFAULT_CALIBRATION_BINS,
) -> tuple[CalibrationBin, ...]:
    """Bin forecasts by stated probability and compare to what happened.

    Empty bins are **omitted**, not reported with a zero rate: "no forecast
    fell in [0.8, 1.0]" and "every forecast in [0.8, 1.0] was wrong" are
    different findings and a zero would conflate them.

    Raises:
        ConfigurationError: if ``edges`` is not strictly increasing with at
            least two entries — an unordered edge list silently drops
            forecasts into no bin at all.
    """
    if len(edges) < 2 or any(b <= a for a, b in pairwise(edges)):
        raise ConfigurationError(
            f"Calibration bin edges must be strictly increasing, got {list(edges)}."
        )

    bins: list[CalibrationBin] = []
    for position in range(len(edges) - 1):
        lower, upper = edges[position], edges[position + 1]
        is_last = position == len(edges) - 2
        members = [
            (probability, outcome)
            for probability, outcome in forecasts
            if lower <= probability < upper or (is_last and probability == upper)
        ]
        if not members:
            continue
        bins.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                count=len(members),
                mean_probability=sum(p for p, _ in members) / len(members),
                observed_rate=sum(1 for _, outcome in members if outcome) / len(members),
            )
        )
    return tuple(bins)
