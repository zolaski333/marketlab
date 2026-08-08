# ADR-0001 — Four arms as a crossed 2×2 of grants, with matched placebos

- **Status:** accepted
- **Implemented by:** `marketlab.experiments.arms`
- **Checked by:** `tests/unit/test_arms.py`, `tests/integration/test_materials_wiring.py`

## Context

The specification names six conditions — A, B, C, D, B′, C′ — and says what
each is for. It does not say what each one *grants*, and the difference is the
whole experiment: an arm is only a treatment if you can state what was added.

The naive reading is three arms: nothing, memory, memory-and-reflection. It
fails immediately. If C beats A, the difference could be memory, or reflection,
or the interaction, and nothing in the data distinguishes them. The write-up
would attribute the effect to whichever factor it happened to name first, and
no reader could contradict it.

The second problem is subtler. Memory and reflection are delivered as **text
injected into the prompt**. A model handed several hundred extra words of
plausible, on-topic prose may perform differently for reasons that have nothing
to do with what the prose says — priming, an implied instruction to think
harder, a longer context window changing attention. If B beats A, "memory
helped" and "being handed some writing helped" both fit.

## Options considered

**Three arms (A, B, C).** Cheapest. Rejected: it cannot separate the two
factors, which is the primary question.

**Four arms, un-crossed (A, B, C, and a "more memory" arm).** Would measure a
dose-response on memory. Rejected: it answers a question nobody asked and still
leaves reflection confounded.

**A crossed 2×2 without placebos (A, B, C, D).** Separates the factors. This is
the design most of the literature would stop at. Rejected as insufficient: it
leaves every positive result open to the "it was just more text" reading, and
that reading is *especially* live for LLM agents, where prompt length is known
to change behaviour on its own.

**A crossed 2×2 plus matched placebos for both memory-bearing arms.** Chosen.

**Placebos for all four arms.** A placebo for D as well would isolate
reflection-content from reflection-shaped-text. Rejected on cost: it adds a
seventh and eighth condition — a third more model spend and a third more
multiplicity burden — for a comparison that only matters if D shows an effect.
Recorded as a thing to add if it does.

## Decision

An arm is a pair of grants and nothing else:

| Arm | memory | reflection | placebo of |
|---|---|---|---|
| A | `NONE` | `NONE` | — |
| B | `GENUINE` | `NONE` | — |
| C | `GENUINE` | `GENUINE` | — |
| D | `NONE` | `GENUINE` | — |
| B′ | `PLACEBO` | `NONE` | B |
| C′ | `PLACEBO` | `PLACEBO` | C |

Two consequences follow from encoding it this way rather than as behaviour:

1. **Nothing downstream branches on an arm's name.** The moment a code path
   reads `if arm_id == "C"`, the study measures that branch. Everything
   downstream reads the grants.
2. **"Is B′ a correct placebo for B?" becomes computable.**
   `is_matched_placebo` compares the two specs coordinate by coordinate: every
   channel the genuine arm leaves empty must be empty in the placebo, every
   channel it grants genuinely must be granted as placebo, and at least one
   channel must actually be substituted. A future arm added with a mismatched
   shape fails a test instead of quietly entering the study as a seventh
   condition nobody declared.

D is coherent because the two channels are defined as genuinely separable:
memory is raw episodic recall, reflection is distilled strategy produced by a
process that reads the run's record. **Under D the reflection process reads the
history; the agent does not.** D is therefore the cell that separates "having
been told what works" from "being able to look up what happened".

Placebo material is produced by passing fabricated episodes through the *same
renderer* as genuine ones, sized from the arm's own recorded episode shapes —
so length and line count match by construction rather than by estimate.

## Consequences

**What it buys.** B−A is the memory main effect. D−A is the reflection main
effect. C−(B+D−A) is the interaction. B−B′ separates content from prose. B′−A
measures the prose alone, and is a result worth reporting on its own.

**What it costs — six arms, not three.** Every cycle runs six elicitations plus
six panels. Model spend doubles against the naive design. The confirmatory
family is five contrasts wide, so the multiplicity correction is materially
harsher and the study needs more sessions to reach the same corrected power.

**A placebo matches shape, not information content.** It is matched line for
line and to within 2% on length, and contains no instrument, probability or
failure kind from the genuine record. It is **not** matched on how plausible or
engaging the text is, and that is not measurable here. If a model can tell the
two apart for any reason other than content — a stylistic tell in the
fabricated identifiers, an implausible uniformity — then B′ has stopped being a
control and B−B′ measures something else. Nothing in this repository can detect
that; only a pilot examining the model's own behaviour could.

**Placebo reflection carries a fixed three rules.** Matching the genuine rule
count would require reading the genuine reflection — which for C′ means reading
C's, i.e. genuine content crossing into a placebo. The resulting length
difference is bounded and measured rather than assumed.

## What would make us revisit this

- **D shows an effect.** Then reflection-shaped-text becomes a live
  alternative explanation and D needs its own placebo (D′), even at the cost of
  a seventh arm.
- **A pilot shows a model can identify placebo material.** Then the placebo
  construction is the thing to fix, not the design — but the B−B′ contrast
  would have to be reported as uninterpretable until it is.
- **The interaction turns out to be the interesting quantity.** The current
  design estimates it as a difference of differences, which is the noisiest
  thing a 2×2 produces. Powering *that* rather than the main effects would
  change the recommended duration substantially.
