"""A world in which the true effect is known (task #4).

A power simulation is only worth the paper it is written on if the data it
generates has the dependence structure of the real thing. Two features of this
study destroy naive power calculations, and both are produced here **by
construction** rather than imposed as a correlation parameter:

**Cross-sectional correlation.** Every instrument on one date shares a market
factor. A condition that leans bullish is right about all of them at once, or
wrong about all of them at once. Treating panel items as independent
observations is how a study reports a standard error several times too small.

**Overlapping horizons.** A 5-session forecast made on Monday and one made on
Tuesday are judged on windows that share four of their five sessions. Here that
falls out of the construction: the outcome for anchor ``d`` at horizon ``h`` is
the sign of the summed increments over ``(d, d+h]``, and consecutive anchors
share ``h-1`` of them. Nothing declares the resulting autocorrelation; it is
whatever that overlap implies.

How skill is parameterised
--------------------------
There is a real, knowable edge in this world: a signal ``x`` visible at the
decision instant which shifts the mean of the coming increments. An **oracle**
that knew ``x`` exactly would report

    p* = Phi(edge * x * sqrt(h) / sigma)

An arm recovers a *fraction* of that edge:

    q = 0.5 + skill * (p* - 0.5) + reporting noise

``skill = 0`` is an arm that always says 0.5 and knows nothing; ``skill = 1``
is the oracle. The treatment effect is a difference in ``skill`` between arms.

This matters more than it looks. Parameterising the effect in *Brier units*
would require assuming the answer — the whole reason the ROPE is undecided is
that nobody knows what a Brier difference of 0.005 means. Parameterising it in
recovered-signal units and letting the Brier difference come out as an
**observable** is what makes this simulation able to inform the ROPE rather
than presuppose it.

Everything else is shared
-------------------------
Every arm sees the same ``p*`` and is judged on the same realised outcomes,
because that is the real design: one frozen snapshot, one imposed panel, six
conditions answering the same questions. The pairing is genuine, not assumed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from marketlab.core.failures import ConfigurationError
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.core.rng import DeterministicRng
from marketlab.evaluation.resolution import ForecastResolutionRow, ForecastSource, ResolutionStatus

__all__ = ["Scenario", "at", "generate_resolutions", "realised_skill_gap"]

_EPOCH: Final = datetime(2026, 1, 5, 20, 0, tzinfo=UTC)
_PROBABILITY_FLOOR: Final = 0.001
"""Reported probabilities are clipped away from 0 and 1.

