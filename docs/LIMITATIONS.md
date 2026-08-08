# Limitations

What this study cannot conclude, collected in one place so nobody has to
assemble the list from the sections that happen to mention them.

This is about **the science**. For what is unfinished in the *code*, see
[ROADMAP.md](ROADMAP.md), which is the only place completeness is claimed.

A useful way to read this file: each entry is something a reviewer would
eventually find. Finding it here first is the point.

## 1. The limitation that comes before all the others

**No result in this repository says anything about memory or reflection yet.**

The only model shipped here is a deterministic fake — a closed-form function of
the closing price that ignores the material each condition is granted. All six
arms are shown different things and decide identically, so an arm comparison
run today is a comparison of nothing.

That is deliberate and pinned by a test. A fake that branched on its injected
context would manufacture a memory effect out of thin air, which is exactly the
defect an audit found in this project's predecessor
([ADR-0017](adr/0017-a-fake-that-ignores-its-context.md)).

What *is* established: the channel is live. A test double that reads its
context produces different decisions per arm through the identical pipeline.

## 2. What the design cannot observe

Each of these bounds the claim, and each is a deliberate choice rather than an
omission.

**Sizing skill.** Every arm sizes at the same fixed fraction of equity. An arm
cannot win by sizing and cannot demonstrate skill at it. If memory's genuine
contribution to a trading agent is knowing when to bet big, this study reports
a null. ([ADR-0014](adr/0014-identical-fixed-fraction-sizing.md))

**Bearish conviction.** Short selling is not modelled. An arm confident that
something will fall can only decline to buy it. Combined with fixed sizing, the
portfolio's expressive range is narrow.

**Question selection.** The imposed panel is the unit of analysis, and its
questions come from the snapshot rather than from any arm. A treatment whose
real benefit is *knowing what to look at* is invisible here by construction.
([ADR-0002](adr/0002-imposed-panel-as-unit-of-analysis.md))

**Whether better forecasts produce better portfolios.** Panel answers never
move the portfolio. The two arms of the study — forecast quality and equity
path — are measured on the same runs but are not linked by any test.

**Leverage, margin, interest, FX conversion.** None modelled. Uninvested cash
earns nothing, which understates every arm's return equally.

**The model's reasoning.** Whether a model is sandbagging, recognising the
study, or reward-hacking the panel is beyond anything this platform can detect.

## 3. What the causal claim actually is

**The treatment is endogenous to each arm's own trajectory**, and this is not
fixable by any design that studies persistent memory.

Arm B at cycle 30 is shown what B decided on cycles 22–29 — decisions that were
themselves shaped by the memory B held then. Condition D shows it most sharply:
D receives strategy rules distilled from D's own record, and that record was
shaped by reflection at every earlier interval. **The treatment intensity is a
feedback loop whose gain nobody has measured.**

What contains it: the world is shared and exogenous (no market impact, so arms
cannot diverge *through* the world), the panel questions are exogenous, and the
model sees no portfolio state. The one channel left is a line in the rendered
memory reading *"portfolio value at the time: …"*.

**So B − A estimates the effect of running under a memory regime for the whole
study, averaged over the trajectory that regime produced.** It does not
estimate the effect of memory on a decision at a fixed state, and nothing here
can. A write-up must say which one it is claiming.
([ADR-0016](adr/0016-treatment-is-endogenous-to-trajectory.md))

Two consequences that follow:

- **The effect may vary with session index and the analysis averages over it.**
  Effect-against-date is inspectable and should be reported as a description,
  not as a test — a test chosen after seeing the shape is not a test.
- **Arms are exchangeable at cycle 1 and never again.** Randomisation happens
  once, at the design level.

## 4. What a null result would and would not mean

**A null does not distinguish "memory does not help" from "this model ignores
its context".** No statistical care changes that; only a pilot showing that
granted material measurably changes decisions makes a null interpretable at
all. This is stated in [PRE_REGISTRATION.md](PRE_REGISTRATION.md) §10 before
any data exist.

**A null does not distinguish "reflection does not help" from "*this*
reflection is too weak to help".** Reflection here is closed-form rules about
the condition's own record — a thin distillate compared to what a model would
write. **This is the single most important limitation on how far a null result
generalises**, and it belongs in a write-up's first paragraph, not its
appendix. ([ADR-0015](adr/0015-closed-form-reflection-without-outcomes.md))

**A null does not distinguish "memory does not help" from "memory without
outcomes does not help".** Episodes record what a condition decided, not
whether it was right. That is a coherent treatment — "does having your own
history help?" — but the literature's intuitions mostly concern the stronger
version, "does having your own *track record* help?".

