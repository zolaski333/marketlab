# ADR-0012 — Replay recomputes everything downstream of the model, and only that

- **Status:** accepted
- **Implemented by:** `marketlab.replay.verifier`
- **Checked by:** `tests/replay/test_replay_verifier.py`

## Context

§12.5 asks for exact replay. The predecessor of this project shipped one: it
re-serialised a string, checked it was non-empty, and returned
`EXACT_REPLAY_SUCCESS` **unconditionally**. It could not fail, so it verified
nothing, and nothing in that repository could contradict its report.

Building a replay that can fail requires deciding what "the same" means, and
that decision is not obvious, because one component of the pipeline is not a
function.

**A real language model is not deterministic.** Even at temperature 0,
providers do not guarantee bit-identical outputs across time, hardware or
silent model updates. A replay that re-elicited the model would report a
divergence on every run — a divergence that is not a defect. After the third
such report nobody would read the fourth, and the verifier would be worse than
useless: it would be noise that trains its reader to ignore it.

## Options considered

**Re-run everything including the model, and compare.** Rejected for the reason
above. It is the only thing that would deserve the phrase "exact replay", and
it is unavailable against any real provider.

**Re-run everything including the model, and compare with a tolerance.**
Rejected: "the decisions are similar enough" has no threshold anyone can
defend, and a tolerance wide enough to absorb provider drift is wide enough to
absorb a bug.

**Record every provider response and stub the model with a recorded-response
player.** This is VCR-style replay. It verifies the *orchestration* — that the
same responses produce the same downstream state — which is genuinely useful.
Rejected as the primary mechanism because it does not verify the thing that
matters: it re-plays the recorded bytes rather than re-deriving anything from
them, so a sealed decision whose stored payload disagrees with its stored hash
would replay perfectly.

**Treat sealed decisions as inputs, re-derive their fingerprints from the
payloads behind them, and recompute everything downstream from nothing.**
Chosen.

## Decision

A replay reads the study database, writes into a **separate** database, and
compares field by field.

**Sealed decisions and panels are inputs**, exactly as the raw market data is —
but their content hashes are **re-derived from the payload blobs behind them**,
so a payload that has been altered to disagree with its recorded fingerprint is
caught. What is trusted is that the model said what was recorded; what is
verified is everything that follows from it.

Everything downstream is recomputed from nothing: sizing, order placement,
execution eligibility, fills, fees, slippage, settlement, ledger postings,
position lots and cost basis, corporate action application, valuation, and
forecast resolution. Each is compared to what the original run produced.

**The model factory a replay hands every runner raises if it is ever
constructed.** A replay cannot re-elicit a model — not by convention, but
because there is nothing to elicit with. That is what makes "sealed decisions
are inputs" a structural fact rather than a promise.

`marketlab replay --run-id X` exits non-zero on any divergence.

## Consequences

**Every mistake the platform itself can make lies downstream of the model, and
all of it is recomputed.** A fee formula that changed, an off-by-one in the
settlement calendar, a corporate action applied in the wrong order, a cost
basis that drifted — all are caught.

**The model's own contribution is not verified, and cannot be.** If a real
provider returned different text on a different day, this replay would not know
and is not designed to. The claim it supports is precisely: *given what the
model said, everything else is reproducible.* That is the claim this document
makes and no larger one.

**Divergence reports are readable.** The verifier found a real defect during
development: 56 resolutions reported as "present/absent" because the replay
database had no decision bundles to collect forecasts from. The fix was to
carry sealed decisions and panels across as inputs — a bug in the replay, found
by the replay being able to fail.

**Ledger and position comparison is on balances, not on individual rows.** An
entry id embeds its transaction id, which legitimately differs when nothing
else does. What must match to the cent is what the books *say*. A replay that
produced the same balances by a different sequence of postings would not be
flagged, and that is a deliberate weakening.

**The command is self-contained; the library call is not.**
`marketlab replay --run-id X` reads the persisted configuration and rebuilds the
world's calendars, so a replay needs nothing but the database. `ReplayVerifier`
itself still takes a `ReplayConfig`, and one constructed by hand with a
different policy will report divergences — correct, since that would genuinely
be a different study, but it is a foot-gun the CLI avoids and a direct caller
does not.

## What would make us revisit this

- **A provider offering genuinely reproducible sampling** — a seed that is
  contractually stable across model versions. Then full replay including the
  model becomes possible and this record should be superseded. No provider
  offers this today.
- **Recorded-response replay as a *second* mode.** It would add orchestration
  coverage — did the agent make the same tool calls in the same order — which
  the current design does not check. Worth adding alongside, not instead.
- **Row-level ledger comparison**, if identifier derivation is ever changed so
  that entry ids are stable across replays. That would strengthen the check for
  free.
