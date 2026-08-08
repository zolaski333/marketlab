# ADR-0008 — Aggregate to dates, bootstrap by blocks, decide with a three-valued TOST

- **Status:** accepted
- **Implemented by:** `marketlab.analysis.aggregation`, `marketlab.analysis.bootstrap`, `marketlab.analysis.equivalence`, `marketlab.analysis.plan`
- **Checked by:** `tests/unit/test_analysis.py`, `tests/unit/test_bootstrap.py`, `tests/unit/test_analysis_values.py`, `docs/POWER.md`

## Context

The panel produces a lot of numbers: sessions × instruments × horizons × arms.
A 120-session study with 4 instruments at one horizon has 480 scores per arm.

Treating those 480 as independent observations would be wrong twice over.

**Within a date, the items are correlated.** Every arm reads the same snapshot
and answers about instruments moving in a common market. A day on which
everything rose is one favourable day, not four independent successes.

**Across dates, the windows overlap.** A horizon-20 forecast made on session
*t* and one made on session *t+1* share 19 of their 20 sessions. Their outcomes
are almost the same event. At 60 anchors there are nowhere near 60 independent
pieces of information.

Both inflate precision. A naive standard error would produce confidence
intervals far too narrow and a study that finds effects that are not there.

## Options considered

**A paired t-test over items.** Rejected: assumes independence twice over, and
normality on a bounded score.

**A mixed model with random effects for date and instrument.** The
statistically orthodox answer. Rejected on two grounds. It assumes a covariance
structure that nobody here has evidence for, and — more importantly — it makes
the analysis a fitted model whose specification could be adjusted after seeing
the data. The pre-registration would then have to fix the specification in
detail, and any convergence failure would become a live decision point mid-analysis.

**Cluster-robust standard errors by date.** Handles the cross-sectional
correlation, not the serial overlap.

**Newey-West / HAC standard errors.** Handles the serial dependence
parametrically, requires a bandwidth choice, and still assumes asymptotic
normality on a few dozen dates.

**Aggregate to dates, then a moving block bootstrap over the date series.**
Chosen. Aggregation handles the within-date correlation by construction —
after it there is one number per date, so there is nothing left to correlate
within one. Blocks handle the serial dependence by resampling contiguous runs
of dates rather than individual ones, which preserves the overlap structure
without modelling it.

## Decision

The pipeline is fixed, in this order, and is not a menu:

1. **Pair** on `(date, instrument, horizon)`, restricted to the imposed panel
   ([ADR-0002](0002-imposed-panel-as-unit-of-analysis.md)), complete cases only
   ([ADR-0010](0010-complete-cases-only.md)).
2. **Aggregate** to one paired difference per date.
3. **Moving block bootstrap** over the date series, block length
   `round(n^(1/3))` unless overridden, resampling driven by an explicit
   SHA-256 key stream ([ADR-0013](0013-determinism-from-an-explicit-key-stream.md)).
4. **TOST against the ROPE**, read off the bootstrap distribution rather than a
   t-distribution, at a `1 − 2α` interval.
5. **Multiplicity correction** across the whole declared family, Holm by
   default.

### Three verdicts, not two

| verdict | when | meaning |
|---|---|---|
| `EQUIVALENT` | interval lies inside the ROPE | no practically useful effect — a **finding** |
| `DIFFERENT` | interval disjoint from the ROPE | an effect large enough to matter |
| `INCONCLUSIVE` | interval straddles a ROPE boundary | not enough data to say either |

`INCONCLUSIVE` is kept as a distinct verdict rather than collapsed into "no
difference". Collapsing them is how underpowered studies come to claim null
results, and the power simulation shows the distinction is not academic here:
under the null at 60 sessions the plan returns `EQUIVALENT` 23% of the time and
`INCONCLUSIVE` most of the rest. Reporting those as one number would turn a
duration problem into a scientific claim.

Comparisons with no data are **skipped and excluded from the family**, never
given a fabricated p-value — which would inflate the correction and make the
surviving comparisons look stronger than they are.

## Consequences

**The design effect is measured, not assumed.** `marketlab.power.simulate`
reports it: 1.16–1.20 on the paired difference, against 1.35–1.73 measured on a
single arm. That is the paired design demonstrably removing most of the
cross-sectional correlation, and it is the quantity that would have been
invisible under any method that assumed independence.

**Aggregation throws information away.** Four instruments per date become one
number, and a real effect present on one instrument and absent on three is
diluted rather than detected. That is the price of not modelling the
correlation, and it is priced into the power curves.

**The bootstrap's coverage at this sample size is asymptotic and unverified.**
Block bootstrap guarantees are large-sample; a study has a few dozen dates. The
false-positive column in [POWER.md](../POWER.md) is the empirical check —
0.00–0.01 across the grid — and it is the only evidence offered that the
procedure is not anticonservative.

**The block length is a rule of thumb, not an estimate.** Data-driven selectors
exist and depend on an autocorrelation nobody has measured. The value used is
carried on every result, so an interval always says which block length produced
it, and [POWER.md §5](../POWER.md) shows the horizon-20 result survives
quadrupling it (power 0.980 → 0.953, false positive ≤ 0.007).

**One set of distributional assumptions instead of two.** Reading both one-sided
tests off the same bootstrap that produced the interval avoids assuming
normality on top of the resampling.

## What would make us revisit this

- **A panel wide enough that aggregation is wasteful.** With 30 instruments per
  date, discarding within-date structure costs real power, and a mixed model
  with a pre-registered specification becomes worth its complexity.
- **A measured autocorrelation.** Once a real run exists, the block length can
  be estimated rather than guessed — but the estimate must be made on pilot
  data, never on the confirmatory data, or the block length becomes a forking
  path.
- **Any interim analysis.** There is no stopping rule and no alpha spending. A
  study that wanted to stop early would need both, decided here first.
