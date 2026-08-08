"""Tests for the moving block bootstrap and the deterministic RNG (§21.4).

The point of this file is that the bootstrap is real. The audit that started
this project found a replay verifier that returned success unconditionally;
an interval that nobody checked would be the same defect in a different
costume. So these tests assert that resampling actually happens, that the
interval responds to the data, and — the one that matters most — that block
resampling widens the interval on autocorrelated data, which is the entire
reason blocks exist.
"""

from __future__ import annotations

import math
from statistics import fmean

import pytest

from marketlab.analysis.bootstrap import (
    block_bootstrap,
    quantile,
    suggested_block_length,
)
from marketlab.core.failures import ConfigurationError
from marketlab.core.rng import DeterministicRng

RESAMPLES = 2_000


def _white_noise(n: int, *, seed: str = "noise") -> tuple[float, ...]:
    """A series with no serial dependence, drawn deterministically."""
    rng = DeterministicRng(seed)
    return tuple(rng.below(2001) / 1000.0 - 1.0 for _ in range(n))


def _persistent(n: int, *, seed: str = "persistent") -> tuple[float, ...]:
    """A strongly autocorrelated series: a random walk's own increments
    smoothed, so neighbouring values resemble each other."""
    noise = _white_noise(n + 20, seed=seed)
    window = 10
    return tuple(fmean(noise[i : i + window]) for i in range(n))


# ---------------------------------------------------------------------------
# The RNG underneath
# ---------------------------------------------------------------------------


def test_the_same_seed_gives_the_same_draws() -> None:
    assert DeterministicRng("s").draws_below(1000, 20) == DeterministicRng("s").draws_below(
        1000, 20
    )


def test_a_different_seed_gives_different_draws() -> None:
    assert DeterministicRng("a").draws_below(1000, 20) != DeterministicRng("b").draws_below(
        1000, 20
    )


def test_draws_are_spread_over_the_whole_range() -> None:
    """A rejection-sampling bug that clipped the top of the range would still
    look random; this catches it."""
    draws = DeterministicRng("spread").draws_below(10, 5000)
    counts = [draws.count(value) for value in range(10)]
    assert min(counts) > 350, counts


def test_a_bound_of_one_always_draws_zero() -> None:
    assert DeterministicRng("s").draws_below(1, 5) == (0, 0, 0, 0, 0)


def test_a_zero_bound_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="bound must be"):
        DeterministicRng("s").below(0)


def test_shuffling_is_a_permutation() -> None:
    items = tuple(range(20))
    shuffled = DeterministicRng("shuffle").shuffled(items)
    assert sorted(shuffled) == list(items)
    assert shuffled != items


# ---------------------------------------------------------------------------
# Quantiles
# ---------------------------------------------------------------------------


def test_quantiles_interpolate_between_order_statistics() -> None:
    values = [0.0, 1.0, 2.0, 3.0]
    assert quantile(values, 0.0) == 0.0
    assert quantile(values, 1.0) == 3.0
    assert quantile(values, 0.5) == pytest.approx(1.5)


def test_a_quantile_of_one_value_is_that_value() -> None:
    assert quantile([7.0], 0.025) == 7.0


def test_an_empty_quantile_is_refused_rather_than_returning_zero() -> None:
    with pytest.raises(ConfigurationError, match="undefined"):
        quantile([], 0.5)


# ---------------------------------------------------------------------------
# The bootstrap actually resamples
# ---------------------------------------------------------------------------


def test_the_estimate_is_the_statistic_on_the_observed_series() -> None:
    """Not the mean of the replicates: the bootstrap estimates uncertainty
    about the observed statistic, it does not replace it."""
    values = _white_noise(40)
    result = block_bootstrap(values, seed="s", resamples=RESAMPLES)
    assert result.estimate == pytest.approx(fmean(values))


def test_the_replicates_are_not_all_identical() -> None:
    """The plainest possible check that resampling happened at all."""
    result = block_bootstrap(_white_noise(40), seed="s", resamples=RESAMPLES)
    assert len(set(result.replicates)) > RESAMPLES // 10


