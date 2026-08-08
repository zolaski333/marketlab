"""Equivalence testing against a pre-registered ROPE (§21.7).

The requirement this exists for
-------------------------------
§21.7 asks that "no practically useful effect" be a **reachable conclusion**.
An ordinary significance test cannot reach it: failing to reject the null says
only that the study was not powerful enough to tell, which is a statement about
the study rather than about memory or reflection. If the honest answer turns
out to be "persistent memory makes no useful difference", the analysis has to
be able to say so.

That needs a region of practical equivalence — a band around zero inside which
a difference, however real, is too small to matter for this study's purpose.
:class:`Rope` is therefore a **required argument everywhere**. There is no
default and there must not be one: a ROPE chosen after seeing the data is not
a pre-registration, and a ROPE invented by this module would be a scientific
claim made by a library. ``docs/ROADMAP.md`` records it as open question 3,
awaiting the study owner.

TOST, read off the bootstrap
----------------------------
Two one-sided tests: is the difference credibly above the ROPE's lower bound,
and credibly below its upper bound? Both must pass for equivalence. Rather
than assume normality on a few dozen dates, both are read off the block
bootstrap distribution, and the decision is stated in the equivalent and more
legible interval form:

* the interval lies entirely inside the ROPE → ``EQUIVALENT``
* the interval lies entirely outside it → ``DIFFERENT``
* otherwise → ``INCONCLUSIVE``

Three outcomes, not two. "Inconclusive" is a real answer and the most likely
one for a pilot; collapsing it into "no difference" is how underpowered
studies come to claim null results.

The ``1 - 2alpha`` convention
-----------------------------
TOST at level alpha corresponds to a ``1 - 2alpha`` interval, not a
``1 - alpha`` one: each one-sided test spends alpha in one tail. So
``alpha=0.05`` reads a 90% interval. Getting this wrong makes an equivalence
claim roughly twice as easy as it should be, which is why it is spelled out
here rather than left to the caller to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from marketlab.analysis.bootstrap import BootstrapResult
from marketlab.core.failures import ConfigurationError

__all__ = [
    "DEFAULT_ALPHA",
    "EquivalenceResult",
    "EquivalenceVerdict",
    "Rope",
    "equivalence_test",
]

DEFAULT_ALPHA: Final = 0.05


class EquivalenceVerdict(StrEnum):
    """What one comparison concluded."""

    EQUIVALENT = "EQUIVALENT"
    """The whole interval is inside the ROPE: any real effect is too small to matter."""

    DIFFERENT = "DIFFERENT"
    """The whole interval is outside the ROPE: a practically meaningful effect."""

    INCONCLUSIVE = "INCONCLUSIVE"
    """The interval straddles a ROPE boundary. Not evidence of no effect."""


@dataclass(frozen=True, slots=True)
class Rope:
    """A region of practical equivalence, in the units of the difference."""

    lower: float
    upper: float

    def __post_init__(self) -> None:
        if self.lower >= self.upper:
            raise ConfigurationError(
                f"A ROPE must be a non-empty interval, got [{self.lower}, {self.upper}]."
            )
        if not self.lower <= 0.0 <= self.upper:
            raise ConfigurationError(
                f"A ROPE must contain zero, got [{self.lower}, {self.upper}]: it is the "
                "band of differences treated as equivalent to no effect, so excluding "
                "zero would declare 'no difference' to be a meaningful difference."
            )

    def contains(self, value: float) -> bool:
        return self.lower <= value <= self.upper

    @property
    def width(self) -> float:
        return self.upper - self.lower


@dataclass(frozen=True, slots=True)
class EquivalenceResult:
    """One comparison, with everything needed to check it."""

    label: str
    estimate: float
    interval: tuple[float, float]
    rope: Rope
    alpha: float
    confidence: float
    p_lower: float
    """One-sided bootstrap p for H0: the difference is at or below the ROPE floor."""

    p_upper: float
    """One-sided bootstrap p for H0: the difference is at or above the ROPE ceiling."""

    p_two_sided: float
    """Two-sided bootstrap p against a zero difference. Reported alongside
    equivalence, never instead of it: a small p says an effect exists, not
    that it is large enough to care about."""

    verdict: EquivalenceVerdict
    observations: int
    block_length: int

    @property
    def p_tost(self) -> float:
        """TOST p-value: the worse of the two one-sided tests."""
        return max(self.p_lower, self.p_upper)


def equivalence_test(
    result: BootstrapResult, *, rope: Rope, label: str, alpha: float = DEFAULT_ALPHA
) -> EquivalenceResult:
    """Decide equivalence, difference, or neither, from a bootstrap.

    ``rope`` is required; see the module docstring for why there is no
    default.

    Raises:
        ConfigurationError: if ``alpha`` is not in ``(0, 0.5)``. At ``alpha >=
            0.5`` the two one-sided tests overlap and the procedure stops
            controlling anything.
    """
    if not 0.0 < alpha < 0.5:
        raise ConfigurationError(
            f"alpha must be in (0, 0.5) for two one-sided tests, got {alpha}.", alpha=alpha
        )

    confidence = 1.0 - 2.0 * alpha
    low, high = result.interval(confidence)

    if low >= rope.lower and high <= rope.upper:
        verdict = EquivalenceVerdict.EQUIVALENT
    elif high < rope.lower or low > rope.upper:
        verdict = EquivalenceVerdict.DIFFERENT
    else:
        verdict = EquivalenceVerdict.INCONCLUSIVE

    # Both tails are counted with a closed comparison. Using P(X > 0) as the
    # complement of P(X <= 0) instead looks equivalent and is not: on a
    # distribution concentrated at exactly zero — six arms that decided
    # identically, which is what the shipped fake produces — it reports
    # p = 0 and calls a difference of nothing highly significant.
    at_most = result.proportion_at_most(0.0)
    at_least = result.proportion_at_least(0.0)
    return EquivalenceResult(
        label=label,
        estimate=result.estimate,
        interval=(low, high),
        rope=rope,
        alpha=alpha,
        confidence=confidence,
        p_lower=result.proportion_at_most(rope.lower),
        p_upper=result.proportion_at_least(rope.upper),
        p_two_sided=min(1.0, 2.0 * min(at_most, at_least)),
        verdict=verdict,
        observations=result.observations,
        block_length=result.block_length,
    )
