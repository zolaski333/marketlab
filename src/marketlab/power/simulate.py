"""How often the study would detect an effect it actually has (task #4).

The one design decision that makes this worth doing
----------------------------------------------------
Every replication is analysed by :class:`~marketlab.analysis.plan.AnalysisPlan`
— the same object that will produce the published result, with the same
pairing, the same date-level aggregation, the same moving block bootstrap and
the same TOST. Nothing here re-derives a standard error.

That matters because the usual way a power calculation is wrong is that it
prices a *different* analysis than the one that gets run: a closed-form
t-test's power, quoted for a study that will actually use a block bootstrap
over aggregated dates. Whatever this platform's analysis loses to its own
conservatism is included in these numbers, because these numbers came out of
it.

What is reported, and why each
------------------------------
``power``
    Share of replications the plan called ``DIFFERENT``, under a scenario where
    the arms genuinely differ. This is the number that answers "how long must
    the study run".
``false_positive_rate``
    The same, under a scenario where they do not. Reported alongside power
    always, because a procedure can be made arbitrarily powerful by being
    arbitrarily wrong, and a reader must be able to see that it has not been.
``equivalence_rate``
    Share called ``EQUIVALENT`` under the null. §21.7 requires "no practically
    useful effect" to be reachable; this says whether it is reachable *at this
    duration*, which is a different question from whether the code can express
    it.
``mean_estimate``
    The average paired difference, in the units of the chosen metric. Under a
    known skill gap this is what turns "arm B recovers 10% more of the signal"
    into a number of Brier points — which is the input a **ROPE** needs and
    the reason this simulation exists at all.
``design_effect`` and ``effective_sample_size``
    How much of the nominal item count survives the correlation structure. A
    study with 4 000 panel items and a design effect of 12 has the precision of
    roughly 330 independent ones, and quoting the first figure would be the
    single most misleading thing this repository could publish.

Skipped comparisons
-------------------
A replication whose comparison the plan skipped for want of data is counted
separately and excluded from the rates, rather than being scored as a failure
to detect. The distinction is the same one the plan itself draws.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean, pvariance
from typing import Final

from marketlab.analysis.aggregation import aggregate_by_date, paired_differences
from marketlab.analysis.equivalence import EquivalenceVerdict, Rope
from marketlab.analysis.pairing import PairedSample, RepetitionStatistic, pair_scores
from marketlab.analysis.plan import AnalysisPlan, Contrast, SkippedComparison
from marketlab.core.failures import ConfigurationError
from marketlab.evaluation.scoring import ScoringRule
from marketlab.power.dgp import Scenario, generate_resolutions

__all__ = [
    "DEFAULT_REPLICATIONS",
    "PowerResult",
    "design_effect",
    "run_power",
]

DEFAULT_REPLICATIONS: Final = 200
"""Monte Carlo replications.

