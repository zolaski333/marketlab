# Power and cost: the numbers behind the design decisions

> **What this is.** Task #4's deliverable: the evidence needed to close open
> questions 1 (primary metric), 3 (ROPE), 4 (budgets), 6 (repetitions) and 7
> (recall depth) in [ROADMAP.md](ROADMAP.md).
>
> **What it is not.** A decision. Every number below is an input to a choice
> the study owner makes and records in [PRE_REGISTRATION.md](PRE_REGISTRATION.md).

## How these numbers were produced

Every replication is analysed by the **real** `AnalysisPlan` — the same object
that will produce the published result, with the same complete-case pairing,
the same date-level aggregation, the same moving block bootstrap and the same
TOST against a ROPE. Nothing here re-derives a standard error.

That is the one design decision that makes a power simulation worth doing. The
usual way such a calculation is wrong is that it prices a *different* analysis
than the one that gets run — a closed-form t-test's power, quoted for a study
that will actually bootstrap over aggregated dates. Whatever this platform's
analysis loses to its own conservatism is already inside these figures.

Reproduce any row:

```bash
uv run marketlab power --dates 60 --horizons 5 --skill-gap 0.20 --replications 200
```

**Effect size is parameterised in recovered signal, not in Brier points.** An
arm with `skill = s` reports `0.5 + s × (oracle − 0.5)`: it recovers a fraction
of the edge that exists. This matters — parameterising the effect in Brier
units would have required assuming the answer, since the whole reason the ROPE
is undecided is that nobody knows what a Brier difference of 0.005 means. Here
the Brier difference is an **output**.

## 1. Power against duration, by horizon

Baseline skill 0.30, treatment 0.50 (the treated arm recovers 20 points more of
the available signal), 4 instruments, ROPE ±0.005, 150 replications.
`fp` is the false-positive rate: the same analysis run on a world where the
arms are identical.

| horizon | sessions | power | inconclusive | fp | equiv. under null | mean effect (Brier) | design effect | effective *n* (of items) |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 20 | 0.07 | 0.92 | 0.00 | 0.00 | −0.0053 | 1.31 | 61 of 80 |
| 1 | 60 | 0.01 | 0.97 | 0.00 | 0.07 | −0.0041 | 1.42 | 169 of 240 |
| 1 | 120 | 0.05 | 0.83 | 0.00 | 0.44 | −0.0041 | 1.41 | 342 of 480 |
| 1 | 180 | 0.03 | 0.87 | 0.00 | 0.69 | −0.0045 | 1.41 | 511 of 720 |
| **5** | 20 | 0.49 | 0.51 | 0.01 | 0.01 | −0.0147 | 1.18 | 68 of 80 |
| **5** | 40 | 0.61 | 0.39 | 0.01 | 0.04 | −0.0148 | 1.24 | 129 of 160 |
| **5** | **60** | **0.81** | 0.19 | 0.01 | 0.23 | −0.0157 | 1.23 | 195 of 240 |
| **5** | 90 | 0.91 | 0.09 | 0.00 | 0.42 | −0.0153 | 1.24 | 290 of 360 |
| **5** | 120 | 0.94 | 0.06 | 0.00 | 0.61 | −0.0151 | 1.25 | 385 of 480 |
| **5** | 180 | 0.99 | 0.01 | 0.00 | 0.83 | −0.0151 | 1.26 | 572 of 720 |
| 20 | 20 | 0.90 | 0.10 | 0.01 | 0.07 | −0.0302 | 1.08 | 74 of 80 |
| 20 | 60 | 1.00 | 0.00 | 0.00 | 0.39 | −0.0305 | 1.13 | 213 of 240 |
| 20 | 120 | 1.00 | 0.00 | 0.00 | 0.79 | −0.0301 | 1.14 | 422 of 480 |

### The finding that decides the primary metric

**Horizon is worth far more than duration.** The same true skill gap produces a
Brier difference of −0.004 at 1 session, −0.015 at 5, and −0.030 at 20. Adding
sessions moves power slowly; changing horizon moves it by an order of
magnitude.

