"""Correcting for a family of comparisons (§21.6).

The crossed design does not produce one test. It produces one per treatment
contrast per horizon — B vs A, D vs A, C vs A, plus each placebo control, at
1, 5 and 20 sessions. Fifteen tests at alpha = 0.05 give better than even
odds of at least one spurious "significant" result on data with no effect at all, and
reporting the smallest p of the fifteen without saying how many were run is
the most common way a study overstates itself.

Two methods, for two different questions
----------------------------------------
``HOLM``
    Controls the family-wise error rate: the probability of *any* false
    rejection in the family. Appropriate for the confirmatory contrasts a
    pre-registration names, where one wrong claim is one too many.
``BENJAMINI_HOCHBERG``
    Controls the false discovery rate: the expected share of rejections that
    are false. Appropriate for exploratory scans, where a few false leads
    among many are tolerable.

Neither is the default. :func:`adjust` requires the method, because which
error rate the study controls is a pre-registration decision and a default
would make it an accident of the call site.

Both return adjusted p-values, not just a reject/keep flag, so a reader can
apply their own threshold to the same numbers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from marketlab.core.failures import ConfigurationError

__all__ = ["Adjusted", "Correction", "adjust"]


class Correction(StrEnum):
    """Which error rate the family controls."""

    HOLM = "HOLM"
    BENJAMINI_HOCHBERG = "BENJAMINI_HOCHBERG"


@dataclass(frozen=True, slots=True)
class Adjusted:
    """One test, before and after correction."""

    label: str
    p_value: float
    adjusted_p: float
    rejected: bool


def adjust(
    p_values: Mapping[str, float], *, method: Correction, alpha: float = 0.05
) -> tuple[Adjusted, ...]:
    """Adjust a family of p-values, returned in ascending order of raw p.

    Raises:
        ConfigurationError: on an empty family, an out-of-range alpha, or a
            p-value outside ``[0, 1]``. A family of zero tests needing no
            correction is a call that should not have been made, and silently
            returning nothing would hide it.
    """
    if not p_values:
        raise ConfigurationError(
            "Cannot correct an empty family of tests. If nothing was tested, that is "
            "what the report should say."
        )
    if not 0.0 < alpha < 1.0:
        raise ConfigurationError(f"alpha must be in (0, 1), got {alpha}", alpha=alpha)
    for label, p_value in p_values.items():
        if not 0.0 <= p_value <= 1.0:
            raise ConfigurationError(
                f"p-value for {label!r} must be in [0, 1], got {p_value}", label=label
            )

    ordered = sorted(p_values.items(), key=lambda entry: (entry[1], entry[0]))
    raw = [p_value for _, p_value in ordered]
    adjusted = _holm(raw) if method is Correction.HOLM else _benjamini_hochberg(raw)

    return tuple(
        Adjusted(
            label=label,
            p_value=p_value,
            adjusted_p=adjusted_p,
            rejected=adjusted_p <= alpha,
        )
        for (label, p_value), adjusted_p in zip(ordered, adjusted, strict=True)
    )


def _holm(sorted_p: Sequence[float]) -> list[float]:
    """Step-down: multiply by the number of tests still standing, then enforce
    monotonicity so a later test is never reported as more significant than an
    earlier, smaller one."""
    total = len(sorted_p)
    running = 0.0
    result: list[float] = []
    for position, p_value in enumerate(sorted_p):
        running = max(running, min(1.0, (total - position) * p_value))
        result.append(running)
    return result


def _benjamini_hochberg(sorted_p: Sequence[float]) -> list[float]:
    """Step-up: walk from the largest p downwards, keeping a running minimum
    of ``m/i * p_(i)``."""
    total = len(sorted_p)
    running = 1.0
    result = [0.0] * total
    for position in range(total - 1, -1, -1):
        running = min(running, min(1.0, total * sorted_p[position] / (position + 1)))
        result[position] = running
    return result
