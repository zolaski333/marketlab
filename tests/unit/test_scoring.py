"""Tests for scoring resolved forecasts (§21.1)."""

from __future__ import annotations

import pytest

from marketlab.core.failures import ConfigurationError
from marketlab.evaluation.scoring import (
    ScoringRule,
    absolute_error,
    brier_score,
    calibration_table,
    score,
)


def test_a_certain_and_correct_forecast_scores_zero() -> None:
    assert brier_score(1.0, True) == 0.0
    assert brier_score(0.0, False) == 0.0


def test_a_certain_and_wrong_forecast_scores_the_worst_possible() -> None:
    assert brier_score(1.0, False) == 1.0


def test_total_ignorance_scores_a_quarter() -> None:
    assert brier_score(0.5, True) == 0.25
    assert brier_score(0.5, False) == 0.25


def test_the_brier_score_is_proper_so_honesty_minimises_it() -> None:
    """The property the primary metric is chosen for: against a world that
    rises 70% of the time, no reported probability beats 0.7."""
    truth = 0.7
    expected = {
        reported / 100: truth * brier_score(reported / 100, True)
        + (1 - truth) * brier_score(reported / 100, False)
        for reported in range(0, 101)
    }
    assert min(expected, key=lambda reported: expected[reported]) == pytest.approx(truth)


def test_absolute_error_is_offered_but_is_not_proper() -> None:
    """Stated as a test so nobody promotes it to the primary metric: against
    the same world, reporting certainty beats reporting the truth."""
    truth = 0.7
    honest = truth * absolute_error(0.7, True) + (1 - truth) * absolute_error(0.7, False)
    overconfident = truth * absolute_error(1.0, True) + (1 - truth) * absolute_error(1.0, False)
    assert overconfident < honest


def test_a_probability_outside_the_unit_interval_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="must be in"):
        brier_score(1.4, True)


def test_the_rule_is_named_at_the_call_site() -> None:
    assert score(ScoringRule.BRIER, 0.8, True) == pytest.approx(0.04)
    assert score(ScoringRule.ABSOLUTE_ERROR, 0.8, True) == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_a_perfectly_calibrated_forecaster_has_no_gap() -> None:
    forecasts = [(0.9, index < 9) for index in range(10)]
    (only_bin,) = calibration_table(forecasts)
    assert only_bin.count == 10
    assert only_bin.observed_rate == pytest.approx(0.9)
    assert only_bin.gap == pytest.approx(0.0)


def test_an_overconfident_forecaster_shows_a_positive_gap() -> None:
    (only_bin,) = calibration_table([(0.9, index < 5) for index in range(10)])
    assert only_bin.gap == pytest.approx(0.4)


def test_an_empty_bin_is_omitted_rather_than_reported_as_zero() -> None:
    """ "Nobody forecast in this range" and "everyone in this range was wrong"
    are different findings; a zero would conflate them."""
    table = calibration_table([(0.5, True), (0.5, False)])
    assert len(table) == 1
    assert (table[0].lower, table[0].upper) == (0.4, 0.6)


def test_a_probability_of_exactly_one_lands_in_the_top_bin() -> None:
    """Half-open bins would otherwise drop it entirely — silently discarding
    every certain forecast, which is the population most worth inspecting."""
    (only_bin,) = calibration_table([(1.0, True)])
    assert only_bin.upper == 1.0
    assert only_bin.count == 1


def test_unordered_bin_edges_are_refused() -> None:
    with pytest.raises(ConfigurationError, match="strictly increasing"):
        calibration_table([(0.5, True)], edges=(0.0, 0.6, 0.4, 1.0))
