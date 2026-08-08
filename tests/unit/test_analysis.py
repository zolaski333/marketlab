"""Tests for pairing, aggregation, equivalence and multiplicity (§21).

Every case builds :class:`ForecastResolutionRow` objects directly. They are
plain ORM rows and never touch a session here, which keeps the statistics
testable against exactly the data they need — including data that could not
occur, so the guards against it are not vacuous.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from marketlab.analysis.aggregation import aggregate_by_date, mean_of, paired_differences
from marketlab.analysis.bootstrap import BootstrapResult, block_bootstrap
from marketlab.analysis.equivalence import (
    EquivalenceVerdict,
    Rope,
    equivalence_test,
)
from marketlab.analysis.multiplicity import Correction, adjust
from marketlab.analysis.pairing import DropReason, PairedSample, pair_scores
from marketlab.analysis.plan import PRIMARY_CONTRASTS, AnalysisPlan
from marketlab.core.failures import ConfigurationError, IntegrityError
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.evaluation.resolution import (
    ForecastResolutionRow,
    ForecastSource,
    ResolutionStatus,
)
from marketlab.evaluation.scoring import ScoringRule

ALPHA = "id-alpha"
BETA = "id-beta"


def at(day: int) -> Instant:
    return instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC) + timedelta(days=day))


def row(
    *,
    arm: str,
    day: int,
    instrument: str = ALPHA,
    horizon: int = 5,
    probability: float = 0.6,
    outcome_up: bool | None = True,
    status: ResolutionStatus = ResolutionStatus.RESOLVED,
    source: ForecastSource = ForecastSource.PANEL,
    repetition: int = 0,
) -> ForecastResolutionRow:
    return ForecastResolutionRow(
        resolution_id=f"{arm}-{day}-{instrument}-{horizon}-{repetition}".ljust(64, "0"),
        forecast_id="f" * 64,
        run_id="RUN",
        source=str(source),
        source_bundle_id="b" * 64,
        arm_id=arm,
        repetition=repetition,
        instrument_id=instrument,
        horizon_sessions=horizon,
        probability_up=probability,
        anchor_at=str(at(day)),
        target_at=str(at(day + horizon)),
        status=str(status),
        outcome_up=outcome_up if status is ResolutionStatus.RESOLVED else None,
        detail="",
        resolved_at=str(at(day + horizon)),
    )


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def test_a_cell_both_arms_answered_is_paired() -> None:
    sample = pair_scores(
        [row(arm="A", day=0, probability=0.5), row(arm="B", day=0, probability=0.9)],
        arms=("A", "B"),
    )
    assert len(sample.items) == 1
    assert sample.items[0].scores["A"] == pytest.approx(0.25)
    assert sample.items[0].scores["B"] == pytest.approx(0.01)
    assert sample.items[0].outcome_up is True


def test_a_cell_one_arm_never_answered_is_dropped_and_counted() -> None:
    """Complete case, and countably so: an analysis that cannot say how much
    it discarded cannot be checked."""
    sample = pair_scores([row(arm="A", day=0)], arms=("A", "B"))
    assert sample.items == ()
    assert len(sample.dropped) == 1
    assert sample.dropped[0].reason is DropReason.MISSING_ARM
    assert "B" in sample.dropped[0].detail
    assert sample.completeness == 0.0


def test_an_unresolvable_forecast_drops_its_cell_rather_than_scoring_it() -> None:
    sample = pair_scores(
        [row(arm="A", day=0), row(arm="B", day=0, status=ResolutionStatus.UNRESOLVABLE)],
        arms=("A", "B"),
    )
    assert sample.items == ()
    assert sample.dropped[0].reason is DropReason.MISSING_ARM


def test_a_censored_forecast_drops_its_cell_too() -> None:
    sample = pair_scores(
        [
            row(arm="A", day=0),
            row(arm="B", day=0, status=ResolutionStatus.CENSORED_BY_DELISTING),
        ],
        arms=("A", "B"),
    )
    assert sample.items == ()


def test_decision_forecasts_are_not_paired_by_default() -> None:
    """Two arms that chose different instruments produce numbers that are not
    comparable; only the imposed panel guarantees the same question."""
    sample = pair_scores(
        [
            row(arm="A", day=0, source=ForecastSource.DECISION),
            row(arm="B", day=0, source=ForecastSource.DECISION),
        ],
        arms=("A", "B"),
    )
    assert sample.items == ()
    assert sample.dropped == ()


def test_repetitions_of_one_arm_are_averaged_not_stacked() -> None:
    sample = pair_scores(
        [
            row(arm="A", day=0, probability=0.5, repetition=0),
            row(arm="A", day=0, probability=0.9, repetition=1),
            row(arm="B", day=0, probability=0.6, repetition=0),
            row(arm="B", day=0, probability=0.6, repetition=1),
        ],
        arms=("A", "B"),
    )
    assert len(sample.items) == 1
    assert sample.items[0].scores["A"] == pytest.approx((0.25 + 0.01) / 2)


def test_unequal_repetitions_drop_the_cell_rather_than_weighting_arms_unequally() -> None:
    sample = pair_scores(
        [
            row(arm="A", day=0, repetition=0),
            row(arm="A", day=0, repetition=1),
            row(arm="B", day=0, repetition=0),
        ],
        arms=("A", "B"),
    )
    assert sample.items == ()
    assert sample.dropped[0].reason is DropReason.UNEQUAL_REPETITIONS


def test_arms_that_disagree_about_what_happened_are_a_platform_bug() -> None:
    """The realised direction is a property of the world. If two conditions
    were told different things about the same instrument at the same instant,
    resolution is broken and averaging over it would bury the evidence."""
    with pytest.raises(IntegrityError, match="disagree about what happened"):
        pair_scores(
            [row(arm="A", day=0, outcome_up=True), row(arm="B", day=0, outcome_up=False)],
            arms=("A", "B"),
        )


def test_pairing_one_arm_with_itself_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="at least two distinct arms"):
        pair_scores([row(arm="A", day=0)], arms=("A", "A"))


def test_horizons_are_kept_separate() -> None:
    sample = pair_scores(
        [row(arm=arm, day=0, horizon=horizon) for arm in ("A", "B") for horizon in (1, 5, 20)],
        arms=("A", "B"),
    )
    assert sample.horizons() == (1, 5, 20)
    assert len(sample.restricted_to(5).items) == 1


def test_completeness_over_no_candidates_is_refused_rather_than_reported_as_perfect() -> None:
    sample = pair_scores([], arms=("A", "B"))
    with pytest.raises(ConfigurationError, match="No candidate cells"):
        _ = sample.completeness


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _two_arm_sample(days: int = 6, instruments: tuple[str, ...] = (ALPHA, BETA)) -> PairedSample:
    return pair_scores(
        [
            row(arm=arm, day=day, instrument=instrument, probability=probability)
            for day in range(days)
            for instrument in instruments
            for arm, probability in (("A", 0.5), ("B", 0.7))
        ],
        arms=("A", "B"),
    )


def test_aggregation_produces_one_number_per_arm_per_date() -> None:
    series = aggregate_by_date(_two_arm_sample())
    assert len(series) == 6
    assert series.items_per_date == (2,) * 6
    assert len(series.series_for("A")) == 6


def test_cross_sectional_items_collapse_into_one_observation() -> None:
    """Two instruments on one date are one observation, not two: a market-wide
    move would otherwise be counted twice and halve the standard error."""
    one = aggregate_by_date(_two_arm_sample(instruments=(ALPHA,)))
    two = aggregate_by_date(_two_arm_sample(instruments=(ALPHA, BETA)))
    assert len(one) == len(two)
    assert two.items_per_date == (2,) * 6


def test_an_empty_sample_is_refused_rather_than_aggregated() -> None:
    with pytest.raises(ConfigurationError, match="empty paired sample"):
        aggregate_by_date(pair_scores([], arms=("A", "B")))


def test_differences_are_taken_within_each_date() -> None:
    series = aggregate_by_date(_two_arm_sample())
    differences = paired_differences(series, treatment="B", control="A")
    # B forecast 0.7 and A 0.5 against a rise: B scores 0.09, A scores 0.25.
    assert all(value == pytest.approx(0.09 - 0.25) for value in differences)


def test_comparing_an_arm_with_itself_is_refused() -> None:
    series = aggregate_by_date(_two_arm_sample())
    with pytest.raises(ConfigurationError, match="with itself"):
        paired_differences(series, treatment="A", control="A")


def test_an_unknown_arm_is_named_in_the_error() -> None:
    series = aggregate_by_date(_two_arm_sample())
    with pytest.raises(ConfigurationError, match="No series for arm"):
        series.series_for("Z")


def test_the_mean_of_nothing_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="undefined, not zero"):
        mean_of([])


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------


def _test_result(values: list[float]) -> BootstrapResult:
    return block_bootstrap(values, seed="equivalence", block_length=1, resamples=2000)


def test_a_tight_difference_inside_the_rope_is_equivalent() -> None:
    result = equivalence_test(
        _test_result([0.001, -0.001] * 40), rope=Rope(-0.02, 0.02), label="tiny"
    )
    assert result.verdict is EquivalenceVerdict.EQUIVALENT
    assert result.p_tost < 0.05


def test_a_large_difference_outside_the_rope_is_different() -> None:
    result = equivalence_test(
        _test_result([0.30, 0.31, 0.29] * 20), rope=Rope(-0.02, 0.02), label="large"
    )
    assert result.verdict is EquivalenceVerdict.DIFFERENT
    assert result.p_two_sided < 0.05


def test_a_noisy_difference_is_inconclusive_not_equivalent() -> None:
    """An underpowered result must not be reported as evidence of no effect —
    the failure mode equivalence testing exists to prevent."""
    result = equivalence_test(
        _test_result([0.4, -0.4, 0.3, -0.35] * 10), rope=Rope(-0.02, 0.02), label="noisy"
    )
    assert result.verdict is EquivalenceVerdict.INCONCLUSIVE


def test_the_interval_read_is_one_minus_two_alpha() -> None:
    """TOST at alpha spends alpha in each tail; reading a 1-alpha interval
    would make equivalence about twice as easy to claim as it should be."""
    result = equivalence_test(
        _test_result([0.01, -0.01] * 30), rope=Rope(-0.5, 0.5), label="x", alpha=0.05
    )
    assert result.confidence == pytest.approx(0.90)


def test_a_rope_that_excludes_zero_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="must contain zero"):
        Rope(0.01, 0.05)


def test_an_empty_rope_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="non-empty interval"):
        Rope(0.02, 0.02)


def test_an_alpha_of_a_half_is_refused() -> None:
    with pytest.raises(ConfigurationError, match=r"alpha must be in \(0, 0.5\)"):
        equivalence_test(_test_result([0.0] * 10), rope=Rope(-1, 1), label="x", alpha=0.5)


# ---------------------------------------------------------------------------
# Multiplicity
# ---------------------------------------------------------------------------


def test_holm_multiplies_by_the_number_of_tests_still_standing() -> None:
    adjusted = adjust({"a": 0.01, "b": 0.04, "c": 0.03}, method=Correction.HOLM)
    by_label = {entry.label: entry.adjusted_p for entry in adjusted}
    assert by_label["a"] == pytest.approx(0.03)
    assert by_label["c"] == pytest.approx(0.06)
    assert by_label["b"] == pytest.approx(0.06)


def test_holm_is_monotone_in_the_raw_p_values() -> None:
    adjusted = adjust({str(i): i / 100 for i in range(1, 11)}, method=Correction.HOLM)
    values = [entry.adjusted_p for entry in adjusted]
    assert values == sorted(values)


def test_benjamini_hochberg_is_less_conservative_than_holm() -> None:
    p_values = {str(i): i / 100 for i in range(1, 11)}
    holm = {e.label: e.adjusted_p for e in adjust(p_values, method=Correction.HOLM)}
    bh = {e.label: e.adjusted_p for e in adjust(p_values, method=Correction.BENJAMINI_HOCHBERG)}
    assert all(bh[label] <= holm[label] + 1e-12 for label in p_values)
    assert any(bh[label] < holm[label] for label in p_values)


def test_a_single_test_is_unchanged_by_either_correction() -> None:
    for method in Correction:
        (only,) = adjust({"a": 0.02}, method=method)
        assert only.adjusted_p == pytest.approx(0.02)
        assert only.rejected


def test_adjusted_p_values_never_exceed_one() -> None:
    adjusted = adjust({str(i): 0.9 for i in range(10)}, method=Correction.HOLM)
    assert all(entry.adjusted_p <= 1.0 for entry in adjusted)


def test_correcting_an_empty_family_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="empty family"):
        adjust({}, method=Correction.HOLM)


def test_a_p_value_outside_the_unit_interval_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="must be in"):
        adjust({"a": 1.5}, method=Correction.HOLM)


# ---------------------------------------------------------------------------
# The plan as a whole
# ---------------------------------------------------------------------------


def _six_arm_resolutions(days: int = 12) -> list[ForecastResolutionRow]:
    probabilities = {"A": 0.5, "B": 0.5, "C": 0.5, "D": 0.5, "B_PRIME": 0.5, "C_PRIME": 0.5}
    return [
        row(arm=arm, day=day, horizon=horizon, probability=probability)
        for day in range(days)
        for horizon in (1, 5)
        for arm, probability in probabilities.items()
    ]


def _plan(**overrides: object) -> AnalysisPlan:
    defaults: dict[str, object] = {
        "rope": Rope(-0.05, 0.05),
        "horizons": (1, 5),
        "resamples": 400,
        "block_length": 2,
    }
    defaults.update(overrides)
    return AnalysisPlan(**defaults)  # type: ignore[arg-type]


def test_the_plan_runs_every_contrast_at_every_horizon() -> None:
    report = _plan().run(_six_arm_resolutions())
    assert len(report.comparisons) == len(PRIMARY_CONTRASTS) * 2
    assert report.family_size == len(report.comparisons)


def test_identical_arms_come_out_equivalent() -> None:
    """Every arm forecasting 0.5 differs from every other by exactly zero, so
    a correct pipeline must land inside any ROPE containing zero."""
    report = _plan().run(_six_arm_resolutions())
    assert all(
        comparison.equivalence.verdict is EquivalenceVerdict.EQUIVALENT
        for comparison in report.comparisons
    )


def test_a_genuinely_better_arm_comes_out_different() -> None:
    resolutions = [
        row(arm=arm, day=day, horizon=5, probability=probability)
        for day in range(12)
        for arm, probability in (("A", 0.5), ("B", 0.95))
    ]
    report = _plan(contrasts=(PRIMARY_CONTRASTS[0],), horizons=(5,), rope=Rope(-0.02, 0.02)).run(
        resolutions
    )
    (comparison,) = report.comparisons
    assert comparison.equivalence.estimate < 0  # lower Brier is better
    assert comparison.equivalence.verdict is EquivalenceVerdict.DIFFERENT


def test_a_contrast_with_no_data_is_skipped_not_scored() -> None:
    """Never a p-value of 1, an estimate of 0, or an INCONCLUSIVE verdict:
    each would put a fabricated number in a results table."""
    report = _plan(horizons=(1, 5, 20)).run(_six_arm_resolutions())
    skipped = {comparison.horizon_sessions for comparison in report.skipped}
    assert skipped == {20}
    assert all(comparison.horizon_sessions != 20 for comparison in report.comparisons)


def test_a_skipped_comparison_does_not_inflate_the_multiplicity_family() -> None:
    report = _plan(horizons=(1, 5, 20)).run(_six_arm_resolutions())
    assert report.family_size == len(PRIMARY_CONTRASTS) * 2
    assert len(report.skipped) == len(PRIMARY_CONTRASTS)


def test_the_plan_is_reproducible() -> None:
    resolutions = _six_arm_resolutions()
    first = _plan().run(resolutions)
    second = _plan().run(resolutions)
    assert [c.equivalence.interval for c in first.comparisons] == [
        c.equivalence.interval for c in second.comparisons
    ]


def test_a_plan_needs_a_rope_it_cannot_invent_one() -> None:
    with pytest.raises(TypeError):
        AnalysisPlan()  # type: ignore[call-arg]


def test_a_plan_with_no_contrasts_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="at least one contrast"):
        _plan(contrasts=())


def test_the_scoring_rule_travels_with_the_sample() -> None:
    sample = pair_scores([row(arm="A", day=0), row(arm="B", day=0)], arms=("A", "B"))
    assert sample.rule is ScoringRule.BRIER


def test_a_difference_of_exactly_zero_is_not_significant() -> None:
    """Found by running the CLI end to end: six arms that decided identically
    produce a degenerate bootstrap at exactly zero. Computing the upper tail
    as ``1 - P(X <= 0)`` rather than ``P(X >= 0)`` reported p = 0 for them —
    a difference of nothing, called highly significant, and then carried
    through the multiplicity correction as a rejection."""
    result = equivalence_test(_test_result([0.0] * 30), rope=Rope(-0.01, 0.01), label="identical")
    assert result.estimate == 0.0
    assert result.p_two_sided == 1.0
    assert result.verdict is EquivalenceVerdict.EQUIVALENT


def test_a_difference_centred_on_zero_is_not_significant_either() -> None:
    result = equivalence_test(
        _test_result([0.05, -0.05] * 30), rope=Rope(-0.5, 0.5), label="symmetric"
    )
    assert result.p_two_sided > 0.5


def test_the_two_sided_p_still_detects_a_real_shift() -> None:
    """The complement, so the fix above cannot have been a blanket p = 1."""
    result = equivalence_test(
        _test_result([0.30, 0.31, 0.29] * 20), rope=Rope(-0.02, 0.02), label="shifted"
    )
    assert result.p_two_sided < 0.01
