"""The pre-registered analysis plan (§21).

Everything a study is allowed to decide *before* looking at results lives on
:class:`AnalysisPlan`: which contrasts, at which horizons, scored how, against
which region of practical equivalence, corrected for how many tests. Running
it produces the whole family at once.

That shape is the point. An analysis assembled comparison by comparison lets
the family grow while the results are visible — one more horizon here, one
more contrast there — and the multiplicity correction then applies to whatever
happened to be asked for last. Naming the family up front makes the correction
apply to the family that was actually planned.

Why these five contrasts
------------------------
The design is a crossed 2x2 of (memory, reflection) with matched placebos, so
each contrast answers a distinct question and none is redundant:

* **B vs A** — does raw episodic recall help, with no reflection involved?
* **D vs A** — does distilled strategy help, with no recall involved?
* **C vs A** — do the two together help, which is what a naive study would
  measure and attribute to whichever it happened to name?
* **B vs B-prime** and **C vs C-prime** — is any advantage the *content* of the granted
  material, or merely having been handed some text? A B-vs-A difference that
  survives B-vs-B-prime is about memory; one that does not is about prose.

A comparison with no data is skipped, not scored
------------------------------------------------
If a contrast has no cell both arms resolved, it produces a
:class:`SkippedComparison` naming the reason and takes no part in the
multiplicity family. It is not given a p-value of 1, an estimate of 0, or an
``INCONCLUSIVE`` verdict — each of which would put a fabricated number in a
results table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from marketlab.analysis.aggregation import aggregate_by_date, paired_differences
from marketlab.analysis.bootstrap import DEFAULT_RESAMPLES, block_bootstrap
from marketlab.analysis.equivalence import (
    DEFAULT_ALPHA,
    EquivalenceResult,
    EquivalenceVerdict,
    Rope,
    equivalence_test,
)
from marketlab.analysis.multiplicity import Adjusted, Correction, adjust
from marketlab.analysis.pairing import PairedSample, pair_scores
from marketlab.core.failures import ConfigurationError
from marketlab.evaluation.resolution import ForecastResolutionRow, ForecastSource
from marketlab.evaluation.scoring import ScoringRule
from marketlab.experiments.arms import ArmId
from marketlab.forecasting.panel import DEFAULT_HORIZONS

__all__ = [
    "PRIMARY_CONTRASTS",
    "AnalysisPlan",
    "AnalysisReport",
    "Comparison",
    "Contrast",
    "SkippedComparison",
]


@dataclass(frozen=True, slots=True)
class Contrast:
    """One treatment set against one control, and the question it answers."""

    treatment: ArmId
    control: ArmId
    question: str

    @property
    def label(self) -> str:
        return f"{self.treatment}-vs-{self.control}"


PRIMARY_CONTRASTS: Final[tuple[Contrast, ...]] = (
    Contrast(ArmId.B, ArmId.A, "Does raw episodic recall improve forecast quality?"),
    Contrast(ArmId.D, ArmId.A, "Does distilled strategy alone improve forecast quality?"),
    Contrast(ArmId.C, ArmId.A, "Do recall and reflection together improve forecast quality?"),
    Contrast(ArmId.B, ArmId.B_PRIME, "Is B's effect the content of memory, or merely text?"),
    Contrast(ArmId.C, ArmId.C_PRIME, "Is C's effect the content of its material, or merely text?"),
)


@dataclass(frozen=True, slots=True)
class Comparison:
    """One contrast at one horizon, with its evidence."""

    contrast: Contrast
    horizon_sessions: int
    equivalence: EquivalenceResult
    dates: int
    items: int
    dropped: int

    @property
    def label(self) -> str:
        return f"{self.contrast.label}@{self.horizon_sessions}"

    @property
    def completeness(self) -> float:
        total = self.items + self.dropped
        return self.items / total if total else 0.0


@dataclass(frozen=True, slots=True)
class SkippedComparison:
    """One contrast at one horizon that could not be computed at all."""

    contrast: Contrast
    horizon_sessions: int
    reason: str
    dropped: int

    @property
    def label(self) -> str:
        return f"{self.contrast.label}@{self.horizon_sessions}"


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    """Everything one run of the plan concluded."""

    comparisons: tuple[Comparison, ...]
    skipped: tuple[SkippedComparison, ...]
    adjusted: tuple[Adjusted, ...]
    correction: Correction
    alpha: float

    def verdict_for(self, label: str) -> EquivalenceVerdict | None:
        for comparison in self.comparisons:
            if comparison.label == label:
                return comparison.equivalence.verdict
        return None

    def adjusted_for(self, label: str) -> Adjusted | None:
        for entry in self.adjusted:
            if entry.label == label:
                return entry
        return None

    @property
    def family_size(self) -> int:
        """How many tests the correction was applied over.

        Skipped comparisons are excluded, so this is the number of tests
        actually performed rather than the number planned — reporting the
        larger figure would over-correct and quietly cost the study power.
        """
        return len(self.adjusted)


@dataclass(frozen=True, slots=True)
class AnalysisPlan:
    """Every analysis decision, fixed before the results are visible.

    ``rope`` has no default and never will: a region of practical equivalence
    chosen after seeing the data is not a pre-registration, and one invented
    by this module would be a scientific claim made by a library. See
    ``docs/ROADMAP.md`` open question 3.
    """

    rope: Rope
    contrasts: tuple[Contrast, ...] = PRIMARY_CONTRASTS
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    rule: ScoringRule = ScoringRule.BRIER
    source: ForecastSource = ForecastSource.PANEL
    alpha: float = DEFAULT_ALPHA
    correction: Correction = Correction.HOLM
    resamples: int = DEFAULT_RESAMPLES
    block_length: int | None = None
    seed: str = "marketlab-analysis"

    def __post_init__(self) -> None:
        if not self.contrasts:
            raise ConfigurationError("An analysis plan needs at least one contrast.")
        if not self.horizons:
            raise ConfigurationError("An analysis plan needs at least one horizon.")

    def run(self, resolutions: Sequence[ForecastResolutionRow]) -> AnalysisReport:
        """Execute the whole family and correct it as one."""
        comparisons: list[Comparison] = []
        skipped: list[SkippedComparison] = []

        for contrast in self.contrasts:
            sample = pair_scores(
                resolutions,
                arms=(str(contrast.treatment), str(contrast.control)),
                rule=self.rule,
                source=self.source,
            )
            for horizon in self.horizons:
                outcome = self._compare(contrast, horizon, sample)
                if isinstance(outcome, SkippedComparison):
                    skipped.append(outcome)
                else:
                    comparisons.append(outcome)

        adjusted: tuple[Adjusted, ...] = ()
        if comparisons:
            adjusted = adjust(
                {c.label: c.equivalence.p_two_sided for c in comparisons},
                method=self.correction,
                alpha=self.alpha,
            )
        return AnalysisReport(
            comparisons=tuple(comparisons),
            skipped=tuple(skipped),
            adjusted=adjusted,
            correction=self.correction,
            alpha=self.alpha,
        )

    def _compare(
        self, contrast: Contrast, horizon: int, sample: PairedSample
    ) -> Comparison | SkippedComparison:
        narrowed = sample.restricted_to(horizon)
        if not narrowed.items:
            return SkippedComparison(
                contrast=contrast,
                horizon_sessions=horizon,
                reason=(
                    "no cell was resolved for both arms at this horizon"
                    if narrowed.dropped
                    else "no forecast was made at this horizon"
                ),
                dropped=len(narrowed.dropped),
            )

        series = aggregate_by_date(narrowed)
        differences = paired_differences(
            series, treatment=str(contrast.treatment), control=str(contrast.control)
        )
        bootstrap = block_bootstrap(
            differences,
            seed=f"{self.seed}|{contrast.label}|h={horizon}",
            block_length=self.block_length,
            resamples=self.resamples,
        )
        return Comparison(
            contrast=contrast,
            horizon_sessions=horizon,
            equivalence=equivalence_test(
                bootstrap,
                rope=self.rope,
                label=f"{contrast.label}@{horizon}",
                alpha=self.alpha,
            ),
            dates=len(series),
            items=len(narrowed.items),
            dropped=len(narrowed.dropped),
        )