def test_every_replicate_has_the_length_of_the_original_series() -> None:
    """Checked through the statistic: a replicate padded to a multiple of the
    block length would shift the mean of an unbalanced series."""
    values = tuple(float(i) for i in range(10))
    result = block_bootstrap(values, seed="s", block_length=3, resamples=200, statistic=len)
    assert set(result.replicates) == {10}


def test_the_bootstrap_is_reproducible() -> None:
    values = _white_noise(40)
    first = block_bootstrap(values, seed="same", resamples=RESAMPLES)
    second = block_bootstrap(values, seed="same", resamples=RESAMPLES)
    assert first.replicates == second.replicates


def test_a_different_seed_gives_a_different_distribution() -> None:
    values = _white_noise(40)
    first = block_bootstrap(values, seed="one", resamples=RESAMPLES)
    second = block_bootstrap(values, seed="two", resamples=RESAMPLES)
    assert first.replicates != second.replicates


def test_a_constant_series_has_a_degenerate_interval() -> None:
    """No resampling of identical values can produce spread, so the interval
    must collapse — a bootstrap that reported width here would be inventing
    it."""
    result = block_bootstrap([2.5] * 30, seed="s", resamples=500)
    low, high = result.interval()
    assert (low, high) == (2.5, 2.5)


# ---------------------------------------------------------------------------
# ...and responds to the data
# ---------------------------------------------------------------------------


def test_more_observations_narrow_the_interval() -> None:
    def width(n: int) -> float:
        result = block_bootstrap(_white_noise(n), seed="s", block_length=1, resamples=RESAMPLES)
        low, high = result.interval()
        return high - low

    assert width(400) < width(50) / 2


def test_blocks_widen_the_interval_on_autocorrelated_data() -> None:
    """The reason blocks exist at all.

    On a series where neighbouring values resemble each other, resampling
    single observations destroys the dependence and reports a far narrower
    interval than the truth. If this test ever fails, the block resampling has
    stopped working and every equivalence claim built on it is too confident.
    """
    values = _persistent(200)

    def width(block_length: int) -> float:
        result = block_bootstrap(values, seed="s", block_length=block_length, resamples=RESAMPLES)
        low, high = result.interval()
        return high - low

    assert width(20) > 1.5 * width(1)


def test_blocks_barely_change_the_interval_on_independent_data() -> None:
    """The complement: with no dependence to preserve, blocking costs little.
    Together with the previous test this shows the width difference tracks the
    data rather than the block length."""
    values = _white_noise(200)

    def width(block_length: int) -> float:
        result = block_bootstrap(values, seed="s", block_length=block_length, resamples=RESAMPLES)
        low, high = result.interval()
        return high - low

    assert 0.7 < width(10) / width(1) < 1.4


def test_a_wider_confidence_level_gives_a_wider_interval() -> None:
    result = block_bootstrap(_white_noise(60), seed="s", resamples=RESAMPLES)
    narrow = result.interval(0.5)
    wide = result.interval(0.99)
    assert wide[0] <= narrow[0] and narrow[1] <= wide[1]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_empty_series_is_refused_rather_than_bootstrapped() -> None:
    with pytest.raises(ConfigurationError, match="empty series"):
        block_bootstrap([], seed="s")


def test_a_block_longer_than_the_series_is_refused_not_clamped() -> None:
    """Clamping would silently report an interval computed under a model the
    caller did not ask for."""
    with pytest.raises(ConfigurationError, match="block_length must be"):
        block_bootstrap([1.0, 2.0, 3.0], seed="s", block_length=10)


def test_the_suggested_block_length_is_the_cube_root_rule() -> None:
    assert suggested_block_length(27) == 3
    assert suggested_block_length(1000) == 10
    assert suggested_block_length(1) == 1


def test_the_result_records_how_it_was_produced() -> None:
    """So a stored interval can always say which block length and how many
    replicates it came from."""
    result = block_bootstrap(_white_noise(30), seed="audit", block_length=4, resamples=100)
    assert result.block_length == 4
    assert result.resamples == 100
    assert result.observations == 30
    assert "audit" in result.seed_material
    assert math.isfinite(result.standard_error)
