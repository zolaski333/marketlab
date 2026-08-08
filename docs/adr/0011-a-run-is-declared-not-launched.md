# ADR-0011 — A run is declared; re-declaring it with changed parameters is refused

- **Status:** accepted
- **Implemented by:** `marketlab.study.config.StudyConfig`, `marketlab.study.config.StudyRegistry`
- **Checked by:** `tests/cli/test_cli.py`, `tests/test_release_readiness.py`

## Context

§29.2 says a run is launched with parameters. That is how every experiment
runner works, and it has a failure mode this project cannot afford.

A study runs for weeks. Halfway through, something looks wrong — the target
weight is too small, the recall depth too shallow, the panel too narrow.
Changing it and continuing is the natural thing to do, and it is fatal: the
resulting data come from two different studies pooled under one identifier, and
nothing in the record says so. Worse, the change is usually made *because of
something visible in the partial results*, which makes the whole run a tuned
one.

Nobody does this dishonestly. They do it because the parameter is in a YAML
file and editing a YAML file feels like configuration, not like a scientific
act.

## Options considered

**Record the configuration alongside the results and trust the reader to
compare.** Rejected: nobody compares, and a configuration recorded per cycle
would make the change visible only to someone who diffed 120 of them.

**Copy the configuration into the run directory on first use and warn on
mismatch.** Rejected: a warning during a long run scrolls past. And "warn"
means the study continues.

**Version the configuration — allow changes, record them, analyse per
version.** Considered seriously. It is the honest version of allowing changes,
and it was rejected because it makes mid-study retuning *supported*. The
statistical cost is not recoverable by bookkeeping: an underpowered study
retuned twice is three underpowered studies, however well documented.

**Fingerprint the configuration and refuse any run that contradicts it.**
Chosen.

## Decision

A run is **declared**, not launched.

`StudyConfig` holds every pre-registered parameter — arms, repetitions,
sessions, seed, order policy, budgets, panel horizons, recall depth, reflection
cadence, target weight, participation cap, minimum notional, starting capital —
with every monetary value as an **exact string**, never a float, so the
fingerprint cannot drift with a binary rounding.

On first use, `StudyRegistry.declare` writes the configuration's SHA-256
fingerprint to the append-only `runs` table under its `run_id`. On every later
use it compares. **A mismatch raises**, and the CLI exits with code 4
(`INTEGRITY`) — the same class as a broken hash chain or a replay divergence,
because it is the same kind of problem.

Changing the design mid-study therefore requires a new `run_id`, which is
visible in the data, in the CLI output, and in any write-up that quotes it.
`marketlab status` reports the fingerprint so it can be quoted.

This is stated as a **departure from the specification**: §29.2 does not say
the configuration is immutable. This implementation makes it so.

## Consequences

**"We did not retune" becomes checkable.** The fingerprint is in the database,
in an append-only table, under a hash chain.

**Resuming is safe and idempotent.** `marketlab run` on an existing run resumes
rather than redoing, because the configuration is known to be the same one.
This is a direct benefit of the mechanism rather than a separate feature.

**A typo costs a new `run_id`.** Fixing a genuinely wrong parameter — a
misspelled instrument, a target weight with a misplaced decimal — means
starting a new run and discarding what was collected. That is a real cost and
it will be paid at least once, irritatingly, on a change that was obviously
innocent.

**The refusal is coarse.** Any field change is a different study, including
ones that could not affect the science. There is no notion of a
non-substantive parameter, deliberately: deciding which parameters are
harmless is itself a judgement, and a list of exemptions is a list of places to
hide a change.

**Not everything is captured.** Trading calendars are objects with behaviour
rather than values; they are named by `world` and rebuilt by the world builder.
Honest for a fixed synthetic script, and a real Phase 3 universe would need a
persisted calendar registry of its own — otherwise a change to the calendar
code would silently change the study without changing the fingerprint. This is
the largest gap in the mechanism and it is not closed.

## What would make us revisit this

- **A Phase 3 universe.** The calendar gap above becomes load-bearing the
  moment calendars are not a hard-coded synthetic script.
- **The model version changing under a stable identifier.** Providers deprecate
  and silently update. The configuration records the model id it was given; if
  a provider serves a different model under the same id, the fingerprint is
  unchanged and the study is not. That is a provider-policy problem
  ([PROVIDER_POLICY.md](../PROVIDER_POLICY.md)), not a configuration one, but
  it defeats this mechanism just as effectively.
- **Genuine operational parameters** — a request timeout, a retry count —
  entering `StudyConfig`. Those legitimately vary without changing the study,
  and their presence would make the coarse refusal actively harmful. They
  belong outside the fingerprinted object.
