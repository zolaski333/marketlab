"""Exact values, computed by hand, for the whole analysis path (§30.9).

Every other analysis test asserts a *property* — that blocks widen an interval,
that a correction is monotone, that an empty family is refused. Properties
catch structural mistakes and miss arithmetic ones: a Brier score off by a
factor, a difference taken in the wrong direction, a mean divided by the wrong
count would satisfy every one of them.

So this file works one small dataset end to end with numbers a reader can check
on paper, and asserts exact equality rather than approximate. The probabilities
are deliberately exact binary fractions (0.25, 0.5, 0.75), so ``==`` is a fair
test of the arithmetic rather than of floating-point luck.

The worked example
------------------
Two arms, one instrument, three dates, one horizon. The realised direction is
up, up, down.

===========  =========  ==============  ===========
date         arm A      arm B           outcome
===========  =========  ==============  ===========
1            p = 0.50   p = 0.75        up
2            p = 0.50   p = 0.75        up
3            p = 0.50   p = 0.25        down
===========  =========  ==============  ===========

Brier for A is ``(0.5 - y)**2 = 0.25`` on every date, whichever way it went.
Brier for B is ``(0.75 - 1)**2 = 0.0625`` on the two rises and
``(0.25 - 0)**2 = 0.0625`` on the fall. The paired difference ``B - A`` is
therefore ``0.0625 - 0.25 = -0.1875`` on every date, and its mean is
``-0.1875``. Lower Brier is better, so the negative sign means B forecast
better — which is the direction convention
:func:`marketlab.analysis.aggregation.paired_differences` deliberately does not
flip.

With the block length set to the length of the series there is exactly one
block, so every bootstrap replicate is the original series and the interval
collapses onto the estimate. That makes the interval hand-checkable too,
rather than merely reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from marketlab.analysis.aggregation import aggregate_by_date, paired_differences
from marketlab.analysis.bootstrap import block_bootstrap, quantile
from marketlab.analysis.equivalence import EquivalenceVerdict, Rope, equivalence_test
from marketlab.analysis.multiplicity import Correction, adjust
from marketlab.analysis.pairing import pair_scores
from marketlab.analysis.plan import AnalysisPlan, Contrast
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.evaluation.resolution import ForecastResolutionRow, ForecastSource, ResolutionStatus
from marketlab.evaluation.scoring import brier_score

INSTRUMENT = "id-alpha"
HORIZON = 5
OUTCOMES = (True, True, False)
PROBABILITIES = {"A": (0.5, 0.5, 0.5), "B": (0.75, 0.75, 0.25)}

BRIER_A = 0.25
BRIER_B = 0.0625
DIFFERENCE = BRIER_B - BRIER_A  # -0.1875, exactly representable


def at(day: int) -> Instant:
    return instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC) + timedelta(days=day))


def _rows() -> list[ForecastResolutionRow]:
    return [
        ForecastResolutionRow(
            resolution_id=f"{arm}-{day}".ljust(64, "0"),
            forecast_id=f"f-{arm}-{day}".ljust(64, "0"),
            run_id="RUN",
            source=str(ForecastSource.PANEL),
            source_bundle_id="b" * 64,
            arm_id=arm,
            repetition=0,
            instrument_id=INSTRUMENT,
            horizon_sessions=HORIZON,
            probability_up=probabilities[day],
            anchor_at=str(at(day)),
            target_at=str(at(day + HORIZON)),
            status=str(ResolutionStatus.RESOLVED),
            outcome_up=OUTCOMES[day],
            detail="",
            resolved_at=str(at(day + HORIZON)),
        )
        for arm, probabilities in PROBABILITIES.items()
        for day in range(len(OUTCOMES))
    ]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_the_brier_scores_are_the_ones_in_the_table() -> None:
    assert brier_score(0.5, True) == BRIER_A
    assert brier_score(0.5, False) == BRIER_A
    assert brier_score(0.75, True) == BRIER_B
    assert brier_score(0.25, False) == BRIER_B


# ---------------------------------------------------------------------------
# Pairing and aggregation
# ---------------------------------------------------------------------------


def test_pairing_produces_one_cell_per_date_with_both_arms_scored() -> None:
    sample = pair_scores(_rows(), arms=("A", "B"))
    assert len(sample.items) == len(OUTCOMES)
    assert sample.dropped == ()
    for item in sample.items:
        assert item.scores["A"] == BRIER_A
        assert item.scores["B"] == BRIER_B


def test_aggregation_gives_one_number_per_date_and_it_is_the_score_itself() -> None:
    """One item per date, so the mean is that item — which is the simplest
    case in which a wrong divisor would still show."""
    series = aggregate_by_date(pair_scores(_rows(), arms=("A", "B")))
    assert series.dates == tuple(at(day) for day in range(len(OUTCOMES)))
    assert series.series_for("A") == (BRIER_A,) * len(OUTCOMES)
    assert series.series_for("B") == (BRIER_B,) * len(OUTCOMES)
    assert series.items_per_date == (1,) * len(OUTCOMES)


def test_the_paired_difference_is_treatment_minus_control_and_keeps_its_sign() -> None:
    """Negative means the treatment scored *lower*, which for a loss-oriented
    score means better. A helper that silently negated its input would make
    every stored interval mean the opposite of what it says."""
    series = aggregate_by_date(pair_scores(_rows(), arms=("A", "B")))
    assert paired_differences(series, treatment="B", control="A") == (DIFFERENCE,) * 3
    assert paired_differences(series, treatment="A", control="B") == (-DIFFERENCE,) * 3


# ---------------------------------------------------------------------------
# The bootstrap, in the case where it is exactly computable
# ---------------------------------------------------------------------------


def test_one_block_reproduces_the_series_so_the_interval_collapses() -> None:
    result = block_bootstrap([DIFFERENCE] * 3, seed="exact", block_length=3, resamples=50)
    assert result.estimate == DIFFERENCE
    assert set(result.replicates) == {DIFFERENCE}
    assert result.interval(0.95) == (DIFFERENCE, DIFFERENCE)
    assert result.standard_error == 0.0


def test_quantiles_of_a_known_series_are_the_documented_interpolation() -> None:
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert quantile(values, 0.25) == 1.0
    assert quantile(values, 0.5) == 2.0
    assert quantile(values, 0.125) == 0.5


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------


def test_the_worked_example_lands_outside_a_five_percent_rope() -> None:
    result = equivalence_test(
        block_bootstrap([DIFFERENCE] * 3, seed="exact", block_length=3, resamples=50),
        rope=Rope(-0.05, 0.05),
        label="B-vs-A",
    )
    assert result.estimate == DIFFERENCE
    assert result.interval == (DIFFERENCE, DIFFERENCE)
    assert result.verdict is EquivalenceVerdict.DIFFERENT
    assert result.p_two_sided == 0.0
    assert result.confidence == pytest.approx(0.9)


def test_the_same_example_is_equivalent_under_a_wide_enough_rope() -> None:
    """The ROPE is where the scientific judgement lives: the same data is a
    meaningful effect or a negligible one depending on a number chosen in
    advance, and nowhere else."""
    result = equivalence_test(
        block_bootstrap([DIFFERENCE] * 3, seed="exact", block_length=3, resamples=50),
        rope=Rope(-0.5, 0.5),
        label="B-vs-A",
    )
    assert result.verdict is EquivalenceVerdict.EQUIVALENT


# ---------------------------------------------------------------------------
# Multiplicity, on published worked examples
# ---------------------------------------------------------------------------


def test_holm_on_a_four_test_family() -> None:
    """p = 0.01, 0.02, 0.03, 0.04 over four tests. Holm multiplies by 4, 3, 2,
    1 and then enforces monotonicity: 0.04, 0.06, 0.06, 0.06."""
    adjusted = {
        entry.label: entry.adjusted_p
        for entry in adjust({"a": 0.01, "b": 0.02, "c": 0.03, "d": 0.04}, method=Correction.HOLM)
    }
    assert adjusted["a"] == pytest.approx(0.04)
    assert adjusted["b"] == pytest.approx(0.06)
    assert adjusted["c"] == pytest.approx(0.06)
    assert adjusted["d"] == pytest.approx(0.06)


def test_benjamini_hochberg_on_the_same_family() -> None:
    """BH divides by rank: 4/1, 4/2, 4/3, 4/4 gives 0.04 throughout."""
    adjusted = {
        entry.label: entry.adjusted_p
        for entry in adjust(
            {"a": 0.01, "b": 0.02, "c": 0.03, "d": 0.04},
            method=Correction.BENJAMINI_HOCHBERG,
        )
    }
    assert all(value == pytest.approx(0.04) for value in adjusted.values())


# ---------------------------------------------------------------------------
# The whole plan, on the same numbers
# ---------------------------------------------------------------------------


def test_the_plan_reproduces_the_hand_computed_result() -> None:
    report = AnalysisPlan(
        rope=Rope(-0.05, 0.05),
        contrasts=(Contrast("B", "A", "worked example"),),  # type: ignore[arg-type]
        horizons=(HORIZON,),
        block_length=3,
        resamples=50,
    ).run(_rows())

    (comparison,) = report.comparisons
    assert comparison.dates == 3
    assert comparison.items == 3
    assert comparison.dropped == 0
    assert comparison.completeness == 1.0
    assert comparison.equivalence.estimate == DIFFERENCE
    assert comparison.equivalence.interval == (DIFFERENCE, DIFFERENCE)
    assert comparison.equivalence.verdict is EquivalenceVerdict.DIFFERENT
    assert report.family_size == 1
