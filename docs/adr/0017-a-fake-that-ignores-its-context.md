# ADR-0017 — The shipped model ignores the material it is granted

- **Status:** accepted
- **Implemented by:** `marketlab.models.deterministic.DeterministicPolicyModel`
- **Checked by:** `tests/unit/test_deterministic_policy.py`, `tests/integration/test_multi_arm_wiring.py`, `tests/integration/test_materials_wiring.py`

## Context

Phase 1 has no real provider. Something has to stand in for a language model so
the pipeline can be built and tested end to end, and what that stand-in does is
a scientific decision, not a testing convenience.

This project's predecessor got it wrong in a specific, instructive way: its
mock model **branched on `condition_id`**. Every run therefore produced
different decisions per arm, every analysis found an effect, and the effect was
manufactured by the test double. The validation report presented it as a
result.

The tempting fix — a fake that reads `injected_context` and varies its output
by what it contains — is the same mistake in better taste. It produces arms
that differ, which looks like the pipeline working, and any comparison run
against it would measure the fake's own text-sensitivity.

## Options considered

**A fake that branches on the condition.** Rejected. This is the defect the
audit found.

**A fake that reads `injected_context` and varies its forecasts by its
content.** Rejected. It manufactures an effect through a legitimate channel,
which makes the manufactured effect *harder* to spot, not easier. Anyone
running `marketlab analyse` against it would get significant results.

**A random fake.** Rejected: not replayable, and a study whose stand-in model is
noise cannot distinguish a broken pipeline from a working one.

**A deterministic closed-form function of the market data, blind to the
injected context.** Chosen.

## Decision

`DeterministicPolicyModel` is a closed-form function of the closing price. It
receives `injected_context` — the channel is live and the text really arrives —
and **does not read it**.

The consequence is stated bluntly wherever a reader might be misled, including
the first screen of the README: **all six arms are shown different things and
decide identically, so an arm comparison run against this model is a comparison
of nothing.**

That property is pinned by a test rather than left as an intention. A fake that
started reading its context would break `tests/integration/test_multi_arm_wiring.py`,
which asserts every arm decides the same under the null materials provider.

**Separately, a test double that *does* read its context is used in
`tests/integration/test_materials_wiring.py`** and produces different decisions
per arm through the identical pipeline. That is what establishes the channel is
live: without it, "the arms decide identically" would be consistent with the
material never arriving at all.

## Consequences

**No result in this repository says anything about memory or reflection.** The
README says so, the ROADMAP says so, and a release-readiness test asserts the
README keeps saying so.

**The two facts a reader needs are separated and both are checkable.** *The
material arrives* is proven by one test with a context-reading double. *The
shipped model ignores it* is proven by another. Neither is inferred from the
other.

**The pipeline is fully exercised without a provider.** Every downstream
component — execution, accounting, resolution, analysis, replay — runs against
real decisions in real volume, and a full six-arm twenty-session study takes
about eleven seconds.

**The A/A property is a genuine guard.** Under the null materials provider,
every arm receiving nothing must decide identically. Any difference is a defect
in the plumbing, and this catches leakage paths the field-level and
content-level condition-isolation scans would miss.

**Nothing about a real model's behaviour is established here.** Whether a real
provider attends to injected context at all is the central empirical risk of
the study, stated in [PRE_REGISTRATION.md](../PRE_REGISTRATION.md) §10, and no
amount of work on the fake reduces it.

## What would make us revisit this

- **A real provider adapter landing (Phase 3).** The fake remains for tests;
  the README's warning changes only when a pilot has actually run.
- **A need for a fake that exercises failure paths** — malformed JSON,
  refusals, hallucinated tickers. Those exist as separate, clearly-named test
  doubles rather than as modes of the deterministic policy, so that no run can
  accidentally use one and no reader can mistake one for the shipped model.
- **Never** for a fake that reads its context and is used as the default. The
  reason this record exists is that the mistake looks like an improvement.
