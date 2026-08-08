# ADR-0016 — The treatment is endogenous to each arm's own trajectory, and this is not fixable

- **Status:** accepted
- **Implemented by:** `marketlab.experiments.materials`, `marketlab.memory.store`, `marketlab.reflection.engine`
- **Checked by:** `tests/integration/test_materials_wiring.py`, `tests/unit/test_memory_materials.py`

## Context

In a clean experiment the treatment is assigned from outside and does not
depend on what the subject did. Here it cannot be.

**Memory is the arm's own history.** Arm B at cycle 30 is shown what B decided
on cycles 22 through 29 — and what B decided on cycle 25 was itself influenced
by the memory B held then. The treatment at any point is a function of the
treatment's own past effects.

**Reflection is worse, and condition D shows why.** D receives distilled
strategy rules derived from D's own episodes, produced by a process that reads
D's record. D itself has no memory, so it never sees that record directly — but
the rules it is handed at cycle 30 were distilled from a trajectory that
reflection at cycles 5, 10, 15, 20 and 25 already shaped. A rule of the form
*"you have consistently held Alpha"* makes holding Alpha more likely, which
strengthens the rule at the next reflection. **The treatment intensity is not
constant; it is a feedback loop whose gain is unmeasured.**

Under D this is at its purest. B and C at least have direct access to the
material their treatment was distilled from, so a divergence is in principle
inspectable in the episodes. D is handed a conclusion whose provenance it
cannot see, drawn from a history it cannot read, that its own past receipt of
conclusions produced.

## Options considered

**Give every arm the same memory** — for instance, arm A's history, or a
history generated once and reused. Rejected: it is not memory. "Being shown
somebody else's past decisions" is a different treatment, and probably a much
weaker one, since the value of episodic recall is presumably that it is *yours*.

**Force the arms' portfolios to be identical** by having every arm's decision
executed in a shared book. Rejected: it destroys the free-decision arm of the
study entirely, and it would make each arm's memory a record of a portfolio it
did not cause.

**Re-randomise the arm assignment each cycle**, so no agent accumulates a
trajectory. Rejected: it makes persistent memory unmeasurable, which is the
question.

**Accept the endogeneity, contain what can be contained, and state precisely
what the estimand becomes.** Chosen.

## Decision

### What is held exogenous, structurally

- **The market is shared.** Every arm of a cycle reads *the same frozen
  snapshot object* — not one rebuilt identically, the same object. No arm's
  actions affect prices, volumes, news or corporate events. There is no market
  impact model, so trajectories cannot diverge through the world.
- **The panel questions are shared.** They are derived from the snapshot, never
  from anything an arm said. The *question* is exogenous even though the
  *treatment* is not — which is what keeps the primary metric comparable at all
  ([ADR-0002](0002-imposed-panel-as-unit-of-analysis.md)).
- **The model sees no portfolio state.** `ConditionContext` carries the
  injected text and a turn budget, and nothing else. There is no field for
  cash, positions or equity. A divergence in the books cannot reach a decision
  except through the memory text.

### What is endogenous, and admitted

- **Memory content.** Each arm's episodes are its own, in its own scope.
- **The equity line inside a memory episode.** `render_memory` writes
  *"portfolio value at the time: …"*. That number is the arm's own trajectory,
  and it is the one channel by which the divergent books reach a decision. The
  placebo fabricates a value in the same position so B′ is not distinguishable
  by its absence.
- **Reflection rules**, derived from the arm's own record, with the feedback
  loop described above.

### What the estimand therefore is

**B − A estimates the effect of *running under a memory regime for the whole
study*, averaged over the trajectory that regime produced.** It does not
estimate the effect of memory on a decision at a fixed state, and nothing in
this design can.

That is a legitimate causal quantity — it is what a deployment would actually
experience — but it is not the quantity a per-cycle reading of the result would
suggest. Any write-up must say which one it is claiming.

## Consequences

**Arms are exchangeable at cycle 1 and never again.** Randomisation happens
once, at the design level; there is no per-cycle re-randomisation to restore
balance.

**The effect may vary with session index, and the analysis averages over it.**
If memory helps only once eight episodes have accumulated, the early sessions
dilute the estimate. If reflection's feedback loop amplifies, late sessions
dominate. **The paired difference by date is recorded, so effect-against-time
is inspectable — and it should be inspected and reported, as a secondary
description rather than as a test**, since a test chosen after seeing the shape
is not a test.

**A degenerate feedback loop is possible and would not be flagged.** A
reflection that reinforces itself into a fixed strategy would show up as low
decision variance late in the run. Nothing detects that automatically. The
dispersion statistic (`RepetitionStatistic.DISPERSION`) is the natural
instrument and needs ≥2 repetitions to be computable at all.

**Repetitions are the only within-condition variance available**, and one is
the current default. With one repetition per arm, a trajectory that went badly
by chance is indistinguishable from a treatment that is worse.

**The confound is structural, not a defect.** Every study of persistent memory
in an agent has it. What this record adds is that it is named, its channel is
identified down to one line of rendered text, and the estimand is stated rather
than left for a reader to assume.

## What would make us revisit this

- **A market impact model.** It would open a second channel — an arm's trades
  moving prices the other arms then see — and would break the "shared exogenous
  world" guarantee that currently does most of the containment. It should not
  be added without re-reading this record.
- **Repetitions ≥ 2 becoming standard.** That would make trajectory variance
  estimable rather than merely acknowledged, and would let a degenerate
  feedback loop be detected instead of assumed absent.
- **Outcome feedback in memory**
  ([ADR-0015](0015-closed-form-reflection-without-outcomes.md)). It tightens the
  loop considerably: the treatment would then depend on the arm's own realised
  accuracy, making the feedback gain both larger and more directly tied to the
  outcome being measured.
