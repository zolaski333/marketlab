# Pre-registration

> **Status: not yet binding.** This document is complete in structure and
> incomplete in content. The decisions marked **OPEN** below must be made
> before any run counts as confirmatory.
>
> The power simulation that several of them waited on has now run — see
> [POWER.md](POWER.md) — so what remains is judgement, not missing evidence.
> The two that still have no evidence behind them and cannot get any from a
> simulation are the **model** and the **recall depth**: both need a pilot.
>
> Publishing it in this state is the point. A pre-registration written after
> the data exist is not one, so the honest sequence is to publish the frame
> first, fill it in a signed commit, and only then run.

## 1. Question

Do persistent memory and periodic strategic reflection improve the
**probabilistic quality, coherence and stability** of an LLM agent's decisions
in a virtual multi-asset market?

Financial performance is a **secondary, exploratory** measure. A positive
return is not treated as evidence of competence, and no result here is
investment advice.

## 2. Design

A crossed 2×2 of two channels, plus matched placebos.

| Arm | Memory | Reflection | Role |
|---|---|---|---|
| A | — | — | Control |
| B | genuine | — | Raw episodic recall only |
| C | genuine | genuine | Both channels |
| D | — | genuine | Distilled strategy only |
| B′ | placebo | — | Matched control for B |
| C′ | placebo | placebo | Matched control for C |

**Why crossed rather than three arms.** Without D, a C-versus-A difference
cannot be attributed to memory or to reflection; it would be attributed to
whichever the write-up happened to name.

**Why placebos.** B′ and C′ receive text of the same shape and length as B and
C — matched line for line and to within 2% on length, by construction rather
than by estimate, because the placebo runs through the same renderer. A
B-versus-A difference that survives B-versus-B′ is about the *content* of
memory. One that does not is about having been handed some prose.

**What is held identical across arms.** One frozen snapshot per cycle, shared
by object identity, not rebuilt per arm. One tool budget, one turn allowance,
one fresh model instance each. Execution order is balanced by Latin square so
that provider drift within a cycle cannot be confounded with the condition.

## 3. Primary outcome

**OPEN — the evidence is in; the choice is not made.**

The power simulation has now run — see [POWER.md](POWER.md) — so this is a
choice to be *made* rather than deferred. What it found:

| candidate | power at 120 sessions | verdict |
|---|---:|---|
| Brier @ 5 sessions | 0.94 | viable; recommended |
| Brier @ 20 sessions | 1.00 | viable; slow to resolve |
| Brier @ 1 session | 0.05 | **not viable** — effect falls inside the ROPE |
| Decision stability | — | implemented, needs a pilot to power |
| Evidential fidelity | — | not simulatable from a forecast-skill model |

Brier is strictly proper, which is why it is the implemented default: a
forecaster minimises its expected score only by reporting its true belief.

**Still OPEN**: which of these is written here as *the* primary. The others
become secondary and are reported without confirmatory weight.

Secondary outcomes, reported but not confirmatory: calibration tables per arm,
observed agent-failure rates by kind, portfolio equity paths.

## 4. Region of practical equivalence

**OPEN — but now informed.**

§21.7 requires "no practically useful effect" to be a reachable conclusion,
which requires a band around zero inside which a real difference is too small
to matter. On the Brier scale this is not intuitive, which is what the
simulation was for: at horizon 5 the effect scales at roughly **0.07 Brier
points per unit of recovered signal**, so a ±0.005 band treats as negligible
any arm that recovers less than about 7 points more of the available edge.

Reaching an `EQUIVALENT` verdict also costs duration: under the null it is
returned 23% of the time at 60 sessions and 83% at 180. If a credible null
result is a goal — and it should be — the study is longer than power against
an effect alone would require.

This is **enforced, not merely intended**: `marketlab.analysis.plan.AnalysisPlan`
has no default ROPE and cannot be constructed without one. No analysis can be
run in this repository until the band is chosen.

## 5. Analysis plan

Fixed, implemented and tested. In order:

1. **Pairing** on `(date, instrument, horizon)`, restricted to the imposed
   panel. Complete case only: a cell enters the analysis only if every compared
   arm resolved it. **There is no imputation option and there will not be one.**
   Dropped cells are counted and reported by reason.
2. **Aggregation** to one observation per date. Panel items within a session are
   strongly cross-sectionally correlated; treating them as independent would
   count one favourable day many times over.
3. **Moving block bootstrap** over the date series, deterministic from a seed,
   block length `round(n^(1/3))` unless overridden.
