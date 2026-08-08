# ADR-0007 — Brier is the scoring rule; the log score is refused

- **Status:** accepted
- **Implemented by:** `marketlab.evaluation.scoring`
- **Checked by:** `tests/unit/test_scoring.py`, `tests/unit/test_analysis_values.py`

## Context

The study scores probabilistic forecasts. The choice of rule is not a detail:
it determines what "better" means, and different proper rules rank forecasters
differently in finite samples.

A rule must be **proper** — a forecaster must minimise its expected score only
by reporting its true belief. An improper rule rewards strategic
misstatement, and an agent that learned to exploit it would show up as a
memory effect.

Two proper rules are standard. Brier is the squared error, `(p − y)²`. The
logarithmic score is `−log p_y`. The log score is the one most information
theorists prefer, and it punishes overconfidence far harder — which sounds
like exactly what one wants from an LLM agent.

## Options considered

**Log score.** Rejected, for a reason that only shows up in implementation: it
is infinite at `p ∈ {0, 1}`, and real models emit exactly those values. Every
implementation therefore clips — to 1e-15, or 0.01, or whatever the library's
author chose.

**That clip value silently sets how much a single confident error is worth.**
At a clip of 1e-15 one wrong certainty costs ~34 nats and dominates a hundred
sessions of ordinary forecasting; at 0.01 it costs ~4.6 and is merely bad. The
primary metric's behaviour would then be determined by a constant nobody
pre-registered, buried in a scoring function.

That is a pre-registration decision with real consequences, and
`marketlab.evaluation.scoring` will not invent one on the study owner's behalf.
It is refused rather than defaulted — the same reasoning as
[ADR-0009](0009-no-default-rope.md).

**Brier.** Chosen. Bounded on `[0, 1]`, proper, finite everywhere including at
`p ∈ {0, 1}`, and it needs no tuning constant of any kind.

**Continuous ranked probability score.** Rejected as inapplicable: the panel
asks binary direction questions, and CRPS on a binary outcome reduces to Brier.

**Absolute error `|p − y|`.** Offered as a robustness check and **documented as
improper**. A forecaster minimises its expected absolute error by reporting 0
or 1 whenever it believes `p ≠ 0.5`, which is exactly the strategic
misstatement propriety exists to prevent. It is in the library because a result
that survives a switch to a different (even improper) rule is more convincing
than one that does not, and it is labelled so that nobody promotes it to
primary by accident.

## Decision

`ScoringRule` offers `BRIER` and `ABSOLUTE_ERROR`. Brier is the default and the
one [POWER.md](../POWER.md) is computed against. `ABSOLUTE_ERROR` carries the
word *improper* in its own documentation.

The log score is **not offered at all** — not offered with a default clip, not
offered with a required clip parameter. Adding it requires a study owner to
choose the clip and record the choice here, which is the point.

## Consequences

**The metric has no free parameters.** Brier at horizon 5 means one thing.
Nobody can produce a different number from the same data by choosing a
different constant.

**The scale is unintuitive, and that cost is real.** A Brier difference of
0.005 is not interpretable by inspection, which is exactly why the ROPE could
not be chosen by judgement alone and why the power simulation had to
parameterise effects in *recovered signal* and report Brier as an output. The
answer — about 0.07 Brier points per unit of recovered signal at horizon 5 — is
in [POWER.md](../POWER.md) and is a consequence of this decision.

**Overconfidence is punished less than a log score would punish it.** If a
model's failure mode is stating 0.99 and being wrong, Brier costs it ~0.98 per
event while a log score at a 0.01 clip costs it ~4.6. The study is therefore
*less* sensitive to exactly the pathology LLM agents are most suspected of. A
calibration table is reported per arm as a secondary outcome partly to make
that visible, but a table is not a test.

**Brier's dependence on the outcome is proportional to distance from 0.5**, and
this had a consequence nobody anticipated: the first version of the power
simulation measured a design effect of exactly 1.00 because correlated outcomes
do not by themselves correlate Brier scores. See [POWER.md §3](../POWER.md).
That is a property of this rule, not of the simulation.

## What would make us revisit this

- **A study owner who wants the log score and states a clip.** That is a
  legitimate choice; it needs a record here naming the value and the reasoning,
  and the power simulation re-run against it, since the clip changes the
  variance and therefore the required duration.
- **Evidence that overconfidence is the dominant failure mode.** Then a
  metric that punishes it harder is the right primary, and Brier becomes the
  robustness check rather than the other way round.
- **A non-binary panel.** Continuous or multi-category questions would make
  CRPS or the ranked probability score live options, and this record would not
  apply to them.
