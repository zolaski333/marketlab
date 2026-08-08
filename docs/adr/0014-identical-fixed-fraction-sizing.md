# ADR-0014 — Every arm sizes identically, so no arm can win by sizing

- **Status:** accepted
- **Implemented by:** `marketlab.execution.policy`
- **Checked by:** `tests/unit/test_execution_policy.py`, `tests/unit/test_execution_failures.py`

## Context

An agent that says "buy Alpha" has not said how much. Something has to decide,
and whatever decides it is part of the experiment.

Letting the agent size is the realistic choice: a real trading agent decides
conviction as well as direction. It is also the choice that destroys the
study's ability to answer its own question.

Portfolio returns are dominated by sizing. An arm that happened to bet larger
on its winners would show a better equity curve with no better forecasts, and
a difference in *sizing behaviour* between arms — plausibly caused by memory,
since a model shown its past positions might anchor on them — would appear in
the results as a memory effect on decision quality. There would be no way to
separate the two after the fact.

## Options considered

**The agent states a size or a weight.** Rejected. Realistic, and it makes the
primary comparison uninterpretable. It also multiplies what the study is
testing: direction, timing *and* sizing, on a sample sized for one of them.

**The agent states a conviction level, mapped to a size by a fixed table.**
Rejected as the same problem with extra steps. A conviction channel is a sizing
channel, and it would differ between arms.

**Size proportional to the stated probability** (Kelly-like). Rejected for a
subtler reason: it makes the portfolio outcome a deterministic function of the
forecast, so the "secondary, exploratory" equity measure would carry no
information the primary metric does not already have — while looking like an
independent confirmation.

**A fixed fraction of equity, identical for every arm.** Chosen.

## Decision

`ExecutionPolicy` sizes every position at a fixed `target_weight` of the
portfolio's equity, with the same value for every arm, declared in
`StudyConfig` and fingerprinted with everything else.

The same applies to every other execution parameter: fee schedule, minimum
notional, participation cap, spread. **Every arm faces the same market and the
same costs.**

`TradeIntent` therefore carries no size and no weight, structurally — there is
nowhere for a model to put one.

## Consequences

**No arm can win by sizing, and no arm can demonstrate skill at it.** Both
halves are true and the second is a real loss. If memory's genuine contribution
to a trading agent is knowing when to bet big, this study is blind to it and
would report a null.

**The equity path measures direction and timing only.** That makes it a weak
secondary measure, which is consistent with the design treating financial
performance as secondary and exploratory. A positive return is never treated as
evidence of competence.

**Fee minimums interact with the weight.** A 5% target weight on a small
portfolio produces orders below the minimum notional, which are rejected as
`BELOW_MINIMUM_SIZE`. Starting capital and target weight therefore have to be
chosen together, and they are still open in
[PRE_REGISTRATION.md](../PRE_REGISTRATION.md) §6.

**Fee magnitudes are placeholders.** 5bp with a 1.00 minimum for equities is in
the spirit of retail brokerage, not a calibrated cost model. They are
pre-registered in the sense that every arm pays the same — which is what the
comparison needs — and they are not claimed to be realistic.

**The bearish direction is barely expressible.** With no short selling, an arm
confident that something will fall can only decline to buy it. Combined with
fixed sizing, the portfolio's expressive range is narrow, and that bounds what
the secondary measure can show.

## What would make us revisit this

- **A study whose question is about position sizing.** Then this decision is
  exactly backwards, and the design needs a sizing channel plus a much larger
  sample.
- **Evidence that arms differ in *what* they trade rather than how confidently.**
  That would make the fixed-weight portfolio a fair comparison of selection
  skill and worth promoting from exploratory to secondary — a change to the
  pre-registration, not to the code.
- **Short selling being modelled.** It would widen the expressible range enough
  that the portfolio measure starts carrying information, and would need its
  own record covering margin, borrow cost and the accounting.
