# Reproducibility

What a stranger can reproduce, exactly how, and where the guarantee stops.

The last section is the important one. A reproducibility document that lists
only what works is a marketing document.

## 1. Reproduce the environment

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 — pinned in
`.python-version`, with `requires-python = ">=3.12"` in `pyproject.toml`.

```bash
git clone https://github.com/zolaski333/marketlab && cd marketlab
```

```bash
uv sync --all-extras --frozen
```

`--frozen` installs exactly the committed `uv.lock` and fails rather than
resolving something newer. CI uses it, and the full gate sequence has been run
from a fresh clone on Windows as well as in the working tree — the fresh clone
is the stronger check, because it is what a stranger actually does.

`.gitattributes` forces `* text=auto eol=lf`, because `ruff format --check`
compares bytes and the CI matrix runs on two operating systems. A file stored
with CRLF passes the formatting gate on one and fails on the other for reasons
unrelated to the code.

## 2. Reproduce a run

```bash
uv run marketlab run --config configs/synthetic-pilot.yaml --db data/study.db
```

Safe to repeat: it resumes rather than redoing. A six-arm, twenty-session study
against the deterministic fake takes about eleven seconds and produces 120
decisions, 120 panels and 1 176 resolved forecasts.

```bash
uv run marketlab resolve --run-id SYNTHETIC_PILOT --db data/study.db
```

```bash
uv run marketlab analyse --run-id SYNTHETIC_PILOT --db data/study.db --rope-lower -0.01 --rope-upper 0.01
```

The ROPE has no default, here or in the library
([ADR-0009](adr/0009-no-default-rope.md)). Add `--json` to any command for one
canonical JSON object per line on stdout, with progress kept on stderr.

## 3. What makes it deterministic

| Source of variation | How it is pinned |
|---|---|
| Arm execution order | Latin square, or Fisher-Yates over an explicit SHA-256 key stream |
| Bootstrap resampling | Moving blocks over the same key stream |
| The synthetic world | Closed-form: prices, news, splits, dividends, and the scripted injection |
| The shipped model | A closed-form function of the closing price |
| Identifiers | `derive_id(IdKind, **parts)` — computed from the facts, not allocated |
| Serialisation | Canonical JSON: sorted keys, fixed separators, no whitespace drift |
| Money | Exact `Decimal`, stored as strings |
| Instants | Canonical UTC, fixed width, lexicographically ordered |

**`import random` appears on no path that produces a published number.**
CPython guarantees the reproducibility of `random()` for a given seed, but
`shuffle`, `sample` and `choices` are implementation details that have changed
between versions — and a replay may run years later on a different interpreter.
So Fisher-Yates, rejection sampling and Box-Muller are implemented explicitly
over a SHA-256 key stream
([ADR-0013](adr/0013-determinism-from-an-explicit-key-stream.md)).

The seed is part of `StudyConfig` and therefore part of the run's fingerprint.
A run cannot be re-run with a different seed under the same identifier.

## 4. Reproduce the analysis from someone else's data

```bash
uv run marketlab replay --run-id SYNTHETIC_PILOT --db data/study.db
```

The replay recomputes **everything downstream of the model** into a separate
database and compares field by field: sizing, order placement, execution
eligibility, fills, fees, slippage, settlement, ledger postings, position lots
and cost basis, corporate action application, valuation, and forecast
resolution. It exits non-zero on any divergence.

Sealed decisions and panels are **inputs**, exactly as raw market data is — but
their content hashes are re-derived from the payload blobs behind them, so a
payload altered to disagree with its recorded fingerprint is caught.

**A replay cannot re-elicit a model**: the model factory it hands every runner
raises if constructed. That is structural, not a convention.

```bash
uv run marketlab verify --db data/study.db
```

Re-derives every hash in the event chain. It takes no `--run-id`: the chain is
one sequence across the whole database, and verifying a slice of it would prove
less than it appears to.

```bash
uv run marketlab status --run-id SYNTHETIC_PILOT --db data/study.db
```

Reports the configuration fingerprint, which should be quoted in any write-up.

## 5. Reproduce the power and cost figures

```bash
uv run marketlab power --dates 60 --horizons 5 --skill-gap 0.20 --replications 200
```

```bash
uv run marketlab cost --config configs/synthetic-pilot.yaml --input-price 3 --output-price 15 --cached-input-price 0.30
```

Every row of [POWER.md](POWER.md) is reproducible this way. The simulation
analyses each replication with the **real** `AnalysisPlan` — the same object
that will produce the published result — so whatever the platform's analysis
loses to its own conservatism is already inside those numbers.

## 6. Reproduce the quality gates

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
```

All must pass. CI runs them on Linux and Windows on every push.

`tests/test_quality_gates.py` checks that those tools are declared, are
executable in the running interpreter, and are actually invoked by the workflow
— because the predecessor's validation report claimed gates that were never
installed, and nothing in that repository could contradict it.

It does **not** check that CI is green. A workflow file can exist and every run
of it can fail; the badge for that is GitHub's, not this repository's.

## 7. Where the guarantee stops

### 7.1 The model is not reproducible, and this is the big one

A real provider is not a pure function. Even at temperature 0, outputs are not
guaranteed bit-identical across time, hardware or silent model updates. **No
part of this platform makes a real model's output reproducible, and none can.**

The claim the replay supports is precisely: *given what the model said,
everything else is reproducible.* That is the claim, and no larger one.

### 7.2 A provider may change under a stable identifier

The configuration fingerprint covers every declared parameter. It does not
cover what the provider actually served. If a provider updates a model behind
the same id, the fingerprint is unchanged and the study is not — see
[PROVIDER_POLICY.md](PROVIDER_POLICY.md) §2.5.

### 7.3 Trading calendars are not in the fingerprint

They are objects with behaviour rather than values, named by `world` and
rebuilt by the world builder. A change to calendar **code** would change the
study without changing its fingerprint. Honest for a fixed synthetic script; a
real Phase 3 universe needs a persisted calendar registry.

### 7.4 The ledger comparison is on balances, not on rows

An entry id embeds its transaction id, which legitimately differs when nothing
else does. What must match to the cent is what the books *say*. A replay that
produced the same balances by a different sequence of postings would not be
flagged.

### 7.5 The hash chain proves tampering, not authorship

Nothing is signed. Someone with write access could rewrite history and
recompute every hash, and the result would verify. Publishing the daily root
hash somewhere outside the authors' control is what would close that, and it is
**not done** — see [THREAT_MODEL.md](THREAT_MODEL.md) §2.1.

### 7.6 The library call is less safe than the command

`marketlab replay --run-id X` reads the persisted configuration and rebuilds
the world's calendars, so it needs nothing but the database. `ReplayVerifier`
itself takes a `ReplayConfig`, and one constructed by hand with a different
policy will report divergences — correct, since that would genuinely be a
different study, but a foot-gun the CLI avoids and a direct caller does not.

### 7.7 CI has run on two operating systems, not on every one

The matrix is Linux and Windows. macOS is untested, as is any Python other than
3.12.

## 8. What to quote in a write-up

Enough that a reader can reproduce what you did without asking you:

1. The **`run_id`** and its **configuration fingerprint** (`marketlab status`).
2. The **exact model identity**, including whatever version discriminator the
   provider exposes.
3. The **ROPE** and the **primary metric and horizon**, as pre-registered.
4. The **commit hash** of this repository, and the tag of the pre-registration
   that was in force — which must predate the first confirmatory run.
5. The **drop counts by reason**, which is what makes the complete-case
   analysis checkable rather than asserted
   ([ADR-0010](adr/0010-complete-cases-only.md)).