**A B-versus-A difference that does not survive B-versus-B′ is a finding about
prose, not about memory**, and will be reported that way.

## 5. What the measurement cannot resolve

**Brier @ 1 session is not viable at all.** Its true effect lies *inside* a
±0.005 ROPE and power never exceeds 0.07 out to 180 sessions. That is the ROPE
correctly saying the effect is too small to matter; running longer does not
help. ([POWER.md](POWER.md))

**Decision stability is implemented but unpowered.**
`RepetitionStatistic.DISPERSION` runs through the same pipeline, needs ≥2
repetitions to be computable at all, and its effect size depends on model-level
reporting noise that no pilot has measured.

**Evidential fidelity is not simulatable.** Citation validity is a property of
model behaviour, not of forecast skill. Powering it would mean inventing a
model of how often an LLM miscites — precisely the quantity the study exists to
observe.

**Overconfidence is punished less than a log score would punish it.** Brier
costs a wrong 0.99 about 0.98; a log score at a 0.01 clip costs ~4.6. The study
is therefore less sensitive to exactly the pathology LLM agents are most
suspected of. The log score is refused because its clip value silently sets how
much a confident error is worth, and that is a pre-registration decision the
library will not make. ([ADR-0007](adr/0007-brier-only-no-log-score.md))

**Aggregating to dates discards within-date structure.** A real effect present
on one instrument and absent on three is diluted rather than detected.

**The bootstrap's coverage at this sample size is asymptotic and unverified.**
The empirical false-positive rate across the power grid (0.00–0.01) is the only
evidence offered that the procedure is not anticonservative.

**A placebo matches shape, not information content.** It is matched line for
line and to within 2% on length, and carries no instrument, probability or
failure kind from the genuine record. It is **not** matched on how plausible it
reads. A model that can tell them apart for stylistic reasons defeats the
control, and nothing here can detect that.

## 6. What the numbers are, and are not

**Every cost figure is `ASSUMED`, not measured.** `TokenUsage` is recorded on
every decision and panel bundle, and `measure_profile` refuses to build a
profile from a run that reported nothing — which is exactly what a run against
the deterministic fake reports. Until a pilot runs against a real provider, the
money in [POWER.md](POWER.md) is arithmetic on a written-down guess.

**The power simulation assumes a world in which skill is expressible.** It
models arms recovering fractions of a real edge. **If a real model's forecasts
cluster within a couple of points of 0.5 whatever it is granted, every effect
size is optimistic and every duration is an underestimate.** The pilot must
report the observed spread of forecast probabilities.

**Recall depth and reflection cadence set the effect size the study is powered
to detect**, and the simulation takes that effect size as a parameter rather
than deriving it. Eight episodes and every five cycles are round numbers. They
are not implementation details.

**The block length is a rule of thumb, not an estimate.** Data-driven selectors
depend on an autocorrelation nobody has measured. Its value is carried on every
result.

**Fee magnitudes are placeholders.** In the spirit of retail brokerage, not a
calibrated cost model. Pre-registered in the sense that every arm pays the
same, which is what the comparison needs; not claimed to be realistic.

## 7. What the record cannot prove

**The hash chain proves tampering, not authorship.** Nothing is signed. An
author with the database file could rewrite history and recompute every hash,
and it would verify. Publishing the daily root hash somewhere outside the
authors' control would close this, and it is **not done**.

**Analyst masking does not exist.** The mitigation is pre-registration and a
pipeline with no discretion in it — and that mitigation's credibility rests
entirely on the pre-registration being tagged before the first confirmatory
run. If that ordering is not verifiable by a stranger, the claim is worth
nothing. ([ADR-0003](adr/0003-masking-is-partial.md))

**A provider may change under a stable identifier.** The configuration
fingerprint would be unchanged and the study would not be. No code here can
detect it.

**Trading calendars are outside the fingerprint.** A change to calendar code
changes the study without changing its fingerprint.

**The replay verifies everything downstream of the model, and only that.** A
real provider is not a pure function, so the claim is precisely: *given what
the model said, everything else is reproducible.*
([ADR-0012](adr/0012-replay-recomputes-downstream-of-the-model.md))

## 8. What the world is

The synthetic universe is a fabricated script: closed-form prices, scripted
news, a news-free session (5), a cash dividend (ex 10, pay 12), a 2-for-1 split
(18), and a deliberate prompt injection (22). It is designed to exercise the
platform's edge cases, **not** to resemble a market.

Nothing about a result obtained against it generalises to real data. It exists
so the pipeline can be built, tested and replayed without a provider — and so
that a corporate action, a missing bar or a hostile headline is a reproducible
fixture rather than something to wait for.