4. **TOST against the ROPE**, read off the bootstrap rather than a
   t-distribution, at a `1 − 2α` interval. Three verdicts: `EQUIVALENT`,
   `DIFFERENT`, `INCONCLUSIVE`. Inconclusive is a real answer and will be
   reported as one.
5. **Multiplicity correction** across the whole planned family (five contrasts
   × the pre-registered horizons), Holm by default. Comparisons with no data
   are *skipped* and excluded from the family rather than given a fabricated
   p-value.

Confirmatory contrasts: B vs A, D vs A, C vs A, B vs B′, C vs C′.

## 6. Model, cadence and budgets

**OPEN.** Each of the following must be fixed in the study configuration before
the run, and the configuration is then immutable (see §8):

| Parameter | Status |
|---|---|
| Model and version | **OPEN** — must be recorded exactly, including the sampling temperature |
| Decision cadence | **OPEN** — working assumption: decide at close of session *t*, execute at the next eligible window |
| Number of sessions | **OPEN** — 60 for 80% power at horizon 5; 120-180 to be able to conclude equivalence ([POWER.md](POWER.md)) |
| Repetitions per arm | **OPEN** — 1 suffices for accuracy; 2+ only if stability is made primary |
| Tool budget, evidence cap, turn cap | placeholders (20 calls / 20 000 chars / 6 turns) |
| Recall depth, reflection cadence | placeholders (8 episodes, every 5 cycles) |
| Starting capital, target weight | placeholders (1 000 000 USD + 500 000 EUR, 5%) |

Recall depth and reflection cadence deserve emphasis: a deeper recall is a
**stronger treatment**, so these choices set the effect size the study is
powered to detect. They are not implementation details.

## 7. What is deliberately not in the design

Stated so that a reader does not mistake an absence for an oversight.

- **No outcome feedback in memory.** An episode records what a condition
  *decided*, not what happened to it. Resolution exists and could supply it,
  but an arm shown its own hit rate is a different arm from one shown its own
  past decisions — a change to the treatment, requiring its own piloting. Open
  question 9 in the roadmap.
- **No short selling, no leverage, no margin, no interest, no FX conversion.**
  A bearish arm can only express itself by not buying. This bounds what the
  study can observe.
- **Sizing is a fixed fraction of equity, identical for every arm.** The study
  measures direction and timing quality, **not portfolio construction**. An arm
  cannot win by sizing and cannot demonstrate skill at it.
- **Reflection is closed-form, not model-authored.** A model-authored
  reflection is a different treatment and would need re-piloting.

## 8. How this document is bound to a run

1. Every parameter above lives in a configuration file under `configs/`.
2. `marketlab run` **declares** that configuration under its `run_id` and
   records its SHA-256 fingerprint in an append-only table.
3. Re-running the same `run_id` with any parameter changed is **refused**, exit
   code 4. Changing the design mid-study requires a new `run_id`, which is
   visible.
4. The fingerprint is reported by `marketlab status` and should be quoted in
   any write-up.
5. This document should be finalised in its own commit, tagged, **before** the
   first confirmatory run — so the ordering is verifiable by anyone, not
   asserted by the authors.

## 9. What would count as a negative result

An `EQUIVALENT` verdict on B vs A at the pre-registered primary metric, with
the interval inside the ROPE, is a **finding** and will be published as one.

So is `INCONCLUSIVE`, reported as inconclusive rather than as "no effect".

A B-versus-A difference that does **not** survive B-versus-B′ is evidence that
the effect is prose, not memory, and will be reported that way.

## 10. Known threat to validity, stated before any data exist

The model may simply not attend to the injected material. If so, every arm
decides alike and the study measures "this model ignores its context" rather
than "memory does not help". The platform cannot distinguish these two from a
null result alone.

Mitigation: a pilot must show that granted material measurably changes
decisions before the confirmatory run is worth paying for. The repository ships
a test double that reads its context and produces different decisions per arm
through the identical pipeline, so the *channel* is known to be live; whether a
given real model uses it is an empirical question the pilot has to answer.

The power simulation sharpens what the pilot must report. It assumes arms
recover fractions of a genuine edge; if a real model's probabilities cluster
within a couple of points of 0.5 whatever it is granted, there is no skill to
differentiate and every duration in [POWER.md](POWER.md) is an underestimate.
**The pilot must therefore report the observed spread of forecast
probabilities**, not only whether the decisions differ.

Cost is not among the risks. A full 120-session, six-arm study projects at $12
to $390 depending on price tier, so the design should be chosen on power and
validity and not on price.