Not a fudge: a real elicitation returns a number a model wrote, and the
platform already rejects anything outside [0, 1]. The floor keeps a simulated
arm from claiming a certainty no forecaster states, which would let one
overconfident draw dominate a whole date's Brier.
"""


@dataclass(frozen=True, slots=True)
class Scenario:
    """One world, and how well each arm forecasts in it."""

    skill: Mapping[str, float]
    """Arm id -> fraction of the available edge it recovers, in [0, 1].

    The effect under test is the *difference* between two of these. A scenario
    where every arm has the same skill is the null, and running it is how the
    simulation checks its own false-positive rate rather than assuming one.
    """

    dates: int = 60
    instruments: int = 4
    horizons: tuple[int, ...] = (1, 5, 20)
    repetitions: int = 1

    market_weight: float = 0.6
    """Share of an increment driven by the common factor. Higher means panel
    items on one date are more correlated, and the honest sample size is
    smaller than the item count suggests."""

    edge: float = 0.35
    """How much of the coming drift the signal actually predicts. Zero means
    the world is unforecastable and no arm can differ from any other, however
    skilled — worth being able to express, because it is the null that a
    sceptic would propose."""

    report_noise: float = 0.02
    """Standard deviation of an arm's reporting noise, on the probability
    scale. Drives the stability metric: an arm with more of it is less stable
    across repetitions while being no less accurate on average."""

    daily_bias: float = 0.04
    """Standard deviation of a per-date lean shared by **every** arm.

    Added because the first version of this simulation did not have it, and
    was wrong in a way worth recording: correlated *outcomes* alone do not
    produce correlated *scores*. The Brier score depends on the outcome only
    through a factor proportional to the forecaster's distance from 0.5, and
    with an idiosyncratic signal that factor points in a different direction
    on every instrument, so the correlation cancels. Measured design effect
    was 1.0 at every skill level.

    What actually makes a date's items move together is the forecaster
    leaning the same way on all of them — an agent reading one macro headline
    and turning bullish on the whole panel. That is this term, and it is
    shared across arms because they all read the same snapshot. Being shared,
    it largely cancels in a paired difference, which is precisely the benefit
    the paired design is claimed to have and is now something this simulation
    can demonstrate rather than assert."""

    arm_daily_bias: float = 0.02
    """The part of the daily lean that is the arm's own, and so does *not*
    cancel in a paired comparison. This is what leaves a design effect above 1
    in the difference, and therefore what date-level aggregation is defending
    against."""

    seed: str = "marketlab-power"

    def __post_init__(self) -> None:
        if not self.skill:
            raise ConfigurationError("A scenario needs at least one arm.")
        for arm, value in self.skill.items():
            if not 0.0 <= value <= 1.0:
                raise ConfigurationError(
                    f"skill for {arm} must be in [0, 1], got {value}: it is the fraction "
                    "of the available edge the arm recovers, not a probability."
                )
        if self.dates < 2:
            raise ConfigurationError(f"dates must be >= 2, got {self.dates}")
        if self.instruments < 1:
            raise ConfigurationError(f"instruments must be >= 1, got {self.instruments}")
        if not self.horizons or any(h < 1 for h in self.horizons):
            raise ConfigurationError(f"horizons must all be >= 1, got {list(self.horizons)}")
        if self.repetitions < 1:
            raise ConfigurationError(f"repetitions must be >= 1, got {self.repetitions}")
        if not 0.0 <= self.market_weight <= 1.0:
            raise ConfigurationError(f"market_weight must be in [0, 1], got {self.market_weight}")

    @property
    def arms(self) -> tuple[str, ...]:
        return tuple(sorted(self.skill))

    @property
    def sessions_needed(self) -> int:
        """Increments to simulate: enough for the longest horizon off the last
        anchor, so no forecast is unresolvable for a reason the design did not
        intend."""
        return self.dates + max(self.horizons)

    def label(self) -> str:
        gaps = ", ".join(f"{arm}={self.skill[arm]:.2f}" for arm in self.arms)
        return f"{self.dates}d x {self.instruments}i [{gaps}]"


def _standard_normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def at(index: int) -> Instant:
    """One decision instant per date, a day apart."""
    return instant_from_datetime(_EPOCH + timedelta(days=index))


def generate_resolutions(
    scenario: Scenario, *, replication: int = 0
) -> list[ForecastResolutionRow]:
    """One replication's worth of resolved forecasts, ready for the real analysis.

    Returns rows of exactly the type :mod:`marketlab.analysis.pairing` consumes,
    so the power estimated here is the power of the analysis that will actually
    be published — not of a re-derivation of it.
    """
    rng = DeterministicRng(f"{scenario.seed}|rep={replication}")
    sessions = scenario.sessions_needed

    # Increments. The market factor is shared across instruments on a date,
    # which is what makes a panel's items correlated.
    market = [rng.normal() for _ in range(sessions + 1)]
    idiosyncratic = [
        [rng.normal() for _ in range(sessions + 1)] for _ in range(scenario.instruments)
    ]
    weight = math.sqrt(scenario.market_weight)
    private = math.sqrt(1.0 - scenario.market_weight)

    # The signal visible at each anchor, and the drift it implies.
    signal = [[rng.normal() for _ in range(sessions + 1)] for _ in range(scenario.instruments)]

    # A lean per date: the part every arm shares (one snapshot, one headline)
    # and the part each arm brings of its own.
    shared_lean = [scenario.daily_bias * rng.normal() for _ in range(scenario.dates)]
    arm_lean = {
        arm: [scenario.arm_daily_bias * rng.normal() for _ in range(scenario.dates)]
        for arm in scenario.arms
    }

    increments = [
        [
            weight * market[step] + private * idiosyncratic[instrument][step]
            for step in range(sessions + 1)
        ]
        for instrument in range(scenario.instruments)
    ]

    rows: list[ForecastResolutionRow] = []
    for date_index in range(scenario.dates):
        for instrument in range(scenario.instruments):
            drift = scenario.edge * signal[instrument][date_index]
            for horizon in scenario.horizons:
                realised = sum(
                    increments[instrument][date_index + step] + drift
                    for step in range(1, horizon + 1)
                )
                outcome_up = realised > 0.0
                oracle = _standard_normal_cdf(drift * math.sqrt(horizon))

                for arm in scenario.arms:
                    for repetition in range(scenario.repetitions):
                        reported = 0.5 + scenario.skill[arm] * (oracle - 0.5)
                        reported += shared_lean[date_index] + arm_lean[arm][date_index]
                        reported += scenario.report_noise * rng.normal()
                        reported = min(1.0 - _PROBABILITY_FLOOR, max(_PROBABILITY_FLOOR, reported))
                        rows.append(
                            _row(
                                arm=arm,
                                repetition=repetition,
                                date_index=date_index,
                                instrument=instrument,
                                horizon=horizon,
                                probability=reported,
                                outcome_up=outcome_up,
                            )
                        )
    return rows


def _row(
    *,
    arm: str,
    repetition: int,
    date_index: int,
    instrument: int,
    horizon: int,
    probability: float,
    outcome_up: bool,
) -> ForecastResolutionRow:
    key = f"{arm}-{repetition}-{date_index}-{instrument}-{horizon}"
    return ForecastResolutionRow(
        resolution_id=key.ljust(64, "0"),
        forecast_id=f"f-{key}".ljust(64, "0"),
        run_id="POWER_SIMULATION",
        source=str(ForecastSource.PANEL),
        source_bundle_id="b" * 64,
        arm_id=arm,
        repetition=repetition,
        instrument_id=f"instrument-{instrument}",
        horizon_sessions=horizon,
        probability_up=probability,
        anchor_at=str(at(date_index)),
        target_at=str(at(date_index + horizon)),
        status=str(ResolutionStatus.RESOLVED),
        outcome_up=outcome_up,
        detail="",
        resolved_at=str(at(date_index + horizon)),
    )


def realised_skill_gap(scenario: Scenario, treatment: str, control: str) -> float:
    """The scenario's true skill difference, for labelling a result."""
    missing = [arm for arm in (treatment, control) if arm not in scenario.skill]
    if missing:
        raise ConfigurationError(f"Scenario has no arm(s) {missing}", missing=missing)
    return scenario.skill[treatment] - scenario.skill[control]
