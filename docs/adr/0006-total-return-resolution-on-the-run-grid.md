# ADR-0006 — Forecasts resolve in total return, on the run's own session grid

- **Status:** accepted
- **Implemented by:** `marketlab.evaluation.resolution`
- **Checked by:** `tests/unit/test_resolution.py`, `tests/unit/test_resolution_store.py`, `tests/unit/test_corporate_actions.py`

## Context

A forecast says "instrument X will be up over N sessions". Turning that into a
0 or a 1 requires answering two questions the phrase does not answer: **up
compared to what**, and **which session is the Nth one**.

Both have an obvious answer that is wrong.

**Up compared to what.** The obvious answer is "the closing price N sessions
later, versus the closing price now". On a session where the instrument goes
2-for-1, the quote halves. Every arm that forecast that instrument is scored as
having predicted a 50% loss. The synthetic world genuinely halves the quote on
its split session precisely so that this failure is reproducible rather than
theoretical.

**Which session is the Nth.** The obvious answer is the instrument's own
trading calendar. But the universe spans US equity, EU equity and 24/7
calendars, and the world prices everything at the US session close. Counting
five sessions on a 24/7 calendar lands on an instant at which no snapshot
exists and no price was ever frozen — so the resolver would have to interpolate,
or pick a nearby bar, or fail.

## Options considered

**Raw close-to-close.** Rejected: scores every corporate action as a forecast
error, identically for every arm, adding noise that is pure measurement
artefact.

**Split-adjusted price only.** Fixes splits, ignores dividends. Rejected as
half a decision: a dividend-paying instrument that trades flat has genuinely
returned something, and treating that as "not up" is a real bias against
income-heavy instruments, which is a bias that varies across the universe.

**Total return, with dividends reinvested.** The convention most index
providers use. Rejected as inconsistent with the books: this platform's ledger
credits dividend cash and earns nothing on it (there is no interest and no
auto-reinvestment). Scoring forecasts under a reinvestment convention the
portfolio does not follow would mean the forecast metric and the equity curve
disagree about what happened.

**Total return, dividends added at face value; sessions counted on the run's
own decision grid.** Chosen.

## Decision

### The quantity

```
total_return = (split_factor × target_close + dividends) / anchor_close − 1
outcome_up   = total_return > 0
```

Corporate events are collected over the **half-open interval
`(anchor, target]`** — strictly after the anchor, up to and including the
target. A dividend whose ex-date is the anchor session belongs to whoever held
the position before the forecast was made, not to the forecast.

A **flat close is scored as "not up"**. A rise is strictly positive. Rare at
four decimal places, but the convention had to be fixed in advance rather than
settled the first time someone noticed a tie in the data.

### The grid

"In N sessions" means N points on the **run's own decision grid** — the ordered
instants at which the run actually decided, read back from its snapshots.

This grid is identical for every arm, deterministic, reconstructible from
persisted artefacts alone, and immune to a missing bar silently turning a
5-session horizon into a 6-session one. `SessionGrid` is the single type a real
multi-calendar study would change.

This is stated as **this implementation's interpretation of §20, not a
quotation of it.**

### Five statuses, one of which is never written

`RESOLVED`, `DELISTED`, `EXPIRED`, `SUSPENDED`, and `PENDING`. `DELISTED` and
`EXPIRED` are **censoring**: the forecast can never be resolved and the cell is
dropped. `SUSPENDED` deliberately is not censoring — a suspension lifts, and a
forecast over a window containing one still has a target price.

`PENDING` is **computed but never persisted**. Writing it would mean either
updating that row when the horizon elapses — which the append-only triggers
refuse ([ADR-0005](0005-append-only-history-hash-chained-by-sequence.md)) — or
leaving a stale row claiming an already-resolved forecast is still open.
Pending is the *absence* of a verdict, so it is represented by the absence of a
row.

## Consequences

**Corporate actions stop being noise.** A 2-for-1 no longer scores as a 50%
loss for everyone who forecast it.

**The resolution matches the books.** Dividend cash is added at face value,
which is exactly what the ledger does with it. Forecast quality and equity path
agree about what happened to an instrument.

**A run with a heterogeneous universe resolves on one grid, not several.** This
is a genuine approximation. "Five sessions" for a 24/7 instrument means "five of
*this run's* decision points", which is not five of that instrument's own
sessions. For a universe where the calendars diverge sharply — a crypto
instrument against a European equity across a long holiday — the horizons are
not the same length in wall-clock time. The alternative resolves to instants
with no price, which is worse, but this is an approximation and not a
refinement.

**Late forecasts never resolve, and that is correct.** In a 20-session run, a
horizon-20 forecast made at session 1 resolves and one made at session 5 does
not. Two integration tests assert exactly this, having originally asserted the
opposite: the tests were wrong, not the resolver.

**Re-running resolution is safe.** It only ever writes terminal verdicts and is
idempotent. It runs as a pass over a completed run rather than as a step inside
the cycle — correct today because nothing consumes outcomes during a run, and
wrong the moment memory is given outcome feedback
([ADR-0015](0015-closed-form-reflection-without-outcomes.md)).

## What would make us revisit this

- **A universe whose calendars genuinely diverge.** `SessionGrid` becomes
  per-instrument, and the resolver needs a real price for instants no snapshot
  covers — which means a data source outside the frozen snapshot, and therefore
  a new point-in-time argument.
- **Intraday decisions.** The grid is one point per cycle. A study deciding
  more than once a session would need the grid to carry which decision within
  the session it means.
- **A study where reinvestment is the right convention.** If the portfolio ever
  reinvests dividends, the scoring convention must change with it, in the same
  commit.
