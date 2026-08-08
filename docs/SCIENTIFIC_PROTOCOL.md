# Scientific protocol

What the study measures, how, and — at every point where it matters — what it
cannot measure.

This is the *methodological* document. The binding parameter values live in
[PRE_REGISTRATION.md](PRE_REGISTRATION.md), which is deliberately shorter and
deliberately incomplete until its open decisions are made. Where the two
disagree, the pre-registration wins: it is the one that binds a run.

## 1. The question, and what would answer it

> Do persistent memory and periodic strategic reflection improve the
> probabilistic quality, coherence and stability of an LLM agent's decisions in
> a virtual multi-asset market?

The answerable form of that is narrower, and stating the narrowing is the first
honest thing this document does:

**Does an agent granted its own past decisions, or a distillate of them, give
better-calibrated probabilities on a fixed panel of direction questions than an
otherwise identical agent granted nothing — and does any such difference
survive comparison against an agent granted matched but contentless prose?**

Financial performance is **secondary and exploratory**. A positive return is
never treated as evidence of competence: with fixed sizing
([ADR-0014](adr/0014-identical-fixed-fraction-sizing.md)) and a small number of
sessions, an equity curve is mostly a draw from the market.

## 2. Design

A crossed 2×2 of two channels, plus matched placebos. Six conditions.

| Arm | memory | reflection | placebo of | isolates |
|---|---|---|---|---|
| A | — | — | — | control |
| B | genuine | — | — | memory main effect |
| C | genuine | genuine | — | both channels |
| D | — | genuine | — | reflection main effect |
| B′ | placebo | — | B | content of memory vs. presence of prose |
| C′ | placebo | placebo | C | content of both vs. volume of text |

**Why crossed rather than three arms.** Without D, a C-versus-A difference
cannot be attributed to memory or to reflection; it would be attributed to
whichever the write-up happened to name.

**Why D is coherent.** The two channels are defined as separable: memory is
*raw episodic recall*, reflection is *distilled strategy* produced by a process
that reads the run's record. Under D the reflection process reads the history;
the agent does not. D separates "having been told what works" from "being able
to look up what happened".

**Why placebos.** A model handed several hundred extra words of plausible prose
may behave differently for reasons unrelated to what the prose says. B′ and C′
receive text matched line for line and to within 2% on length — by
construction, since the placebo runs through the same renderer over fabricated
episodes shaped from the arm's own record. A B-versus-A difference that
survives B-versus-B′ is about the *content* of memory. One that does not is
about having been handed some writing, which is itself a result worth
reporting.

Full reasoning: [ADR-0001](adr/0001-crossed-design-with-matched-placebos.md).

### Confirmatory contrasts

**B vs A**, **D vs A**, **C vs A**, **B vs B′**, **C vs C′** — five, crossed
with the pre-registered horizons, corrected as one family by Holm.

## 3. What is held identical, and how

| Held identical | Mechanism | Strength |
|---|---|---|
| The market | one frozen snapshot object per cycle, shared **by identity** across all six arms | structural |
| The questions | the imposed panel is derived from the snapshot, never from anything an arm said | structural |
| The budgets | one tool budget, one evidence cap, one turn allowance, imposed by the runner; a materials provider returns text, never a context object, so it *cannot* vary them | structural |
| Model state | a fresh model instance per arm per elicitation | structural |
| Position in the cycle | Latin-square execution order, so provider drift within a cycle is not confounded with the condition | by construction |
| Sizing and costs | one target weight, one fee schedule, one participation cap, for every arm | by configuration |

The Latin square balances **position, not carryover**. Cyclic rotation gives
every arm every position exactly once per rotation but does not balance which
arm ran immediately before which. A Williams design would. It matters only if
an arm's execution measurably affects the next one's, and under the current
isolation — fresh model, fresh budget, no shared state — there is no mechanism
for that.

## 4. Masking is partial, and here is where it stops

This is §5.4's requirement, and the word *partial* is the important one.

### Model side — structural, and verified on content

`ModelRequest` has exactly one field through which a condition may differ:
`injected_context`, the granted text itself, or `None`. There is no field for
the arm, the repetition, or the run.

Three independent guards:

1. **Field-shape scan** over every type in `models/` and the signature of
   `LanguageModel.generate`.
2. **Content scan** over every `ModelRequest` produced by a real six-arm cycle
   — this is what catches a leak through a field with an innocent name.
3. **Import scan** forbidding the decision path from holding a session at all.

A model cannot condition on its arm without a change that three tests catch.

### Analysis side — absent

The database stores arm identifiers in the clear. `marketlab analyse` prints
them. Whoever runs it knows exactly which arm is which.

Label-stripping was considered and rejected as *worse than nothing*: with six
arms in a 2×2 with placebos, the mapping is usually recoverable from the
results themselves, so it would produce the appearance of blinding without the
substance.