The reason is structural, not incidental: the oracle probability is
`Phi(drift × sqrt(h))`, so a longer horizon separates the informed forecast
further from 0.5 and leaves skill more room to show. A 1-session direction call
is close to a coin flip *for everyone*, which compresses every arm into the
same score.

**Horizon 1 is unusable as a primary metric at this ROPE.** Its true effect
(−0.004) lies *inside* a ±0.005 band. That is not a power failure — it is the
ROPE correctly saying the effect is too small to matter. Running longer does
not help, and the table shows it: power stays at 0.01–0.07 out to 180 sessions.

## 2. Reaching a negative conclusion

§21.7 requires "no practically useful effect" to be a conclusion the study can
*reach*. Under the null, that takes duration:

| sessions | equivalence rate (h=5) |
|---:|---:|
| 20 | 0.01 |
| 60 | 0.23 |
| 120 | 0.61 |
| 180 | 0.83 |

A 60-session study that finds nothing will mostly return `INCONCLUSIVE`, not
`EQUIVALENT`. **If a credible null result is a goal of the study — and it
should be — the binding constraint is roughly 120–180 sessions, not the 60 that
suffices for 80% power against an effect.**

## 3. Effective sample size

The design effect is 1.1–1.4 on the paired difference: 4 panel instruments per
date yield the precision of about 2.9–3.4 independent ones. A 120-session study
has 480 panel items at one horizon and the information of roughly **385**.

Two things this required getting right, one of which was wrong at first:

- **Pairing removes most of the cross-sectional correlation.** Measured on a
  single arm the design effect is 1.35–1.73; on the paired difference it is
  1.16–1.20. The shared market factor makes both arms wrong together, and the
  difference cancels it. That is the paired design earning its keep, and it is
  now demonstrated rather than asserted.
- **Correlated outcomes alone do *not* correlate the scores.** The first
  version of the simulation had a market factor and no daily lean, and measured
  a design effect of exactly 1.00 at every skill level. The Brier score depends
  on the outcome only through a factor proportional to the forecaster's
  distance from 0.5, and with an idiosyncratic signal that factor points a
  different way on every instrument, so the correlation cancels. What actually
  makes a date's items move together is the *forecaster* leaning the same way
  across the panel — one macro headline, one bullish mood. That term had to be
  added, and without it this document would have told you cross-sectional
  correlation was a non-issue.

## 4. Detectable effect size

At 120 sessions, horizon 5, ROPE ±0.005:

| skill gap | mean effect (Brier) | power |
|---:|---:|---:|
| 0.05 | −0.0043 | 0.05 |
| 0.10 | −0.0082 | 0.45 |
| 0.15 | −0.0118 | 0.82 |
| 0.20 | −0.0151 | 0.94 |
| 0.30 | −0.0207 | 0.99 |

The mapping is close to linear at about **0.07 Brier points per unit of skill
gap** at this horizon. The minimum detectable effect at 80% power is a gap near
**0.15**, or ~0.012 Brier — roughly 2.4× the proposed ROPE half-width.

**This is the input the ROPE decision needs.** A ROPE of ±0.005 declares
"negligible" anything below a skill gap of about 0.07 — an arm recovering 7
points more of the available signal. Whether that is the right line is a
scientific judgement, not a computation; the table says what each choice costs.

## 5. Robustness of the longest horizon

At horizon 20 with 60 anchors, consecutive forecast windows share 19 of 20
sessions. The rule-of-thumb block length is `round(60^(1/3)) = 4`, which could
plausibly be too short for that much overlap. It is not:

| block length | false positive | power |
|---:|---:|---:|
| rule of thumb | 0.000 | 0.980 |
| 3 | 0.000 | 0.980 |
| 6 | 0.000 | 0.967 |
| 10 | 0.000 | 0.960 |
| 20 | 0.007 | 0.953 |

Quadrupling the block length costs under three points of power and leaves the
false-positive rate at or below 0.007. The horizon-20 result is not an artefact
of an over-short block.

## 6. Candidate metrics compared

