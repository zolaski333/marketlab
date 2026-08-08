# Architecture

How the code is arranged, and which arrangements are load-bearing.

Most of this file describes structure you could read off the source. Two parts
do not: **the dependency rule** (§2), because it is enforced by a test rather
than visible in any one file, and **the cycle's step order** (§4), because it
is one sequence that three different callers must agree on.

For *why* a given structure was chosen, see [adr/](adr/). For what is and is
not finished, see [ROADMAP.md](ROADMAP.md) — the only place completeness is
claimed.

## 1. The shape of the thing

MarketLab runs a **cycle** once per simulated session. One cycle:

1. freezes a point-in-time snapshot of the world;
2. asks six experimental conditions to decide, from that same frozen snapshot;
3. asks each of them the same imposed panel of probability questions;
4. sizes, places, executes, settles and books the resulting trades in six
   separate virtual portfolios.

Afterwards, a **resolution** pass scores the forecasts against what happened,
an **analysis** pass compares the arms, and a **replay** pass recomputes
everything downstream of the model into a separate database and checks it
matches.

Nothing here touches a real market. There is no adapter that could.

## 2. Packages, and the rule that constrains them

### The layers

| Layer | Packages | May hold a database session |
|---|---|---|
| **Foundations** | `core`, `models` | no — they import nothing internal at all |
| **Decision path** | `agents`, `retrieval`, `forecasting` | **forbidden**, structurally |
| **Domain** | `instruments`, `ingestion`, `snapshots`, `memory`, `reflection`, `accounting`, `execution` | yes |
| **Science** | `evaluation`, `analysis`, `power` | `evaluation` yes; `analysis` and `power` no |
| **Assembly** | `experiments`, `study`, `replay`, `cli`, `storage`, `audit` | yes |

### The rule

**`agents/`, `retrieval/` and `forecasting/` may not import SQLAlchemy — at
all.** `tests/security/test_decision_path_isolation.py` walks their ASTs and
fails on any import of it, direct or aliased.

A package that cannot hold a session cannot write a query that forgets its
point-in-time filter. This is the whole of the lookahead defence, and it is a
structural fact rather than a convention — see
[ADR-0004](adr/0004-point-in-time-by-package-boundary.md).

The guard shaped the code rather than being retrofitted to it. When the imposed
panel was built, it could not persist itself, so `PanelStore` lives in
`evaluation/panels.py` while `PanelItem` lives in `forecasting/panel.py`. That
split looks arbitrary until you know the rule.

### Where the model may look

`models/` imports nothing internal and defines the provider-independent
interface. Nothing in it carries an arm, condition or repetition — checked by
field-shape scan *and* by scanning the content of real requests from a real
cycle ([ADR-0003](adr/0003-masking-is-partial.md)).

`experiments/context.py` is the **single** place that both knows which arm is
running and decides what it receives. Concentrating that knowledge is what
makes condition-blindness auditable: there is exactly one function to read.

### Two honest irregularities

**`storage/schema.py` imports every package that owns a table.** That inverts
the layering on purpose. SQLAlchemy's declarative base only knows a table once
the module defining it has been imported; a table in a module nobody imported
is absent from the metadata, so `create_all` skips it and any protection keyed
to its name silently does nothing. One module importing all of them makes the
metadata complete by construction.

**`memory` and `reflection` import each other at package level.**
`memory/rendering.py` imports `reflection.engine` (to render a reflection);
`reflection/engine.py` imports `memory.store` (to read episodes). At *module*
granularity there is no cycle — `memory/store.py` imports nothing from
`reflection` — so Python is happy, but the package graph has a loop and a
reader should know it is there rather than discover it.

**`transformations/` is an empty package.** A declared namespace with no
content and no importer. It is scaffolding for derived-indicator work that does
not exist; per the roadmap's own rule, an empty package directory counts for
nothing.

## 3. Data flow through one cycle

```
ingestion ──► blobs + provenance ──► append-only event
                       │
                       ▼
              snapshots.SnapshotBuilder          (holds a session; applies the cutoff)
                       │
                       │  ONE frozen snapshot object, shared by identity
                       ▼
        ┌──────────────┴───────────────┐
        │        per arm (×6)          │
        │                              │
   retrieval.RetrievalToolkit     experiments.context     ← the only arm-aware step
   (frozen index, budgeted)       (memory / reflection / placebo text)
        │                              │
        └──────────────┬───────────────┘
                       ▼
              agents.DecisionAgent  ──►  models.LanguageModel
                       │                 (ModelRequest carries text, never a label)
                       ▼
              sealed decision bundle  ──►  agents.PanelAgent (fresh model, fresh budget)
                       │                              │
                       ▼                              ▼
              execution.ExecutionEngine        evaluation.PanelStore
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
  accounting.Ledger  positions   settlements
```

