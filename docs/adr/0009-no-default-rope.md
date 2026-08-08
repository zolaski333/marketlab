# ADR-0009 — The analysis plan cannot be constructed without a ROPE

- **Status:** accepted
- **Implemented by:** `marketlab.analysis.equivalence.Rope`, `marketlab.analysis.plan.AnalysisPlan`
- **Checked by:** `tests/unit/test_analysis.py`, `tests/cli/test_cli.py`

## Context

§21.7 requires "no practically useful effect" to be a conclusion the study can
*reach*. That requires a region of practical equivalence: a band around zero
inside which a real difference is too small to matter.

The band is a **scientific judgement about what counts as useful**, and it must
be fixed before the data exist. Chosen afterwards, it is the single easiest
knob to turn: a band that happens to exclude the observed interval turns any
result into `DIFFERENT`, and one that happens to contain it turns any result
into `EQUIVALENT`. Neither choice requires bad faith — only a plausible
argument invented after the fact, which is always available.

The problem is that a library needs *something* to run. Every equivalence
testing package solves this with a default, usually 0.2 standard deviations
following Cohen. On a Brier scale that convention is meaningless, and a default
here would be worse than meaningless: it would be the library making a
scientific claim on the study owner's behalf, in a value nobody wrote down and
nobody would think to question.

## Options considered

**A conventional default (±0.01 Brier).** Rejected. On the Brier scale 0.01 is
a *large* effect — it is roughly two thirds of the entire measured effect of a
0.20 skill gap at horizon 5. A study that inherited it would be declaring almost
everything equivalent and would never know it had made a choice.

**A default derived from the data** (a fraction of the observed pooled
standard deviation). Rejected outright: it is a band chosen after seeing the
data, wearing the costume of a formula.

**A default with a loud warning.** Rejected. Warnings are read once and
silenced. The value would still be in force.

**No default. Make it a required constructor argument.** Chosen.

## Decision

`Rope` has no default bounds and `AnalysisPlan` has no default `rope`. Neither
can be constructed without one. `marketlab analyse` has no default either — it
requires `--rope-lower` and `--rope-upper`, and refuses to run without them.

**No analysis can be run in this repository until the study owner fixes the
band.** The omission is impossible to overlook, because nothing runs.

The band's *value* remains open in [PRE_REGISTRATION.md](../PRE_REGISTRATION.md)
§4 — this record fixes the mechanism, not the number. What the power simulation
supplies is the translation the judgement needs: at horizon 5 the effect scales
at about **0.07 Brier points per unit of recovered signal**, so a ±0.005 band
declares negligible any arm recovering less than about 7 points more of the
available edge. That is an input to the decision, not the decision.

## Consequences

**The band cannot be forgotten.** Not "should be set" — cannot be omitted.

**Every caller is inconvenienced, including the tests.** Every test that
constructs an `AnalysisPlan` states a ROPE, which is verbose. That verbosity is
the mechanism working: a reader of any test can see which band produced its
numbers.

**It does not stop the band being chosen late.** The constructor forces a value
to exist; only the pre-registration and the tagged commit that precedes the
first confirmatory run force it to exist *early*. This is a guard against
oversight, not against bad faith — see
[ADR-0003](0003-masking-is-partial.md) on where analyst-side discipline
actually comes from.

**A study owner without a ROPE cannot get a preliminary look.** That is
deliberate and occasionally painful: there is no "just show me the difference"
mode. The estimate and its bootstrap interval are on the result object, so the
information is available — but only to someone who has already committed to a
band.

## What would make us revisit this

- **Nothing about the mechanism.** A default here is the sort of convenience
  that would quietly undo the reason this repository exists.
- **The band's value**, by contrast, is expected to be revisited: once a pilot
  measures a real model's forecast dispersion, "what counts as useful" may look
  different, and the number in the pre-registration would change — in a commit
  made before the confirmatory run, or not at all.
