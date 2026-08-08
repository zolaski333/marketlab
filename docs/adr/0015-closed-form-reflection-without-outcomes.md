# ADR-0015 — Reflection is closed-form, and memory records decisions rather than outcomes

- **Status:** accepted
- **Implemented by:** `marketlab.reflection.engine`, `marketlab.memory.store`
- **Checked by:** `tests/unit/test_memory_materials.py`

## Context

Two questions about the treatment, which look like implementation details and
are not.

**Who writes the reflection?** A real deployment would ask a model to reflect
on its own record and produce strategy notes. That is what "strategic
reflection" means in practice.

**Does an episode carry what happened to it?** Memory currently records what a
condition *decided* — its forecasts, its intents, its equity at the time. It
does not record whether the forecast came true. Resolution exists, so the raw
material for outcome feedback is available.

Both are places where the obviously-better implementation is a **different
treatment**, not a better version of the same one.

## Options considered

### Model-authored reflection

**Ask the model to reflect.** Realistic and probably stronger. Rejected for
Phase 1 on two grounds. It cannot be replayed — a reflection produced by an
opaque, non-deterministic process is not reconstructible from the record, so
[ADR-0012](0012-replay-recomputes-downstream-of-the-model.md)'s guarantee would
stop covering the treatment itself. And it doubles the elicitations that
already have no real provider behind them.

**Closed-form rules derived from the condition's own record.** Chosen for now.
`derive_rules` produces observations like side persistence across episodes,
each carrying how many episodes support it.

### Outcome feedback in memory

**Show the condition whether its forecasts came true.** Rejected — deliberately
deferred rather than implemented. §13 defines each arm by what it is granted,
and **an arm shown its own hit rate is a different arm from one shown its own
past decisions.** Adding outcomes would silently change what B and C *are*,
mid-design, without changing their names.

It also carries two requirements that are easy to get wrong:

- It must be point-in-time: only outcomes whose *target* session is strictly
  before the recall cutoff, or the agent is shown the future
  ([ADR-0004](0004-point-in-time-by-package-boundary.md));
- The placebo would have to match the **resolved** forecast count, not the
  forecast count, or B′ becomes distinguishable from B by length alone.

**Record decisions only.** Chosen for now.

## Decision

Reflection is deterministic and closed-form. It returns `None` when there is too
little history to say anything — an empty reflection would still be *material*,
and handing a condition a page saying nothing would confound "reflection" with
"more text".

Memory episodes record forecasts, intents, equity and failure kinds. **No
reflection rule claims accuracy, and a test asserts it.**

Both are raised as open questions 9 and 10 for the study owner in
[ROADMAP.md](../ROADMAP.md) rather than decided here. This record fixes what is
in force today and why it is not merely an omission.

## Consequences

**The treatment is replayable.** Everything a condition was granted can be
reconstructed from the persisted record, which is what makes the study's
central artefacts verifiable at all.

**The treatment is weaker than a real deployment's.** Closed-form rules about
side persistence are a thin distillate compared to what a model would write
about its own record. If the study finds no reflection effect, "reflection does
not help" and "*this* reflection is too weak to help" are not distinguishable
from the result — and the second is the more likely explanation. **This is the
single most important limitation on how far a null result generalises**, and it
belongs in any write-up's first paragraph, not its appendix.

**Memory without outcomes is closer to a diary than to learning.** A condition
can see what it did and not whether it worked. That is a coherent treatment —
"does having your own history help?" is a real question — but it is a weaker
one than "does having your own *track record* help?", and the literature's
intuitions about memory mostly concern the latter.

**Swapping either in changes no caller.** Both are behind interfaces that
already fit. The barrier is scientific, not technical: either change requires
re-piloting and a new pre-registration, and the ease of the code change is
exactly what makes writing this down necessary.

## What would make us revisit this

- **A pilot showing the closed-form reflection is ignored.** If a model treats
  the strategy notes as noise, the treatment is not being delivered and
  model-authored reflection becomes necessary rather than optional — accepting
  the replay cost, which would then have to be documented as a hole in
  [ADR-0012](0012-replay-recomputes-downstream-of-the-model.md).
- **A study owner deciding outcome feedback is the question.** Then B and C are
  redefined, the placebos are re-matched on resolved counts, resolution moves
  inside the cycle ([ADR-0006](0006-total-return-resolution-on-the-run-grid.md)),
  and this record is superseded rather than amended.
- **Both at once**, which is what a realistic deployment looks like. That is a
  different study from this one, and should be run under a different `run_id`
  and compared, not substituted.