Two properties of that diagram matter more than the boxes:

- **The snapshot is shared by object identity**, not rebuilt identically per
  arm. A difference between two arms' decisions therefore cannot be a
  difference in what they were shown.
- **The only arm-aware step is `experiments.context`**, and it returns *text or
  nothing* — never a context object. If it returned the whole object it could
  also vary the turn budget between arms, confounding the comparison with an
  allowance difference that has nothing to do with memory.

## 4. One cycle order, one assembly

Two things in this system are sequences that several callers must agree on, and
both are held in exactly one place.

**`experiments.driver.CycleDriver.run` is the only supported step order**:
reference data, settlement, corporate actions, fills, decisions, placement.
`tests/unit/test_cycle_driver.py` asserts the sequence directly rather than
inferring it from a downstream number. The integration tests and the replay
both go through it, so the order is not a convention two places happen to
share.

**`study.pipeline.open_study` is the only assembly.** The CLI, the integration
tests and the replay all build their component graph through it. A command line
that built its own would be the fourth assembly in this repository, and the
fourth is where two of them start to differ in a way nobody notices.

What is still *not* enforced: nothing stops new code from wiring the components
up by hand. Only review would catch it.

## 5. Storage

SQLite, WAL mode, deferred transactions. 21 tables, **all** of them append-only
— see [DATA_DICTIONARY.md](DATA_DICTIONARY.md) for every column and
[ADR-0005](adr/0005-append-only-history-hash-chained-by-sequence.md) for why.

Three conventions that a reader will otherwise misread:

- **Money is stored as an exact decimal string**, never a float. `VARCHAR(48)`
  columns hold `"1234.56"`. Binary floating point cannot represent 0.1, and a
  ledger that does not balance to the cent is not a ledger.
- **Instants are stored as canonical UTC strings** of fixed width, so
  lexicographic order is chronological order and a `WHERE as_of < ?` is
  correct without parsing.
- **Large payloads live in the content-addressed blob store**, and tables carry
  the SHA-256 digest. A decision bundle row is metadata plus two digests; the
  decision itself is a blob.

The event log is hash-chained over a monotonic `seq`, **not** over a timestamp:
the six arms of one cycle share a decision instant, so timestamp order among
them is undefined and a chain built on it would have no single valid
linearisation.

## 6. Determinism

Everything that produces a published number is reproducible from a seed:

- **Arm execution order** — a Latin square by default, or a deterministic
  Fisher-Yates shuffle, over an explicit SHA-256 key stream.
- **Bootstrap resampling** — moving blocks, same key stream.
- **The synthetic world** — prices, news, corporate actions, all closed-form.
- **The shipped model** — a closed-form function of the closing price, which
  deliberately ignores the material each arm is granted
  ([ADR-0017](adr/0017-a-fake-that-ignores-its-context.md)).

`import random` appears on no path that produces a published number, because
CPython guarantees the reproducibility of `random()` for a seed but *not* of
`shuffle` or `sample` — see
[ADR-0013](adr/0013-determinism-from-an-explicit-key-stream.md).

## 7. The command line

Eight commands. The six that touch a study all go through `open_study`, and
every one supports `--json` for one canonical JSON object per result on stdout,
with progress kept on stderr.

| Command | Does |
|---|---|
| `run` | Declares the configuration, then runs cycles. Idempotent: resumes rather than redoing. `--dry-run` validates and writes nothing at all. |
| `resolve` | Scores elapsed forecasts. Idempotent; only ever writes terminal verdicts. |
| `analyse` | Runs the pre-registered plan. Requires an explicit ROPE. |
| `replay` | Recomputes into a separate database. Non-zero on any divergence. |
| `verify` | Re-derives every hash in the event chain. Takes no `--run-id`: the chain is one sequence across the database. |
| `status` | Reports what a study contains, and its configuration fingerprint. |
| `power` | Runs the power simulation through the real analysis plan. Needs no database. |
| `cost` | Projects API spend for a configuration at stated prices. Needs no database. |

Exit codes are semantic, not just zero/non-zero — see
[FAILURE_POLICY.md](FAILURE_POLICY.md) §4.

## 8. What is not here

- **No real provider adapter.** `models.LanguageModel` is the seam; Phase 3
  implements it. See [PROVIDER_POLICY.md](PROVIDER_POLICY.md) for the contract
  such an adapter must meet.
- **No real market data adapter.** `ingestion.synthetic` implements all five
  provider protocols, which is honest for a fabricated world and will not be
  the shape of a real one.
- **No migrations.** `create_schema()` builds the schema directly.
  `Database.migration_mode()` is the audited window migrations will run in when
  a live study makes them necessary.
- **No FX conversion, no short selling, no leverage, no margin, no interest.**
  Each bounds what the study can observe; see [LIMITATIONS.md](LIMITATIONS.md).