Enough that a power of 0.80 has a standard error near 0.03 — fine for choosing
between "60 sessions" and "120 sessions", too coarse to argue about the third
decimal place. Raise it before quoting a figure to two.
"""


@dataclass(frozen=True, slots=True)
class PowerResult:
    """What one scenario, analysed the real way, produced."""

    label: str
    contrast: str
    horizon_sessions: int
    metric: str
    replications: int
    different: int
    equivalent: int
    inconclusive: int
    skipped: int
    mean_estimate: float
    mean_dates: float
    mean_items: float
    design_effect: float

    @property
    def analysed(self) -> int:
        return self.replications - self.skipped

    @property
    def power(self) -> float:
        """Share of analysed replications called DIFFERENT."""
        return self.different / self.analysed if self.analysed else 0.0

    @property
    def equivalence_rate(self) -> float:
        return self.equivalent / self.analysed if self.analysed else 0.0

    @property
    def inconclusive_rate(self) -> float:
        return self.inconclusive / self.analysed if self.analysed else 0.0

    @property
    def effective_sample_size(self) -> float:
        """Independent-equivalent items, after the correlation structure."""
        if self.design_effect <= 0:
            return 0.0
        return self.mean_items / self.design_effect

    def as_payload(self) -> dict[str, str | int | float]:
        return {
            "scenario": self.label,
            "contrast": self.contrast,
            "horizon": self.horizon_sessions,
            "metric": self.metric,
            "replications": self.replications,
            "analysed": self.analysed,
            "power": round(self.power, 4),
            "equivalence_rate": round(self.equivalence_rate, 4),
            "inconclusive_rate": round(self.inconclusive_rate, 4),
            "mean_estimate": round(self.mean_estimate, 6),
            "dates": round(self.mean_dates, 1),
            "items": round(self.mean_items, 1),
            "design_effect": round(self.design_effect, 2),
            "effective_sample_size": round(self.effective_sample_size, 1),
        }


def design_effect(sample: PairedSample, *, treatment: str, control: str) -> float:
    """How much precision the **cross-sectional** correlation costs.

    The ratio of the variance of the date-level mean difference — what the
    analysis actually uses — to the variance an equal number of independent
    items would have given. One means the items within a date were effectively
    independent; two means the nominal item count overstates the information
    twofold.

    Deliberately *not* a measure of the serial dependence that overlapping
    horizons create. That is real, it is larger at 20 sessions than at 1, and
    it is absorbed by the moving block bootstrap rather than by aggregation —
    so it shows up in :attr:`PowerResult.power` and not here. Reporting one
    number for both would suggest the two are handled by the same mechanism,
    and they are not.

    Returns 0.0 when it cannot be computed (a single date, or no variation at
    all), which the caller reports rather than smoothing over.
    """
    items = [
        item.scores[treatment] - item.scores[control]
        for item in sample.items
        if treatment in item.scores and control in item.scores
    ]
    if len(items) < 2:
        return 0.0

    series = aggregate_by_date(sample)
    dates = paired_differences(series, treatment=treatment, control=control)
    if len(dates) < 2:
        return 0.0

    item_variance = pvariance(items)
    if item_variance <= 0:
        return 0.0
    date_variance = pvariance(dates)

    naive_standard_error_squared = item_variance / len(items)
    actual_standard_error_squared = date_variance / len(dates)
    if naive_standard_error_squared <= 0:
        return 0.0
    return actual_standard_error_squared / naive_standard_error_squared


def run_power(
    scenario: Scenario,
    *,
    treatment: str,
    control: str,
    horizon_sessions: int,
    rope: Rope,
    replications: int = DEFAULT_REPLICATIONS,
    rule: ScoringRule = ScoringRule.BRIER,
    statistic: RepetitionStatistic = RepetitionStatistic.MEAN,
    resamples: int = 400,
    alpha: float = 0.05,
    block_length: int | None = None,
) -> PowerResult:
    """Run the real analysis over many worlds and count what it concluded.

    ``resamples`` is deliberately lower than the analysis default: a power
    study runs hundreds of analyses, and the bootstrap noise it adds averages
    out across replications in a way it does not within a single published
    interval.

    Raises:
        ConfigurationError: on a non-positive replication count, or if the
            scenario does not contain both arms.
    """
    if replications < 1:
        raise ConfigurationError(f"replications must be >= 1, got {replications}")
    for arm in (treatment, control):
        if arm not in scenario.skill:
            raise ConfigurationError(f"Scenario has no arm {arm!r}", arm=arm)
    if horizon_sessions not in scenario.horizons:
        raise ConfigurationError(
            f"Scenario does not forecast at horizon {horizon_sessions}; it has "
            f"{list(scenario.horizons)}."
        )

    contrast = Contrast(treatment, control, "power simulation")  # type: ignore[arg-type]
    plan = AnalysisPlan(
        rope=rope,
        contrasts=(contrast,),
        horizons=(horizon_sessions,),
        rule=rule,
        alpha=alpha,
        resamples=resamples,
        block_length=block_length,
        seed=f"{scenario.seed}|power",
    )

    verdicts: list[EquivalenceVerdict] = []
    estimates: list[float] = []
    dates: list[int] = []
    items: list[int] = []
    effects: list[float] = []
    skipped = 0

    for replication in range(replications):
        rows = generate_resolutions(scenario, replication=replication)
        sample = pair_scores(
            rows, arms=(treatment, control), rule=rule, statistic=statistic
        ).restricted_to(horizon_sessions)
        if not sample.items:
            skipped += 1
            continue

        # Paired here so that the stability statistic is expressible, then
        # handed to the plan for everything downstream. Both metrics therefore
        # price the identical aggregation, bootstrap and TOST.
        outcome = plan.compare_prepared(contrast, horizon_sessions, sample)
        if isinstance(outcome, SkippedComparison):
            skipped += 1
            continue
        comparison = outcome

        verdicts.append(comparison.equivalence.verdict)
        estimates.append(comparison.equivalence.estimate)
        dates.append(comparison.dates)
        items.append(comparison.items)
        effects.append(design_effect(sample, treatment=treatment, control=control))

    measured = [value for value in effects if value > 0]
    return PowerResult(
        label=scenario.label(),
        contrast=f"{treatment}-vs-{control}",
        horizon_sessions=horizon_sessions,
        metric=f"{rule}/{statistic}",
        replications=replications,
        different=sum(1 for v in verdicts if v is EquivalenceVerdict.DIFFERENT),
        equivalent=sum(1 for v in verdicts if v is EquivalenceVerdict.EQUIVALENT),
        inconclusive=sum(1 for v in verdicts if v is EquivalenceVerdict.INCONCLUSIVE),
        skipped=skipped,
        mean_estimate=fmean(estimates) if estimates else 0.0,
        mean_dates=fmean(dates) if dates else 0.0,
        mean_items=fmean(items) if items else 0.0,
        design_effect=fmean(measured) if measured else 0.0,
    )
