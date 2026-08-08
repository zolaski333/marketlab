"""Collapsing a panel to one observation per date (§21.3).

Why not just average every item
-------------------------------
Panel items within one session are not independent. A market-wide move on
Tuesday pushes every instrument's direction the same way, so a condition that
happens to lean bullish scores well on *all* of Tuesday's items at once.
Treating those items as separate observations would count one lucky day as a
dozen, shrink the standard error by roughly the square root of that, and
manufacture significance out of cross-sectional correlation.

Aggregating first makes the **date** the unit of analysis. Whatever dependence
survives is serial — Tuesday still resembles Wednesday — and that is exactly
what the moving block bootstrap in :mod:`marketlab.analysis.bootstrap` is for.
The two steps are a pair: neither alone gives an honest interval.

Paired differences, not two independent series
----------------------------------------------
:func:`paired_differences` subtracts within each date before anything else is
computed. Both arms saw the same evidence and answered the same questions that
day, so the common difficulty of the day cancels — which is the entire benefit
of running the arms on a shared snapshot in the first place, and it is thrown
away by comparing two independently-averaged series.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean

from marketlab.analysis.pairing import PairedSample
from marketlab.core.failures import ConfigurationError
from marketlab.core.instants import Instant

__all__ = ["DateSeries", "aggregate_by_date", "mean_of", "paired_differences"]


@dataclass(frozen=True, slots=True)
class DateSeries:
    """One mean score per arm per date, in chronological order."""

    arms: tuple[str, ...]
    dates: tuple[Instant, ...]
    means: Mapping[str, tuple[float, ...]]
    items_per_date: tuple[int, ...]

    def __len__(self) -> int:
        return len(self.dates)

    def series_for(self, arm: str) -> tuple[float, ...]:
        try:
            return self.means[arm]
        except KeyError:
            raise ConfigurationError(
                f"No series for arm {arm!r}; this aggregation covers {list(self.arms)}.",
                arm=arm,
            ) from None


def aggregate_by_date(sample: PairedSample) -> DateSeries:
    """Average each arm's scores within each date.

    Raises:
        ConfigurationError: if the sample is empty. An empty aggregation is
            not an aggregation of zero — every downstream statistic would
            silently become undefined, and "no data" must surface here rather
            than as a bootstrap of nothing.
    """
    if not sample.items:
        raise ConfigurationError(
            "Cannot aggregate an empty paired sample: every cell was dropped, or "
            "none was ever resolved. Inspect PairedSample.dropped rather than "
            "treating this as a result.",
            dropped=len(sample.dropped),
        )

    grouped: dict[Instant, list[Mapping[str, float]]] = {}
    for item in sample.items:
        grouped.setdefault(item.cell.anchor_at, []).append(item.scores)

    dates = tuple(sorted(grouped))
    return DateSeries(
        arms=sample.arms,
        dates=dates,
        means={
            arm: tuple(fmean(scores[arm] for scores in grouped[date]) for date in dates)
            for arm in sample.arms
        },
        items_per_date=tuple(len(grouped[date]) for date in dates),
    )


def paired_differences(series: DateSeries, *, treatment: str, control: str) -> tuple[float, ...]:
    """``treatment - control`` within each date.

    With a loss-oriented score (Brier), a **negative** difference means the
    treatment did better. That sign convention is deliberately not flipped
    here: a function that silently negated its input would make every stored
    interval mean the opposite of what it says.
    """
    if treatment == control:
        raise ConfigurationError(
            f"Cannot compare arm {treatment!r} with itself; the difference is zero "
            "by construction and would report a spurious equivalence."
        )
    treated = series.series_for(treatment)
    baseline = series.series_for(control)
    return tuple(a - b for a, b in zip(treated, baseline, strict=True))


def mean_of(values: Sequence[float]) -> float:
    """The mean, refusing an empty sequence rather than returning zero."""
    if not values:
        raise ConfigurationError("Mean of an empty sequence is undefined, not zero.")
    return fmean(values)