**The mitigation is not blinding. It is removing the analyst's discretion
before the data exist:** a ROPE with no default, a fixed pipeline rather than a
menu, a declared contrast family, and a run configuration that cannot be
changed without changing its identifier.

That mitigation's credibility rests **entirely** on the pre-registration being
tagged in a signed commit before the first confirmatory run. If that ordering
is not verifiable by a stranger, this section's claim is worth nothing.

Full reasoning: [ADR-0003](adr/0003-masking-is-partial.md).

### What "condition-blind" does not mean

It does not mean the arms see identical text — they are *supposed* to differ;
that is the treatment. What is masked is the **label**, not the material. A
model that infers *"this reads like fabricated filler, so I am a control"*
defeats the placebo entirely, and no structural guard can prevent it.

## 5. The trajectory confound, stated rather than buried

In a clean experiment the treatment is assigned from outside. Here it cannot
be, and this is the design's most important structural limitation.

**Memory is the arm's own history.** Arm B at cycle 30 is shown what B decided
on cycles 22–29 — and what B decided on cycle 25 was itself shaped by the
memory B held then. The treatment at any point is a function of its own past
effects.

**Condition D shows it at its purest.** D receives strategy rules distilled
from D's own episodes. D has no memory, so it never reads that record — yet the
rules it is handed at cycle 30 come from a trajectory that reflection at cycles
5, 10, 15, 20 and 25 already shaped. A rule of the form *"you have consistently
held Alpha"* makes holding Alpha more likely, which strengthens the rule at the
next reflection. **The treatment intensity is not constant. It is a feedback
loop whose gain nobody has measured.**

### What contains it

- **The world is exogenous and shared.** No arm's actions affect prices,
  volumes, news or corporate events; there is no market-impact model. Arms
  cannot diverge *through the world*.
- **The panel questions are exogenous.** They come from the snapshot, not from
  any arm. The question is fixed even though the treatment is not, which is
  what keeps the primary metric comparable.
- **The model sees no portfolio state.** `ConditionContext` carries the
  injected text and a turn budget. There is no field for cash, positions or
  equity.

### The one channel that is left

`render_memory` writes a line reading *"portfolio value at the time: …"*. That
number is the arm's own trajectory, and it is the sole path by which divergent
books reach a decision. (The placebo fabricates a value in the same position,
so B′ is not distinguishable from B by its absence.)

### What the estimand therefore is

**B − A estimates the effect of running under a memory regime for the whole
study, averaged over the trajectory that regime produced.** It does not
estimate the effect of memory on a decision at a fixed state, and nothing in
this design can.

That is a legitimate causal quantity — it is what a deployment would experience
— but it is not what a per-cycle reading of the result would suggest. **Any
write-up must say which one it is claiming.**

Full reasoning: [ADR-0016](adr/0016-treatment-is-endogenous-to-trajectory.md).

## 6. Measurement

### The unit of analysis is the imposed panel

Not the free decision. If arm A forecasts Alpha and Delta while arm B forecasts
Beta and Gamma, their scores are not comparable — and *which* instruments an
arm chooses is itself an outcome of the treatment, so restricting to the
overlap concentrates the selection rather than removing it.

Each cycle poses a fixed list of `(instrument, horizon)` questions, identical
for every arm, in a **separate elicitation** with its own model instance, tool
budget and turn count. Trade intents arriving in a panel response are
discarded: an assessment must not move the portfolio.

Every item is answered or the omission is recorded as a `MISSING_PANEL_ITEM`
failure. Silence is never read as a shorter answer set.

[ADR-0002](adr/0002-imposed-panel-as-unit-of-analysis.md).

### Resolution is in total return

```
total_return = (split_factor × target_close + dividends) / anchor_close − 1
outcome_up   = total_return > 0        # a flat close is "not up"
```

Corporate events over the **half-open interval `(anchor, target]`**. The
synthetic world genuinely halves an instrument's quote on its split session, so
raw close-to-close would score a 2-for-1 as a 50% loss for every arm that
forecast it.

"In N sessions" means N points on the **run's own decision grid**, read back
from its snapshots — identical for every arm, deterministic, and immune to a
missing bar silently turning a 5-session horizon into a 6-session one. Stated
as an interpretation of §20, not a quotation.

[ADR-0006](adr/0006-total-return-resolution-on-the-run-grid.md).

### The scoring rule is Brier, and the log score is refused

Brier is proper, bounded, and finite at `p ∈ {0, 1}` — which real models emit.
The log score is infinite there, so every implementation clips, and **the clip
value silently sets how much a single confident error is worth**. That is a
pre-registration decision with real consequences, and the library will not
invent one.

`ABSOLUTE_ERROR` is offered as a robustness check and documented as
**improper**.

[ADR-0007](adr/0007-brier-only-no-log-score.md).

## 7. Analysis plan

Fixed, implemented, tested, and not a menu. In order:

1. **Pair** on `(date, instrument, horizon)` within the panel. **Complete cases
   only** — a cell enters only if every compared arm resolved it. There is no
   imputation option and there will not be one. Drops are counted and reported
   by reason.
