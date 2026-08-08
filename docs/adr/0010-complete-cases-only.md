# ADR-0010 — Complete cases only; imputation is not offered

- **Status:** accepted
- **Implemented by:** `marketlab.analysis.pairing`
- **Checked by:** `tests/unit/test_analysis.py`, `tests/unit/test_analysis_values.py`

## Context

Cells go missing, for at least five distinct reasons:

- a provider outage left one arm with no decision (`CONDITION_MISSING`);
- the model failed to answer a panel item (`MISSING_PANEL_ITEM`);
- the instrument was delisted or the contract expired before the horizon
  elapsed, so no outcome exists;
- the horizon has not elapsed yet;
- the model emitted a probability outside `[0, 1]` and the answer was rejected.

§23.4 requires a paired policy for incomplete cycles. The options are the
familiar ones: drop the pair, drop the whole cycle, or impute.

The trap is that **missingness here is not random**. An arm that fails to
answer is plausibly an arm that found the question hard, and hard questions are
where the arms most differ. Imputing the missing value — with the arm's own
mean, with the other arm's value, with a model-based estimate — invents the
observation most likely to distinguish the treatments, using an assumption
nobody can check.

## Options considered

**Drop the whole cycle if any arm is incomplete.** Rejected as wasteful and
subtly worse: with six arms, one flaky arm discards five good observations, and
which arm is flaky may itself be treatment-related.

**Impute with the arm's own mean.** Rejected. It shrinks the arm towards its
average precisely on the items where it was distinctive, biasing every contrast
towards zero by an amount that depends on how often each arm failed.

**Impute with 0.5** (maximum ignorance). Rejected: 0.5 is not a neutral value
under Brier — it scores 0.25 exactly, which is better than a confident error
and worse than a confident success. Imputing it rewards arms that fail on items
they would have got wrong.

**Multiple imputation under a stated missingness model.** The statistically
respectable answer, and rejected on honesty grounds: it requires a model of
*why* answers go missing, and the plausible model here (missing-not-at-random,
correlated with difficulty and with the treatment) is exactly the one under
which imputation is invalid.

**Complete cases only, with every drop counted and reported by reason.**
Chosen.

## Decision

A cell `(date, instrument, horizon)` enters a comparison **only if every
compared arm has a resolved score for it.** There is no imputation option in
the library and there will not be one.

`PairedSample.dropped_by_reason` counts what was excluded and why, and it is
reported alongside every result. That is what makes §23.4's paired policy
checkable rather than asserted: a reader can see that 95 of 480 cells were
dropped and what caused each one.

Repetitions of one arm are **averaged within a cell, not stacked** — they are
two draws from one condition, not two observations of the world. A cell where
the arms are represented by *different* numbers of repetitions is dropped
rather than weighted unequally.

## Consequences

**No fabricated observation ever enters the analysis.** Whatever else the
result is, it is computed from things that happened.

**Power is lost, and it is measured.** The `effective n` column in
[POWER.md](../POWER.md) shows it directly: a 120-session study nominally has
480 panel items at one horizon and the analysis works with about 385 after
dropping and the design effect. That is the visible price of this decision.

**Differential dropping is a real threat this does not remove.** If arm A
answers 95% of items and arm B answers 80%, the complete-case set is
disproportionately the items *B* found easy — and the comparison is then run on
a subset selected by one arm's behaviour. Complete-case analysis makes this
visible in the drop counts rather than hiding it inside an imputation model,
but it does not fix it. **A large asymmetry in drop rates between arms should
be treated as invalidating the contrast, not as a nuisance**, and there is
currently no automatic threshold that says so — a human reads the counts.

**A cycle with one missing arm still contributes to every other contrast.**
Only the pairs involving the missing arm are lost.

## What would make us revisit this

- **Drop rates above a few percent, or asymmetric between arms.** That would
  make the missingness itself a finding, and the study would need a
  pre-registered rule for when a contrast is abandoned rather than reported.
- **A missingness mechanism that is genuinely ignorable** — for example, drops
  caused only by delisting, which is a property of the instrument and not of
  the arm. Imputation could be defensible there, restricted to that cause, and
  it would need its own record.
- **A pilot showing a real model refuses panel items at a meaningful rate.**
  Then refusal is a primary observation about the model and belongs in the
  results, not only in the drop counts.