| candidate | status | why |
|---|---|---|
| **Brier @ 5** | recommended primary | 80% power at 60 sessions, 94% at 120; effect comfortably outside a ±0.005 ROPE; design effect mild |
| Brier @ 20 | recommended secondary | most powerful, but 20-session horizons resolve slowly and a 120-session study yields only 100 non-overlapping windows |
| Brier @ 1 | **not viable as primary** | true effect sits inside the ROPE; power never exceeds 0.07 |
| Decision stability | implemented, unpowered here | `RepetitionStatistic.DISPERSION` runs it through the same pipeline, but it needs ≥2 repetitions and its effect size depends on model-level reporting noise, which no pilot has measured |
| Evidential fidelity | **not simulatable** | citation validity is a property of model behaviour, not of forecast skill. This DGP models skill. Powering it requires pilot data and nothing here can substitute |

The last two rows are the honest limit of this document. Simulating a fidelity
effect would mean inventing a model of how often an LLM miscites, which is
precisely the quantity the study is supposed to observe.

## 7. API cost

Projected for **120 sessions × 6 arms**, panel enabled (two elicitations per
condition per cycle), assumed profile: 5 turns, 400 fixed tokens, 1 500 granted
tokens for treated arms, 5 000 evidence tokens, 2 000 output tokens.

```bash
uv run marketlab cost --config configs/synthetic-pilot.yaml \
  --input-price 3 --output-price 15 --cached-input-price 0.30
```

Totals: **9.6M fresh input, 20.3M cacheable input, 2.9M output.**

| tier (in / out / cached, per Mtok) | total |
|---|---:|
| 0.50 / 2 / 0.05 | **$12** |
| 3 / 15 / 0.30 | **$78** |
| 3 / 15 / *no cache* | **$133** |
| 15 / 75 / 1.50 | **$390** |

Three things worth reading off this:

1. **Cost is not the binding constraint.** Even a frontier model runs a
   full-length study for a few hundred dollars. The design should be chosen on
   power and validity, not on price.
2. **Two thirds of the input is resent.** `DecisionAgent` sends every
   accumulated tool result on every turn, so input grows with roughly the
   square of the turn count. Prompt caching cuts the mid-tier bill from $133 to
   $78 — a 41% saving that costs nothing to obtain.
3. **Generated tokens are ~40% of the bill and cannot be cached.** The obvious
   lever — cutting the evidence budget — is worth less than it looks.

**Every figure above is `basis: ASSUMED`.** The platform now records real
provider usage (`TokenUsage` on every decision and panel bundle), and
`measure_profile` refuses to build a profile from a run that reported nothing —
which is exactly what a run against the deterministic fake reports. These
numbers should be replaced by measured ones after the first pilot against a
real model, and the CLI will say `MEASURED` when they are.

## 8. What this suggests, for the study owner to decide

| open question | what the numbers say |
|---|---|
| **1. Primary metric** | Brier @ 5 sessions. Brier @ 1 is not viable; Brier @ 20 as secondary |
| **3. ROPE** | ±0.005 is coherent: it excludes the effect at h=1 and admits it at h=5. It declares a skill gap below ~0.07 negligible |
| **Duration** | 60 sessions for 80% power against a 0.20 gap; **120–180** if reaching `EQUIVALENT` matters |
| **4. Budgets** | Not cost-constrained. Evidence budget cuts save less than expected; turn count is the expensive dimension |
| **6. Repetitions** | 1 is sufficient for the accuracy metrics. ≥2 only if stability is promoted to a primary metric |
| **7. Recall depth** | Untouched by this simulation — it sets the *true* effect size, which is assumed here, not derived. Needs a pilot |

## 9. What would falsify this document

The simulation assumes a world in which a real edge exists and arms recover
fractions of it. If a real model's forecasts are essentially uninformative —
all arms near 0.5 regardless of treatment — then every effect size here is
optimistic and the required durations are underestimates.

**The pilot must therefore report the observed spread of forecast probabilities
before any duration from this document is relied on.** A model whose answers
cluster within ±0.02 of 0.5 has no skill to differentiate, and the study would
be measuring reporting noise.
