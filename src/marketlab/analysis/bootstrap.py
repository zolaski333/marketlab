"""Moving block bootstrap over a time series (§21.4).

This is a real bootstrap. It resamples, it produces a distribution, and its
interval widens when the data say it should — three properties asserted by
tests in ``tests/unit/test_bootstrap.py``, because the audit that started this
project found a "verifier" that returned success unconditionally, and an
interval nobody checked would be the same defect wearing a different name.

Why blocks
----------
The ordinary bootstrap resamples observations independently, which is only
valid if they *are* independent. A daily score series is not: a condition that
is systematically overconfident is overconfident on Tuesday and again on
Wednesday. Resampling single dates destroys that dependence and returns an
interval far narrower than the truth — the classic way to manufacture a
significant result from an autocorrelated series.

The moving block bootstrap resamples *contiguous runs* of length ``L``
instead. Dependence within a block is preserved; only dependence across blocks
is lost, which is the accepted trade. ``L = 1`` degenerates to the ordinary
i.i.d. bootstrap, which is why it is available and why it is not the default.

Choosing ``L``
--------------
:func:`suggested_block_length` uses ``round(n ** (1/3))``, the standard rule of
thumb. It is a **rule of thumb**, not an estimate: data-driven selectors exist
and depend on the very autocorrelation the study has not measured yet (task
#4). Recorded as such in ``docs/ROADMAP.md``. The value used is carried on the
result, so an interval always says which ``L`` produced it.

Determinism
-----------
Every draw comes from :class:`marketlab.core.rng.DeterministicRng`, seeded
explicitly. Two runs with the same seed produce byte-identical intervals; a
replay recomputes them rather than trusting them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import fmean
from typing import Final

from marketlab.core.failures import ConfigurationError
from marketlab.core.rng import DeterministicRng

__all__ = [
    "DEFAULT_CONFIDENCE",
    "DEFAULT_RESAMPLES",
    "BootstrapResult",
    "block_bootstrap",
    "quantile",
    "suggested_block_length",
]

DEFAULT_RESAMPLES: Final = 10_000
"""Bootstrap replicates.

Large enough that the Monte Carlo error on a 95% interval endpoint is small
next to the sampling error it is estimating, and small enough to run inside a
test suite. Unlike the block length this is not a modelling choice — more is
strictly better, it just costs time.
"""

DEFAULT_CONFIDENCE: Final = 0.95


def suggested_block_length(n: int) -> int:
    """``round(n ** (1/3))``, floored at 1. A rule of thumb, not an estimate."""
    if n < 1:
        raise ConfigurationError(f"A series needs at least one observation, got {n}.", n=n)
    cube_root: float = n ** (1 / 3)
    return max(1, round(cube_root))


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted sequence.

    Written out rather than delegated so that the interpolation rule is part
    of this repository: the several conventions in circulation disagree in the
    tails by exactly the amount that matters for a 95% interval endpoint.
    """
    if not sorted_values:
        raise ConfigurationError("Quantile of an empty sequence is undefined.")
    if not 0.0 <= q <= 1.0:
        raise ConfigurationError(f"q must be in [0, 1], got {q}", q=q)
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """A statistic, its resampled distribution, and how it was produced."""

    estimate: float
    """The statistic on the observed series — never a bootstrap average."""

    replicates: tuple[float, ...]
    """Sorted, so every quantile read off it is a plain index."""

    block_length: int
    resamples: int
    seed_material: str
    observations: int

    def interval(self, confidence: float = DEFAULT_CONFIDENCE) -> tuple[float, float]:
        """Percentile interval at ``confidence``."""
        if not 0.0 < confidence < 1.0:
            raise ConfigurationError(
                f"confidence must be strictly between 0 and 1, got {confidence}",
                confidence=confidence,
            )
        tail = (1.0 - confidence) / 2.0
        return quantile(self.replicates, tail), quantile(self.replicates, 1.0 - tail)

    def proportion_at_most(self, threshold: float) -> float:
        """Share of replicates at or below ``threshold``."""
        return sum(1 for value in self.replicates if value <= threshold) / len(self.replicates)

    def proportion_at_least(self, threshold: float) -> float:
        return sum(1 for value in self.replicates if value >= threshold) / len(self.replicates)

    @property
    def standard_error(self) -> float:
        """Spread of the resampled statistic."""
        mean = fmean(self.replicates)
        variance = sum((value - mean) ** 2 for value in self.replicates) / len(self.replicates)
        return float(variance**0.5)


def block_bootstrap(
    values: Sequence[float],
    *,
    seed: str,
    block_length: int | None = None,
    resamples: int = DEFAULT_RESAMPLES,
    statistic: Callable[[Sequence[float]], float] = fmean,
) -> BootstrapResult:
    """Resample ``values`` in contiguous blocks and recompute ``statistic``.

    Each replicate draws ``ceil(n / L)`` blocks uniformly from the ``n - L + 1``
    overlapping blocks of the series, concatenates them, and truncates to ``n``
    so every replicate has the length of the original.

    Raises:
        ConfigurationError: on an empty series, a non-positive ``resamples``,
            or a block length outside ``1..n``. None of these has a sensible
            fallback: silently clamping ``L`` to the series length would
            report an interval computed under a model the caller did not ask
            for.
    """
    n = len(values)
    if n == 0:
        raise ConfigurationError(
            "Cannot bootstrap an empty series. 'No observations' is a finding to "
            "report, not a distribution to resample."
        )
    if resamples < 1:
        raise ConfigurationError(f"resamples must be >= 1, got {resamples}", resamples=resamples)

    length = suggested_block_length(n) if block_length is None else block_length
    if not 1 <= length <= n:
        raise ConfigurationError(
            f"block_length must be between 1 and the series length {n}, got {length}.",
            block_length=length,
            observations=n,
        )

    starts = n - length + 1
    blocks_per_replicate = -(-n // length)  # ceiling division
    rng = DeterministicRng(f"{seed}|n={n}|L={length}|B={resamples}")

    replicates: list[float] = []
    for _ in range(resamples):
        resampled: list[float] = []
        for start in rng.draws_below(starts, blocks_per_replicate):
            resampled.extend(values[start : start + length])
        replicates.append(statistic(resampled[:n]))

    return BootstrapResult(
        estimate=statistic(list(values)),
        replicates=tuple(sorted(replicates)),
        block_length=length,
        resamples=resamples,
        seed_material=rng.seed_material,
        observations=n,
    )
