# ADR-0013 — Randomness comes from an explicit key stream, never from `random`

- **Status:** accepted
- **Implemented by:** `marketlab.core.rng.DeterministicRng`
- **Checked by:** `tests/unit/test_ordering.py`, `tests/unit/test_bootstrap.py`

## Context

Two things in this platform are random, and both end up in a published claim:

- **Arm execution order** within a cycle (§13.4), which is what stops provider
  drift during a cycle from being confounded with the condition;
- **Bootstrap resampling**, which produces the confidence interval the study's
  conclusion rests on.

Both must be reproducible years later, by a stranger, on a different machine.

The obvious tool is `random.Random(seed)`. It is not sufficient, for a reason
that is easy to miss: **CPython guarantees the reproducibility of `random()`
for a given seed, but not of `shuffle`, `sample` or `choices`.** Those are
implementations built on top of it, and they have changed between CPython
versions — `random.sample` and `random.shuffle` have both been reimplemented in
the language's history.

A replay run in 2030 on a newer interpreter would produce a different arm order
and a different bootstrap interval from the same seed, and would report a
divergence that is not a defect — or worse, would silently publish a different
number.

`numpy.random.Generator` fixes this properly with a documented stream
guarantee, at the cost of a dependency this project otherwise does not need on
its hot path.

## Options considered

**`random.Random(seed)` with `shuffle`/`choices`.** Rejected: no cross-version
stream guarantee for exactly the functions used.

**`random.Random(seed)`, using only `random()` and implementing shuffle and
sampling by hand.** Close to what was chosen, and rejected only because
Mersenne Twister's state initialisation from a string seed is itself an
implementation detail, and because it leaves a `random` import on a path where
someone will eventually call `shuffle` on it.

**`numpy.random.Generator(PCG64(seed))`.** Genuinely defensible — NumPy
documents a stream compatibility policy. Rejected because it makes NumPy
load-bearing for the arm ordering, which lives in `experiments/` and otherwise
needs nothing, and because the compatibility policy is a policy rather than a
specification.

**A key stream derived from SHA-256, with Fisher-Yates, rejection sampling and
Box-Muller implemented explicitly.** Chosen.

## Decision

`DeterministicRng` derives its randomness from a SHA-256 key stream over an
explicit label and counter. Every consumer implements its own algorithm on top:

- **Fisher-Yates** for the arm ordering shuffle;
- **rejection sampling** for bounded integers, so no modulo bias enters the
  block bootstrap's index selection;
- **Box-Muller** where normal variates are needed.

`import random` appears nowhere on any path that produces a published number.

The seed is part of `StudyConfig` and therefore part of the run's fingerprint
([ADR-0011](0011-a-run-is-declared-not-launched.md)): a run cannot be re-run
with a different seed under the same identifier.

## Consequences

**Reproducibility rests on SHA-256 and on arithmetic written in this
repository**, both of which are specified and neither of which is an
interpreter implementation detail. The same seed produces the same arm order
and the same bootstrap interval on any Python that runs this code at all.

**Three algorithms are implemented here that a standard library would have
provided.** That is code to get right, and getting Fisher-Yates or rejection
sampling subtly wrong would bias a result rather than crash. They are small,
directly tested, and their outputs are pinned by tests with hand-computed
expectations.

**It is slower than `random`.** One SHA-256 per draw against a Mersenne Twister
step. Irrelevant at the volumes here — a few hundred shuffles and a few hundred
thousand bootstrap indices — and it would matter at a million resamples.

**Statistical quality is not the point and is not claimed.** SHA-256 in counter
mode is a perfectly good source for shuffling six arms and resampling a few
dozen dates; it is not a cryptographic RNG and it is not tuned for speed. What
it is, is *stable*.

## What would make us revisit this

- **Bootstrap resamples reaching a scale where hashing dominates runtime.** The
  power simulation runs 200 replications × 400 resamples and is comfortable; an
  order of magnitude more would make this worth measuring.
- **A published stream guarantee in the standard library.** If CPython ever
  documents `shuffle` as stable across versions, this becomes unnecessary
  complexity.
- **NumPy becoming a core dependency for other reasons.** Then its generator's
  compatibility policy would be free, and the hand-written algorithms would be
  a liability rather than an asset.
