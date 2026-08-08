# ADR-0004 — Point-in-time correctness enforced by a package boundary

- **Status:** accepted
- **Implemented by:** `marketlab.core.cutoff`, `marketlab.snapshots.builder`, `marketlab.retrieval`
- **Checked by:** `tests/security/test_decision_path_isolation.py`, `tests/unit/test_cutoff.py`, `tests/property/test_temporality.py`

## Context

An agent that sees data from after the instant it is deciding at will look
brilliant. Lookahead is the single most common way a backtest lies, and it is
usually introduced by one query that forgot one `WHERE` clause.

The defect has a specific shape: it is a **local mistake with a global
consequence**, invisible in review because the offending line looks like every
other query, and invisible in the results because the results look *good*.

## Options considered

**Discipline: every query filters on the cutoff.** Rejected. It is exactly the
rule that a hurried change breaks, and there is no artefact that could
contradict a claim to be following it.

**A repository layer that always applies the cutoff.** Better — one place to
get it right. Rejected as insufficient: nothing stops a caller from reaching
past the repository to the session it holds. It reduces the number of places
the mistake can be made without reducing it to zero.

**A runtime assertion: compare every returned row's timestamp to the active
cutoff.** Rejected as a primary mechanism (kept as a secondary one). It catches
the mistake at the moment the study is being run, which is late, and only on
rows that a test actually exercises.

**Forbid the decision path from being able to query at all.** Chosen.

## Decision

**Packages on the decision path may not import SQLAlchemy.** Not "should not" —
`tests/security/test_decision_path_isolation.py` walks the AST of every module
under `agents/`, `retrieval/` and `forecasting/` and fails on any import of it,
direct or aliased.

A package that cannot hold a database session cannot write a query that forgets
its cutoff. The mistake is not made less likely; it is made unavailable.

What those packages receive instead is a **frozen snapshot**: a point-in-time
object built once per cycle by `SnapshotBuilder`, containing the universe as it
stood and the evidence visible at that instant, and nothing else. The retrieval
tools search an index over that object. There is no live data behind them.

Two temporal conventions, deliberately different, each stated where it applies:

- **`Cutoff.allows` uses `<=`.** Data first *seen* exactly at the cutoff was
  seen no later than it, so it is visible. Availability is gated by
  `first_seen_at` — when the platform received the data — never by a source's
  claimed publication time.
- **`MemoryStore.recall` uses `<`, strictly.** An episode is written at the
  same instant the decision it records was made. `<=` would hand a condition
  its own current decision as though it were history, and the resulting text
  would be indistinguishable from a correct one.

The asymmetry is not an inconsistency: the first is about *observing the
world*, the second about *observing yourself*, and self-observation is the one
that can be circular.

## Consequences

**The guard shaped the code rather than being retrofitted to it.** When the
imposed panel was built, it could not persist itself — `forecasting/` may not
hold a session — so the panel store lives in `evaluation/panels.py` instead.
That is the guard doing its job before there was anything to catch.

**Snapshots cost memory and build time.** Every cycle materialises the whole
visible universe and evidence set. For the synthetic world this is trivial; for
a real universe of thousands of instruments and years of news it would not be,
and the snapshot would need to become lazy — which is the point at which this
decision gets genuinely expensive.

**The boundary is on imports, not on semantics.** A module in `agents/` could
still receive a badly-built snapshot and would have no way to know. The
snapshot builder itself *does* hold a session, and it is the one place the
cutoff logic must be right. That code is small, tested directly, and covered by
property tests that generate arbitrary cutoffs and assert nothing later is ever
visible.

**Three packages are protected, not the whole system.** `evaluation/`,
`analysis/` and `experiments/` legitimately query across time — resolution
*must* read the future relative to a forecast, since that is what resolving
means. Widening the guard would be wrong, not safer.

## What would make us revisit this

- **A real universe large enough that eager snapshots stop fitting in
  memory.** A lazy, cutoff-aware view would reintroduce query construction on
  the decision path, and this record would need superseding with a much more
  careful design.
- **A fourth package joining the decision path.** The guard's package list is
  explicit. Adding a package without adding it to the list is a silent gap; the
  test names the list, so at least the omission is visible in one place.
- **A dependency that pulls SQLAlchemy in transitively.** The scan checks
  imports written in these packages, not their transitive closure. A helper
  library that exposes a session would pass.
