# Architecture decision records

One file per decision that would be expensive to reverse and that a reader
cannot infer from the code alone.

## Read this first: these were written after the fact

Every record here was written at task #5, after the decision it describes was
already implemented. That is worth saying plainly, because a retrospective ADR
has a specific failure mode: it can quietly rationalise whatever was built,
listing as "options considered" only the ones that make the built thing look
inevitable.

Two things limit that here, neither of them perfect:

- Each record names the **module or test that implements it**, so a reader can
  check the record against the code rather than against the author's memory.
- Each record's *Consequences* section is required to state a cost, not only a
  benefit. A record whose consequences are all favourable is a record that has
  not been thought about.

The reasoning itself is not new — it lived in module docstrings and in the
["Decisions taken that depart from the specification"](../ROADMAP.md) table
since the code was written. What these files add is the part a docstring never
carries: **what was rejected, what it would have cost, and what would make us
change our mind.**

## Status vocabulary

| Status | Meaning |
|---|---|
| `accepted` | In force. The code implements it. |
| `provisional` | In force, but taken on thinner evidence than we would like. The record says what evidence would settle it. |
| `superseded by NNNN` | No longer in force. The file stays; deleting it would erase the fact that the question was once answered differently. |

No record here is `superseded` yet. That is a fact about the project's age, not
about the quality of the decisions.

## The records

### The experimental design

| # | Decision | Status |
|---|---|---|
| [0001](0001-crossed-design-with-matched-placebos.md) | Four arms as a crossed 2×2 of grants, with matched placebos | `accepted` |
| [0002](0002-imposed-panel-as-unit-of-analysis.md) | The imposed panel, not the free decision, is the unit of analysis | `accepted` |
| [0003](0003-masking-is-partial.md) | Masking is structural on the model side and absent on the analysis side | `accepted` |
| [0016](0016-treatment-is-endogenous-to-trajectory.md) | The treatment is endogenous to each arm's own trajectory, and this is not fixable | `accepted` |
| [0014](0014-identical-fixed-fraction-sizing.md) | Every arm sizes identically, so no arm can win by sizing | `accepted` |
| [0015](0015-closed-form-reflection-without-outcomes.md) | Reflection is closed-form, and memory records decisions rather than outcomes | `accepted` |
| [0017](0017-a-fake-that-ignores-its-context.md) | The shipped model ignores the material it is granted | `accepted` |

### The measurement

| # | Decision | Status |
|---|---|---|
| [0006](0006-total-return-resolution-on-the-run-grid.md) | Forecasts resolve in total return, on the run's own session grid | `accepted` |
| [0007](0007-brier-only-no-log-score.md) | Brier is the scoring rule; the log score is refused | `accepted` |
| [0008](0008-date-aggregation-block-bootstrap-tost.md) | Aggregate to dates, bootstrap by blocks, decide with a three-valued TOST | `accepted` |
| [0009](0009-no-default-rope.md) | The analysis plan cannot be constructed without a ROPE | `accepted` |
| [0010](0010-complete-cases-only.md) | Complete cases only; imputation is not offered | `accepted` |

### The record

| # | Decision | Status |
|---|---|---|
| [0004](0004-point-in-time-by-package-boundary.md) | Point-in-time correctness enforced by a package boundary | `accepted` |
| [0005](0005-append-only-history-hash-chained-by-sequence.md) | History is append-only and hash-chained over a monotonic sequence | `accepted` |
| [0011](0011-a-run-is-declared-not-launched.md) | A run is declared; re-declaring it with changed parameters is refused | `accepted` |
| [0012](0012-replay-recomputes-downstream-of-the-model.md) | Replay recomputes everything downstream of the model, and only that | `accepted` |
| [0013](0013-determinism-from-an-explicit-key-stream.md) | Randomness comes from an explicit key stream, never from `random` | `accepted` |

## Writing a new one

Copy the shape of any existing record. `tests/test_documentation.py` requires
every file matching `NNNN-*.md` to carry the five headings and to appear in the
table above — an ADR nobody indexed is an ADR nobody will read.

Number sequentially. Do not renumber, and do not delete: a decision that turns
out wrong is superseded by a new record that says so, and the old file stays as
the evidence that the question was live.
