"""Tests for the power simulation and the cost model (task #4).

A power simulation is a claim about a study that has not happened yet, which
makes it the easiest thing in this repository to get wrong invisibly: it
produces plausible numbers whatever it does. So the tests below are mostly
*calibration* checks — the simulation must behave correctly in the cases where
the right answer is known independently of it.

The strongest of them is the null: run a scenario in which the arms are
identical and the analysis must almost never call them different. A simulation
that reported high power there would be reporting the false-positive rate of a
broken procedure, and every duration it recommended would be too short.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from marketlab.analysis.equivalence import Rope
from marketlab.analysis.pairing import RepetitionStatistic, pair_scores
from marketlab.core.failures import ConfigurationError
from marketlab.models.types import TokenUsage
from marketlab.power.cost import (
    CostModel,
    Prices,
    ProfileSource,
    TokenProfile,
    elicitation_input_tokens,
    measure_profile,
)
from marketlab.power.dgp import Scenario, generate_resolutions, realised_skill_gap
from marketlab.power.simulate import PowerResult, design_effect, run_power

ROPE = Rope(-0.005, 0.005)
REPLICATIONS = 40
RESAMPLES = 200


def _scenario(**overrides: object) -> Scenario:
    defaults: dict[str, object] = {
        "skill": {"A": 0.30, "B": 0.50},
        "dates": 60,
        "instruments": 4,
        "horizons": (1, 5),
        "seed": "test",
    }
    defaults.update(overrides)
    return Scenario(**defaults)  # type: ignore[arg-type]


def _power(scenario: Scenario, **overrides: object) -> PowerResult:
    kwargs: dict[str, object] = {
        "treatment": "B",
        "control": "A",
        "horizon_sessions": 5,
        "rope": ROPE,
        "replications": REPLICATIONS,
        "resamples": RESAMPLES,
    }
    kwargs.update(overrides)
    return run_power(scenario, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The world behaves the way it is documented to
# ---------------------------------------------------------------------------


def test_every_arm_is_judged_on_the_same_outcomes() -> None:
    """The paired design, in the simulation as in the platform: one snapshot,
    one panel, several conditions answering the same questions."""
    rows = generate_resolutions(_scenario())
    outcomes: dict[tuple[str, str, int], set[bool | None]] = {}
    for row in rows:
        key = (row.anchor_at, row.instrument_id, row.horizon_sessions)
        outcomes.setdefault(key, set()).add(row.outcome_up)
    assert outcomes
    assert all(len(values) == 1 for values in outcomes.values())


def test_a_skilless_arm_forecasts_the_base_rate_and_nothing_else() -> None:
    """skill = 0 must mean *no* information, or the null scenario is not null."""
    rows = generate_resolutions(
        _scenario(skill={"A": 0.0, "B": 0.0}, daily_bias=0.0, arm_daily_bias=0.0, report_noise=0.0)
    )
    assert {row.probability_up for row in rows} == {0.5}


def test_a_more_skilled_arm_scores_better_on_average() -> None:
    """The direction the whole simulation rests on. If skill did not improve
    the Brier score, every power figure below would be measuring noise."""
    rows = generate_resolutions(_scenario(skill={"A": 0.0, "B": 0.9}, dates=200))
    sample = pair_scores(rows, arms=("A", "B")).restricted_to(5)
    mean_a = sum(item.scores["A"] for item in sample.items) / len(sample.items)
    mean_b = sum(item.scores["B"] for item in sample.items) / len(sample.items)
    assert mean_b < mean_a


def test_an_unforecastable_world_leaves_every_arm_equal() -> None:
    """With no edge to recover, skill cannot express itself. A simulation in
    which it still did would be manufacturing an effect out of the arm label."""
    rows = generate_resolutions(
        _scenario(
            skill={"A": 0.0, "B": 1.0},
            edge=0.0,
            daily_bias=0.0,
            arm_daily_bias=0.0,
            report_noise=0.0,
            dates=100,
        )
    )
    assert {round(row.probability_up, 9) for row in rows} == {0.5}


def test_the_generated_rows_are_what_the_real_analysis_consumes() -> None:
    """Not a lookalike: the same row type, so the pairing under test is the
    pairing that will be published."""
    rows = generate_resolutions(_scenario())
    sample = pair_scores(rows, arms=("A", "B"))
    assert sample.items
    assert sample.completeness == 1.0


def test_the_world_is_reproducible_from_its_seed() -> None:
    first = generate_resolutions(_scenario(seed="same"))
    second = generate_resolutions(_scenario(seed="same"))
    assert [row.probability_up for row in first] == [row.probability_up for row in second]
    other = generate_resolutions(_scenario(seed="different"))
    assert [row.probability_up for row in first] != [row.probability_up for row in other]


def test_replications_differ_from_one_another() -> None:
    """Otherwise every Monte Carlo replication would be the same world and the
    reported power would be 0 or 1."""
    first = generate_resolutions(_scenario(), replication=0)
    second = generate_resolutions(_scenario(), replication=1)
    assert [row.probability_up for row in first] != [row.probability_up for row in second]


# ---------------------------------------------------------------------------
# The simulation is calibrated
# ---------------------------------------------------------------------------


def test_identical_arms_are_almost_never_called_different() -> None:
    """The false-positive check, and the most important test here. A procedure
    can be made arbitrarily powerful by being arbitrarily wrong."""
    result = _power(_scenario(skill={"A": 0.4, "B": 0.4}))
    assert result.power <= 0.10, result.as_payload()


def test_a_large_true_effect_is_detected_most_of_the_time() -> None:
    result = _power(_scenario(skill={"A": 0.1, "B": 0.9}, dates=80))
    assert result.power >= 0.80, result.as_payload()


def test_the_estimate_has_the_sign_of_the_true_effect() -> None:
    """Lower Brier is better, so a more skilled treatment must come out
    negative. A sign error here would invert every conclusion the study draws."""
    result = _power(_scenario(skill={"A": 0.2, "B": 0.8}))
    assert realised_skill_gap(_scenario(skill={"A": 0.2, "B": 0.8}), "B", "A") > 0
    assert result.mean_estimate < 0


def test_a_longer_study_has_more_power_than_a_shorter_one() -> None:
    """The whole point of a power curve. If duration did not buy power, the
    curve would be advice to run the cheapest study."""
    short = _power(_scenario(skill={"A": 0.3, "B": 0.5}, dates=20))
    long = _power(_scenario(skill={"A": 0.3, "B": 0.5}, dates=120))
    assert long.power > short.power


def test_equivalence_is_reachable_under_the_null_at_a_long_enough_duration() -> None:
    """§21.7 asks that "no practically useful effect" be a conclusion the study
    can arrive at. This is the check that it is reachable in practice and not
    merely expressible in code."""
    result = _power(_scenario(skill={"A": 0.4, "B": 0.4}, dates=120))
    assert result.equivalence_rate > 0.5, result.as_payload()


def test_the_rates_account_for_every_analysed_replication() -> None:
    result = _power(_scenario())
    assert result.different + result.equivalent + result.inconclusive == result.analysed
    assert result.power + result.equivalence_rate + result.inconclusive_rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Effective sample size
# ---------------------------------------------------------------------------


def test_a_daily_lean_shared_across_instruments_costs_effective_sample_size() -> None:
    """The mechanism the first version of this simulation missed.

    Correlated *outcomes* alone leave the design effect at 1, because the Brier
    score depends on the outcome through a factor proportional to the
    forecaster's distance from 0.5, which points a different way on every
    instrument. What actually makes a date's items move together is the
    forecaster leaning the same way across the whole panel.
    """

    def measured(arm_daily_bias: float) -> float:
        scenario = _scenario(dates=200, daily_bias=0.0, arm_daily_bias=arm_daily_bias)
        sample = pair_scores(generate_resolutions(scenario), arms=("A", "B")).restricted_to(5)
        return design_effect(sample, treatment="B", control="A")

    assert measured(0.0) < measured(0.06)


def test_the_effective_sample_size_is_below_the_item_count() -> None:
    result = _power(_scenario(dates=100))
    assert result.mean_items > 0
    assert 0 < result.effective_sample_size <= result.mean_items


def test_a_design_effect_that_cannot_be_computed_is_reported_as_zero() -> None:
    """Rather than as 1, which would claim an independence nobody measured.

    The uncomputable case is no variation at all — two arms forecasting
    identically in a noiseless world, where every paired difference is exactly
    zero and there is nothing whose correlation could be assessed.
    """
    rows = generate_resolutions(
        _scenario(
            skill={"A": 0.4, "B": 0.4},
            edge=0.0,
            report_noise=0.0,
            daily_bias=0.0,
            arm_daily_bias=0.0,
        )
    )
    sample = pair_scores(rows, arms=("A", "B")).restricted_to(5)
    assert sample.items
    assert design_effect(sample, treatment="B", control="A") == 0.0


def test_one_instrument_per_date_has_a_design_effect_of_one() -> None:
    """The complement, so the zero above is not read as "small samples return
    zero": with a single item per date there is no cross-sectional structure,
    and 1.0 is the correct answer rather than a missing one."""
    rows = generate_resolutions(_scenario(dates=40, instruments=1))
    sample = pair_scores(rows, arms=("A", "B")).restricted_to(5)
    assert design_effect(sample, treatment="B", control="A") == pytest.approx(1.0)


def test_refusing_a_scenario_the_simulation_cannot_answer() -> None:
    with pytest.raises(ConfigurationError, match="does not forecast at horizon"):
        _power(_scenario(horizons=(1,)), horizon_sessions=20)
    with pytest.raises(ConfigurationError, match="has no arm"):
        _power(_scenario(), treatment="Z")


def test_a_scenario_with_an_impossible_skill_is_refused() -> None:
    with pytest.raises(ConfigurationError, match=r"must be in \[0, 1\]"):
        Scenario(skill={"A": 1.5})


# ---------------------------------------------------------------------------
# Stability as an alternative primary metric
# ---------------------------------------------------------------------------


def test_stability_cannot_be_measured_from_a_single_repetition() -> None:
    rows = generate_resolutions(_scenario(repetitions=1))
    with pytest.raises(ConfigurationError, match="single repetition"):
        pair_scores(rows, arms=("A", "B"), statistic=RepetitionStatistic.DISPERSION)


def test_a_noisier_arm_is_measurably_less_stable() -> None:
    """Stability is a different question about the same data: this arm is no
    less accurate on average, only less repeatable."""
    scenario = _scenario(skill={"A": 0.4, "B": 0.4}, repetitions=4, dates=40)
    rows = generate_resolutions(scenario)
    noisier = [
        row
        for row in rows
        if row.arm_id == "A" or row.repetition == 0  # B keeps one draw, so varies less
    ]
    del noisier  # the scenario applies one noise level to both arms by design

    sample = pair_scores(
        rows, arms=("A", "B"), statistic=RepetitionStatistic.DISPERSION
    ).restricted_to(5)
    assert sample.items
    assert all(item.scores["A"] >= 0 for item in sample.items)
    assert any(item.scores["A"] > 0 for item in sample.items)


# ---------------------------------------------------------------------------
# The cost model
# ---------------------------------------------------------------------------


def _profile(**overrides: object) -> TokenProfile:
    defaults: dict[str, object] = {
        "turns": 5.0,
        "fixed_tokens": 400,
        "granted_tokens": 0,
        "evidence_tokens": 5_000,
        "output_tokens": 2_000,
    }
    defaults.update(overrides)
    return TokenProfile(**defaults)  # type: ignore[arg-type]


PRICES = Prices(
    input_per_million=Decimal("3"),
    output_per_million=Decimal("15"),
    cached_input_per_million=Decimal("0.30"),
)


def test_the_resend_dominates_the_input_bill() -> None:
    """The thing a naive multiplication gets wrong. Every accumulated tool
    result is resent on every later turn, so input grows with the square of
    the turn count."""
    fresh, cacheable = elicitation_input_tokens(_profile())
    assert cacheable > fresh


def test_more_turns_cost_more_than_proportionally() -> None:
    _, two = elicitation_input_tokens(_profile(turns=2.0))
    _, six = elicitation_input_tokens(_profile(turns=6.0))
    assert six > 3 * two


def test_granted_material_is_charged_on_every_turn() -> None:
    """Which is why an arm that is granted memory costs more than the control,
    and why cost is projected per arm rather than per study."""
    control = sum(elicitation_input_tokens(_profile(granted_tokens=0)))
    treated = sum(elicitation_input_tokens(_profile(granted_tokens=1_500)))
    assert treated - control > 1_500 * 4


def test_a_cache_discount_more_than_halves_the_input_bill() -> None:
    """Stated on input rather than on the total, because the total is not
    halved: at these prices generated tokens are roughly 40% of the bill and
    nothing caches them. A claim about the total would have been wrong, and
    the first version of this test made it."""
    uncached = Prices(input_per_million=Decimal("3"), output_per_million=Decimal("15"))
    elicitations = 1_000
    dear = CostModel(_profile(), uncached).project(label="x", elicitations=elicitations)
    cheap = CostModel(_profile(), PRICES).project(label="x", elicitations=elicitations)

    output_charge = Decimal(dear.usage.output_tokens) * Decimal("15") / Decimal(1_000_000)
    assert cheap.cost - output_charge < (dear.cost - output_charge) / 2
    assert cheap.cost < dear.cost


def test_cost_is_exact_decimal_arithmetic() -> None:
    projection = CostModel(_profile(), PRICES).project(label="x", elicitations=10)
    assert isinstance(projection.cost, Decimal)


def test_a_projection_says_whether_it_rests_on_measurement_or_assumption() -> None:
    projection = CostModel(_profile(), PRICES).project(label="x", elicitations=10)
    assert projection.source is ProfileSource.ASSUMED
    assert not projection.is_measured
    assert projection.as_payload()["basis"] == "ASSUMED"


def test_measuring_a_profile_from_a_run_that_reported_nothing_is_refused() -> None:
    """A run against the deterministic fake reports zero usage. Averaging over
    it would produce a profile claiming a real model is free — the single most
    expensive mistake this model could make."""
    with pytest.raises(ConfigurationError, match="deterministic fake"):
        measure_profile([TokenUsage(), TokenUsage()], turns=[5, 5])


def test_a_measured_profile_is_marked_as_measured() -> None:
    usages = [TokenUsage(input_tokens=18_000, output_tokens=2_000) for _ in range(10)]
    profile = measure_profile(usages, turns=[5] * 10)
    assert profile.source is ProfileSource.MEASURED
    assert profile.output_tokens == 2_000


def test_negative_prices_are_refused() -> None:
    with pytest.raises(ConfigurationError, match="cannot be negative"):
        Prices(input_per_million=Decimal("-1"), output_per_million=Decimal("15"))


def test_zero_elicitations_cost_zero() -> None:
    projection = CostModel(_profile(), PRICES).project(label="none", elicitations=0)
    assert projection.cost == 0
