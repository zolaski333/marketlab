# Architecture decision records

**This directory is empty on purpose, and that is a gap rather than a
decision.** It is task #5 in [../ROADMAP.md](../ROADMAP.md).

## Where the reasoning currently lives

Not nowhere — but not here either, which is the problem. Today an architectural
decision is recorded in two places:

1. **The module docstring of whatever implements it.** These are long and they
   argue. `marketlab/storage/events.py` explains why the hash chain is ordered
   by a monotonic sequence rather than a timestamp;
   `marketlab/accounting/positions.py` explains why lots are folded from
   immutable events instead of decremented in place; `marketlab/replay/verifier.py`
   explains what a replay can and cannot reproduce.

2. **The "Decisions taken that depart from the specification" table** in the
   roadmap, which lists every place this implementation does something other
   than what the specification says, and why.

## Why that is not sufficient

A docstring records the decision that was *taken*. It does not record the
options that were *rejected*, what they would have cost, or what would have to
change for the decision to be revisited. Someone arriving in a year cannot tell
whether a choice was considered and rejected or simply never considered.

The roadmap table is closer, but it is one line per decision and it only covers
departures from the specification — not the many choices the specification left
open.

## What should go here

One file per decision, numbered, in the usual form: context, options
considered, decision, consequences, and what would make us revisit it. The
candidates are already identifiable from the two sources above; the work is
writing them properly, not discovering them.

Until that is done, this directory exists so that its emptiness is visible to
anyone who clones the repository, rather than being an absence git would not
have shown at all.