2. **Aggregate** to one paired difference per date. Panel items within a
   session are strongly cross-sectionally correlated; treating them as
   independent would count one favourable day many times over.
3. **Moving block bootstrap** over the date series, deterministic from a seed,
   block length `round(n^(1/3))` unless overridden. Blocks, because
   consecutive forecast windows overlap: at horizon 20 with 60 anchors,
   neighbouring windows share 19 of 20 sessions.
4. **TOST against the ROPE**, read off the bootstrap rather than a
   t-distribution, at a `1 − 2α` interval. Three verdicts:

   | verdict | when | meaning |
   |---|---|---|
   | `EQUIVALENT` | interval inside the ROPE | no practically useful effect — a **finding** |
   | `DIFFERENT` | interval disjoint from the ROPE | an effect large enough to matter |
   | `INCONCLUSIVE` | interval straddles a bound | not enough data to say |

   `INCONCLUSIVE` is never collapsed into "no difference". Collapsing them is
   how underpowered studies come to claim null results.
5. **Multiplicity correction** across the whole declared family, Holm by
   default. Comparisons with no data are **skipped and excluded**, never given
   a fabricated p-value — which would inflate the correction and make the
   survivors look stronger.

The plan **cannot be constructed without a ROPE**. There is no default, here or
in the library, so no analysis can run until the study owner fixes the band.

[ADR-0008](adr/0008-date-aggregation-block-bootstrap-tost.md),
[ADR-0009](adr/0009-no-default-rope.md),
[ADR-0010](adr/0010-complete-cases-only.md).

## 8. What the numbers say about how long to run

From [POWER.md](POWER.md), produced by running the **real** analysis pipeline
over simulated worlds:

- **Horizon matters far more than duration.** The same skill gap gives −0.004
  Brier at 1 session, −0.015 at 5, −0.030 at 20.
- **Brier @ 1 session is not viable as a primary metric.** Its effect falls
  *inside* a ±0.005 ROPE, and power never exceeds 0.07 out to 180 sessions.
  That is the ROPE correctly saying the effect is too small to matter, not a
  power failure. Running longer does not help.
- **Brier @ 5 reaches 0.81 power at 60 sessions and 0.94 at 120.**
- **Concluding the null costs more than detecting an effect.** `EQUIVALENT`
  under the null: 23% at 60 sessions, 83% at 180. If a credible negative result
  is a goal — and it should be — the binding constraint is **120–180
  sessions**, not the 60 that suffices for power.
- **Cost is not a constraint.** $12 / $78 / $390 for 120 sessions × 6 arms at
  small / mid / frontier price tiers.

## 9. What counts as a negative result

An `EQUIVALENT` verdict on B vs A at the pre-registered primary metric, with
the interval inside the ROPE, is a **finding** and will be published as one.

So is `INCONCLUSIVE`, reported as inconclusive rather than as "no effect".

A B-versus-A difference that does **not** survive B-versus-B′ is evidence that
the effect is prose, not memory, and will be reported that way.

## 10. Deliberately not in the design

Stated so an absence is not mistaken for an oversight.

- **No outcome feedback in memory.** An episode records what a condition
  *decided*, not what happened to it. Resolution exists and could supply it,
  but an arm shown its own hit rate is a **different arm** — a change to the
  treatment requiring its own piloting.
  [ADR-0015](adr/0015-closed-form-reflection-without-outcomes.md).
- **Reflection is closed-form, not model-authored.** A model-authored
  reflection could not be replayed, and is a different treatment.
- **No short selling, no leverage, no margin, no interest, no FX conversion.**
  A bearish arm can only express itself by not buying.
- **Sizing is a fixed fraction, identical for every arm.** The study measures
  direction and timing, **not portfolio construction**. An arm cannot win by
  sizing and cannot demonstrate skill at it.
- **No interim analysis and no stopping rule.** There is no alpha-spending
  plan, so there is no supported way to peek.

## 11. The threat to validity that comes before all the others

**The model may simply not attend to the injected material.**

If so, every arm decides alike and the study measures "this model ignores its
context" rather than "memory does not help". A null result cannot distinguish
these two, and no amount of statistical care changes that.

The repository ships a test double that *does* read its context and produces
different decisions per arm through the identical pipeline, so the **channel**
is known to be live. Whether a given real model uses it is an empirical
question only a pilot can answer.

The power simulation sharpens what that pilot must report. It assumes arms
recover fractions of a genuine edge. **If a real model's probabilities cluster
within a couple of points of 0.5 whatever it is granted, there is no skill to
differentiate and every duration in [POWER.md](POWER.md) is an
underestimate.** The pilot must therefore report the **observed spread of
forecast probabilities**, not only whether the decisions differ.

## 12. Everything else this study cannot conclude

See [LIMITATIONS.md](LIMITATIONS.md), which exists so that the list is in one
place rather than distributed across the sections that happen to mention them.
