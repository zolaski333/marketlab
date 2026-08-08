# ADR-0003 — Masking is structural on the model side and absent on the analysis side

- **Status:** accepted
- **Implemented by:** `marketlab.models.types`, `marketlab.agents.decision.ConditionContext`, `marketlab.experiments.context`
- **Checked by:** `tests/security/test_condition_isolation.py`, `tests/security/test_decision_path_isolation.py`

## Context

The specification asks for partial masking (§5.4). "Partial" is the important
word, and a document that only describes the half that works is worse than no
document at all — a reader will assume the other half works too.

There are two things that could be masked, and they fail differently.

**The model.** If the thing being measured can tell which condition it is in,
it can behave differently for that reason. This is not hypothetical: the audit
of this project's predecessor found a mock model that branched on
`condition_id`. That implementation was manufacturing its own effect and
reporting it as a finding.

**The analyst.** If whoever runs the analysis knows which arm is which while
choosing the metric, the horizon or the equivalence band, the choice can be
made — without any dishonesty — in the direction the data already favours.
This is the ordinary garden of forking paths, and it is the more likely failure
of the two, because it does not require anyone to write any bad code.

## Options considered

**Mask the model by convention.** A rule that says "do not pass the arm id to
the model", enforced by review. Rejected: the predecessor had that rule.

**Mask the model by naming discipline.** Forbid fields called `arm_id` or
`condition` on the model path. Rejected as insufficient on its own — a field
called `context_variant` carrying `"C_PRIME"` passes a name check and leaks
everything.

**Mask the model structurally, and check both the field names and the
content.** Chosen.

**Mask the analyst by automating the analysis end to end with arm labels
stripped.** Considered seriously and rejected. Somebody has to map the
anonymised labels back to arms to write the paper, and with six arms and one
2×2 structure the mapping is usually recoverable from the results themselves
(the placebos pair with their genuine arms; A is the one with no material).
Label-stripping here would produce the *appearance* of blinding without the
substance, which is worse than stating the truth.

**State that analyst masking does not exist, and constrain the analyst a
different way.** Chosen.

## Decision

### Model side: structural, and verified on content

`ModelRequest` has exactly one field through which a condition may differ:
`injected_context`, the memory/reflection/placebo text itself, or `None`. There
is no field for the arm, the repetition or the run.

`marketlab.experiments.context` is the **single** place in the platform where
code both knows which arm is running and decides what that arm receives. A
materials provider returns *text or nothing* — not a context object — so it
cannot also vary the turn budget between arms, which would confound the
comparison with an allowance difference having nothing to do with memory.

Three guards, each of which would fail differently:

1. **Field-shape scan.** `tests/security/test_condition_isolation.py` inspects
   the dataclass fields of every type in `marketlab.models` and the signature
   of `LanguageModel.generate`, and asserts no arm/condition/repetition field
   exists.
2. **Content scan.** The same test runs a real six-arm cycle, captures every
   `ModelRequest` actually produced, and asserts that no arm's identifier
   appears anywhere in its serialised content. This is the guard that catches a
   leak through a field with an innocent name.
3. **Import scan.** `tests/security/test_decision_path_isolation.py` forbids
   `agents/`, `retrieval/` and `forecasting/` from importing SQLAlchemy at all,
   so nothing on the decision path can query for its own identity — see
   [ADR-0004](0004-point-in-time-by-package-boundary.md).

### Analysis side: none, and the mitigation is pre-registration

The database stores arm identifiers in the clear. `marketlab analyse` prints
them. Whoever runs it knows exactly which arm is which.

The mitigation is not blinding, it is **removing the analyst's discretion
before the data exist**:

- `AnalysisPlan` cannot be constructed without a ROPE
  ([ADR-0009](0009-no-default-rope.md)). The band cannot be chosen after
  looking.
- The pairing, aggregation, bootstrap and TOST are one fixed pipeline
  ([ADR-0008](0008-date-aggregation-block-bootstrap-tost.md)), not a menu.
- The confirmatory contrast family and the horizons are declared in
  [PRE_REGISTRATION.md](../PRE_REGISTRATION.md), and the multiplicity
  correction runs over the whole declared family — comparisons with no data are
  *skipped and excluded*, never given a fabricated p-value that would inflate
  the correction and make the survivors look stronger.
- The run configuration is fingerprinted and re-declaration with changed
  parameters is refused ([ADR-0011](0011-a-run-is-declared-not-launched.md)).

## Consequences

**A model cannot condition on its arm.** Not "does not" — cannot, without a
change that three tests would catch.

**An analyst still can.** The pre-registration is the only thing standing
between this study and a forking path, and its credibility rests entirely on
being tagged in a signed commit **before** the first confirmatory run. If that
ordering is not verifiable by a stranger, the analyst-side masking claim is
worth nothing, and this record should be read as saying so.

**The content scan is only as good as the cycle it runs on.** It inspects the
requests a real six-arm cycle produced. A leak on a code path that cycle does
not exercise — an error branch, a provider-specific retry — would not be seen.

**"Condition-blind" does not mean "identical".** Arms are *supposed* to receive
different text; that is the treatment. What is masked is the **label**, not the
material. A model sophisticated enough to infer "this reads like fabricated
filler, so I am in a control group" defeats this entirely, and no structural
guard can prevent it — see [ADR-0001](0001-crossed-design-with-matched-placebos.md).

## What would make us revisit this

- **A provider whose API requires a per-request identifier** (a session key, a
  cache key, a customer-supplied trace id). Any of those could carry the arm.
  A Phase 3 adapter must derive such identifiers from something that is not the
  condition, and the content scan must be extended to cover them — see
  [PROVIDER_POLICY.md](../PROVIDER_POLICY.md).
- **Two analysts.** With more than one person, genuine analyst blinding becomes
  possible: one holds the mapping, the other runs the plan. If this study grows
  past one person, that is worth doing and this record should be superseded.
- **Any need to look at results mid-study.** There is currently no interim
  analysis and no stopping rule. Adding either would require an alpha-spending
  decision recorded here first, because an unplanned peek is exactly the kind
  of discretion the pre-registration is supposed to remove.
