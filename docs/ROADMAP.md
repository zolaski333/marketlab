# Roadmap and honest status

This file is the **only** place where completeness is claimed. If a capability
is not marked done here, it is not done — regardless of what a module docstring,
a class name, or a passing test suite might suggest.

## Why this file exists

The previous implementation of this project shipped a `PHASE_1_VALIDATION_REPORT.md`
asserting "All 24 acceptance criteria PASS" and a requirements traceability
matrix citing 14 test modules. **None of those 14 test files existed.** The
`ruff` and `mypy` gates it claimed to satisfy were never installed. Its "exact
replay" verifier re-serialised a string, checked it was non-empty, and returned
`EXACT_REPLAY_SUCCESS` unconditionally.

The lesson is not "write better reports". It is that a status claim is worthless
unless it is cheap to falsify. So:

- Every row below names the **command** that substantiates it.
- A capability is `done` only when its tests exist, run, and pass in CI.
- `partial` is used freely. `done` is not.

## Status legend

| Mark | Meaning |
|---|---|
| `done` | Implemented, tested, all quality gates pass. |
| `partial` | Implemented in part; the gap is stated explicitly. |
| `not started` | No implementation exists. Empty package directories do not count. |

## Quality gates

All five must pass before anything is marked `done`:

```bash
uv sync --all-extras && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
```

Last verified: 359 tests passing, `mypy --strict` clean on 51 source files.

---

## Phase 0 — Foundations

| Component | Status | Substantiated by |
|---|---|---|
| Repository scaffolding, Python 3.12 + uv, locked deps | `done` | `uv sync --all-extras` |
| Canonical UTC instants (§6.1) | `done` | `tests/property/test_instants.py` |
| Exact monetary arithmetic (§17.2, §34.10) | `done` | `tests/property/test_money.py` |
| Canonical JSON (§9.4, §24.1) | `done` | `tests/property/test_canonical.py` |
| Injectable clocks — System/Frozen/Replay (§6.1) | `done` | used throughout; `test_event_chain.py` |
| Derived identifiers (§16.7) | `done` | `tests/unit/test_ids.py` |
| Failure taxonomy (§3, §23) | `done` | types only; exercised as subsystems land |
| Content-addressed blob store (§8.2) | `done` | `tests/unit/test_blobs.py` |
| Append-only enforcement via DB triggers (§P4) | `done` | `tests/unit/test_event_chain.py` |
| Event log + hash chain, monotonic sequence (§24.1) | `done` | `tests/unit/test_event_chain.py` |
| Daily root hashes (§24.2) | `done` | `tests/unit/test_daily_roots.py` |
| Manual intervention audit (§P6) | `done` | `tests/unit/test_interventions.py` |
| Point-in-time cutoff, structurally required (§6.3) | `done` | `tests/unit/test_cutoff.py` |
| Import lint forbidding raw session access in agent paths | `done` | `tests/security/test_decision_path_isolation.py` |
| **Power simulation and API cost model** | `not started` | — |
| Architecture docs and ADRs | `partial` | this file; module docstrings carry the reasoning |

### Known gaps in what is marked `done`

- **`FailureScope` / `ObservedAgentFailure` are types without call sites.** They
  are the contract the execution and model layers will be held to; nothing
  enforces them yet.
- **No Alembic migration exists.** `create_schema()` builds the schema directly.
  Migrations become necessary only once a study is live and the schema changes;
  `Database.migration_mode()` is the audited window they will run in.
- **The decision-path isolation lint protects `agents/`, `retrieval/`,
  `forecasting/`.** As of task 8, `retrieval/` and `agents/` are both
  populated and the guard does real work for them
  (`tests/security/test_decision_path_isolation.py` scans real files, not
  empty directories). `forecasting/` is still empty, so the test passes
  vacuously for it until task 12 puts code there — that is the point of
  writing the guard before the code it guards exists, not after.

---

## Phase 1 — Synthetic vertical slice

| Component | Status | Substantiated by |
|---|---|---|
| Instrument reference repository — admission, strict resolution, versioning (§7.1–§7.3) | `done` | `tests/unit/test_instruments.py` |
| Daily tradability computation (§7.5) | `done` | `tests/unit/test_instruments.py` |
| Trading calendars — weekday sessions with real DST via `zoneinfo`, 24/7 (§6.4, §16.2) | `done` | `tests/unit/test_calendars.py` |
| Deterministic synthetic market data generator (§31 Phase 1) | `done` | `tests/unit/test_synthetic_provider.py` |
| Ingestion pipeline — blobs + provenance + append-only event (§8.2, §8.5) | `done` | `tests/unit/test_ingestion_pipeline.py` |
| Instruments + calendars + provider + pipeline wired end to end | `done` | `tests/integration/test_synthetic_universe_wiring.py` |
| Frozen exogenous snapshot, point-in-time universe + evidence (§9.1, §23.2) | `done` | `tests/unit/test_snapshot_builder.py` |
| Frozen retrieval index and search (§10.2) | `done` | `tests/unit/test_retrieval_types.py` |
| Typed agent tools with budget enforcement (§10.3, §10.6) | `done` | `tests/unit/test_retrieval_tools.py`, `tests/unit/test_retrieval_budget.py` |
| Snapshot + index + tools wired end to end over the full synthetic run | `done` | `tests/integration/test_snapshot_and_retrieval_wiring.py` |
| Provider-independent model interface (§12.1) | `done` | `tests/unit/test_deterministic_policy.py` |
| Deterministic policy fake with **no access to `condition_id`** | `done` | `tests/unit/test_deterministic_policy.py`, structural guard in `tests/security/test_condition_isolation.py` |
| Decision orchestration loop — tool-calling, citation validation, failure taxonomy (§10, §14.5) | `done` | `tests/unit/test_decision_agent.py` |
| Prompt-injection containment (§11.2) | `done` | `tests/security/test_prompt_injection_containment.py` |
| Arms A / B / C / D **and placebos B′ / C′** — declared as crossed grants, matching checked structurally | `done` | `tests/unit/test_arms.py` |
| Randomised / counterbalanced execution order (§13.4, §30.3) | `done` | `tests/unit/test_ordering.py` |
| Multi-arm cycle runner — shared frozen snapshot, isolated repetitions, sealed decision bundles | `done` | `tests/unit/test_cycle_runner.py`, `tests/integration/test_multi_arm_wiring.py` |
| Condition isolation verified on **content**, not only on field names | `done` | `tests/security/test_condition_isolation.py` |
| Virtual execution at the next eligible window (§16.2) | `not started` | — |
| Double-entry ledger, settlement, corporate actions (§17) | `not started` | — |
| Memory, reflection, imposed panel (§18, §19, §15) | `not started` | — |
| Forecast resolution and the statistical plan (§20, §21) | `not started` | — |
| Real replay that recomputes and compares (§12.5, §30.4) | `not started` | — |
| CLI (§29) | `not started` | — |

### Known gaps in what is marked `done`

- **`blob_metadata` is now written** by `IngestionPipeline`, closing the Phase 0
  gap noted above — but only for the synthetic source. A real provider adapter
  (Phase 3) still needs to go through the same pipeline to inherit this.
- **`FundamentalProvider` (§8.1) does not exist.** Nothing in Phase 1 consumes
  fundamentals data; adding the protocol now would be an unused abstraction.
  Adding it later is a pure addition — every other provider protocol here
  follows the same one-method shape — not a redesign.
- **The admission policy (§7.3) is structural, not a configurable rules
  engine.** `InstrumentRepository.admit()` requires every field §7.3 lists
  (currency, calendar, settlement, execution model) but does not yet
  re-evaluate an *open, changing* universe automatically. That becomes
  necessary once a real provider can propose new instruments (Phase 3).
- **Corporate actions are emitted as raw facts, not yet automatically applied
  to a ledger, a position, or the instrument repository.** The snapshot
  builder correctly *reflects* whatever the instrument repository's state is
  at build time — `tests/integration/test_snapshot_and_retrieval_wiring.py`
  proves a ticker change applied to the repository is picked up by the next
  snapshot and withheld from earlier ones — but nothing yet drives that
  application automatically from an ingested `CORPORATE_ACTION` record.
  Dividends and splits reaching the ledger and position lots is task 10's
  job. Task 9's cycle runner does **not** apply corporate actions either —
  it consumes an already-frozen snapshot and never writes to the instrument
  repository — so the whole of that step now belongs to task 10.
- **`SnapshotStatus` completeness (§23.2) is this implementation's
  interpretation, not a quotation of the specification.** `COMPLETE` requires
  every actively-tradable instrument to have a fresh price bar this session;
  a partial gap is `DEGRADED`; no active instrument priced at all is
  `INVALID`. News/macro/FX absence never affects status, since real markets
  have news-free sessions and the synthetic world scripts one deliberately
  (session 5). See `marketlab.snapshots.builder._compute_status` and the open
  question below.
- **Tool budget magnitudes (`DEFAULT_MAX_TOOL_CALLS`,
  `DEFAULT_MAX_EVIDENCE_CHARS` in `marketlab.retrieval.budget`) are
  placeholders**, not a pre-registered decision — see the open question
  below.
- **Evidence character cost is a plain character count, not a token
  count.** A real per-model tokenizer would make the budget provider-specific,
  which §12.1 forbids at this layer; character count is a deliberately crude,
  provider-independent stand-in until task #4's cost model exists.
- **No real `LanguageModel` provider adapter exists.** Task 8 only had to
  build the interface and a deterministic fake; a real Phase 3 adapter
  (OpenAI, Anthropic, ...) implements the same `LanguageModel` Protocol
  without touching any of its callers.
- **Every arm currently receives nothing.** This is the largest gap in what
  task 9 marks `done`, and it is a gap in *content*, not in machinery. The
  conditions are declared, ordered, isolated, identified and sealed, but the
  only shipped `ConditionMaterialsProvider` is `NullMaterialsProvider`, which
  grants no material to any arm — so a run today produces six conditions that
  are genuinely indistinguishable. The real memory, reflection and matched
  placebo generators are task 11. Until they land, **no comparison between
  arms means anything**; what the passing tests establish is that the
  plumbing carrying that comparison is sound, which is a different and much
  weaker claim.
- **`DecisionOutcome` still carries no `bundle_id`, by design.**
  `marketlab.agents.decision` structurally cannot know the run id, arm id, or
  repetition number — knowing them would itself be the condition leak that
  layer exists to prevent. `marketlab.experiments.runner.CycleRunner`, which
  holds those routing keys, now derives `IdKind.DECISION_BUNDLE` from them
  and persists the result, so the deferral recorded here for task 8 is
  closed.
- **The Latin square balances position, not carryover.** Cyclic rotation
  gives every arm every position exactly once per rotation, but does not
  balance *which arm ran immediately before which*. A Williams design would;
  it matters only if an arm's execution measurably affects the next one's,
  which under the current isolation (fresh model instance, fresh budget, no
  shared memory) there is no mechanism for. Revisit if task 11's memory
  subsystem ever introduces cross-arm state.
- **`CONDITION_MISSING` is recorded but not yet acted on.** A provider outage
  produces a `MissingCondition`, an event, and no bundle — correct as far as
  it goes. §23.4's paired policy (what the analysis does with an incomplete
  cycle: drop the pair, drop the cycle, impute) is a statistical decision
  belonging to task 12, and nothing currently enforces one.
- **A run's configuration is not persisted.** `RunConfig` carries the
  pre-registered parameters (arms, repetitions, seed, order policy, budgets)
  and every cycle event references `run_id`, but the config itself lives only
  in the caller. Reconstructing what a historical run was configured to do
  currently means reading the code that launched it. A `runs` table belongs
  with the CLI (task 13), which is what will actually construct these.
- **`decision_content_hash` deliberately excludes failures and process
  metrics.** Two identical decisions reached in a different number of turns
  hash the same. That is the right default for "did these two conditions
  decide the same thing", but it means an arm comparison keyed on
  `content_hash` alone would not notice that one arm got there while emitting
  three malformed outputs. Those counts are on `decision_bundles` for the
  analyses that care.
- **`RawDecision` covers forecasts and trade intents only.** `IdKind.CLAIM`
  and `IdKind.CONSIDERATION` remain types without call sites (like
  `FailureScope`/`ObservedAgentFailure` were in Phase 0) — a fuller
  claim/reasoning taxonomy is deferred until a task actually needs one
  rather than guessed at now.
- **`TradeIntent` carries no size or weight.** Sizing against real capital
  and risk limits is the execution engine's contract (task 10); inventing a
  numeric shape for it now would likely need reworking once that contract is
  actually designed.
- **`IdKind.SENTINEL_RESULT` remains a type without a call site.**
  Prompt-injection containment (§11.2) is verified by a direct test
  (`tests/security/test_prompt_injection_containment.py`) rather than a
  persisted sentinel-run mechanism. Revisit if a later task wants injection
  results recorded as part of the scientific record, not just tested.
- **Mid-turn budget exhaustion stops the whole decision, not just that
  turn.** `DecisionAgent` does not let the model finalise with whatever
  partial evidence it gathered before hitting the cap — simpler, but worth
  revisiting once real provider cost/latency tradeoffs are known (task #4).

---

## Decisions taken that depart from the specification

Each is deliberate; none is silent.

| § | Specification says | What was built | Why |
|---|---|---|---|
| §26.1 | Use `orjson` | stdlib `json` | Canonical hashing needs byte-level control over the output more than it needs throughput. This path is low-volume and correctness-critical. |
| §26.1 | `exchange_calendars` | Deferred to an optional extra | Phase 1 is synthetic; §34.3 prefers a tested abstraction over an early fragile integration. The adapter seam exists. |
| §24.1 | Hash chain over scientific events | Chain ordered by a monotonic `seq`, not a timestamp | Arms of one cycle share a timestamp, leaving timestamp order undefined. See the `marketlab.storage.events` docstring. |
| §26.1 | SQLite in WAL mode | WAL, with **deferred** transactions | The primary key on `seq` already makes a forked chain impossible, so reads need not take the write lock. See the `marketlab.storage.database` docstring. |
| §8.1 | One adapter per provider protocol | One `SyntheticMarketDataProvider` implementing all five protocols | Honest for a *synthetic* world: a fabricated script can coherently derive news, prices and corporate actions from one source of truth. Real Phase 3 adapters will not share this shape — each gets its own adapter for its own source. See the `marketlab.ingestion.synthetic` docstring. |
| §7.1 | `InstrumentVersion` carries an explicit validity period | No stored `effective_to` column | Storing one would mean mutating the previous version's row the moment a new one is written — exactly the in-place edit append-only storage forbids. "Current as of a cutoff" is instead computed as the latest version whose `effective_from` does not exceed it. See the `marketlab.instruments.repository` docstring. |
| §8.1 | Five raw record kinds, each with its own shape | One uniform `Evidence` type (`kind` + `subject_ids` + `fields`) in the retrieval layer | The raw ingestion types (`RawPriceBar`, `RawNewsItem`, ...) stay precisely typed; only the frozen, agent-facing view collapses them into one searchable, citable shape, the same way a real search index does not keep five parallel collections. See the `marketlab.retrieval.types` docstring. |
| §13 | Arms A/B/C/D plus placebos B′/C′ | The four arms encoded as a **crossed 2×2** of `(memory, reflection)` grants — A neither, B memory, C both, D reflection only — with B′/C′ as matched placebos of B and C | The specification names the six conditions; what each one *grants* is this implementation's reading of them. A crossed design is the only arrangement of four arms that separates the memory and reflection effects instead of confounding them, and it makes exactly two placebos the right number. Recorded as an open question below; `marketlab.experiments.arms.ARMS` is the single table to change if the study owner intends a different mapping. |
| §13.4 | Randomised arm order | A deterministic Fisher-Yates over an explicit SHA-256 key stream, not `random.shuffle` | `random.Random` guarantees reproducibility of `random()` for a seed, but `shuffle`/`sample` are implementation details that have changed between CPython versions. A replay may run years after collection, on a different interpreter. See the `marketlab.experiments.ordering` docstring. |
| §12.1 | A provider's own tool-calling wire format | A custom, provider-independent request/response loop (`ModelRequest`/`ModelResponse`/`ToolCallRequest`/`ToolCallResult`) | Mirroring one real provider's exact shape would make that provider's quirks look like part of the platform's core contract. `marketlab.agents.decision.DecisionAgent` drives this loop generically; a real Phase 3 adapter translates to and from its provider's own format at the edge. See the `marketlab.models.types` docstring. |

## Open questions for the study owner

1. **Primary metric.** Deferred until the power simulation lands. Brier score on
   5-session direction has a dynamic range of roughly 0.005 and a small
   effective sample size once overlapping horizons and cross-sectional
   correlation are accounted for. Candidates to compare on power: Brier at 1 /
   5 / 20 sessions, decision stability under identical bundles, and evidential
   fidelity.
2. **Decision cadence.** Not fixed by the specification. It drives power, cost
   and calendar handling, so it must be pinned before the execution engine is
   finalised. Working assumption: decide at close of session *t*, execute at the
   open of *t+1*.
3. **Equivalence bounds.** §21.7 requires "no practically useful effect" to be a
   reachable conclusion. That requires a pre-registered ROPE; none is defined
   yet.
4. **Tool budget magnitudes.** `ToolBudget`'s defaults (20 calls, 20,000
   evidence characters per decision) are round-number placeholders, not a
   pre-registered decision — they exist so the budget mechanism has something
   to enforce, not because 20 is scientifically motivated. Real values should
   come out of task #4's API cost model together with the decision cadence
   (open question 2), since both drive the same per-cycle cost budget.
5. **What each arm grants (§13).** `marketlab.experiments.arms` encodes A/B/C/D
   as a crossed 2×2 of memory × reflection, with D as reflection-without-memory
   — see the departures table. Confirm this is the intended design, and in
   particular confirm that D is a condition the study wants at all: an agent
   reflecting on strategy with no persistent memory to reflect *over* is a
   coherent cell of the design but an unusual thing to build in practice.
6. **Repetitions per arm.** `RunConfig.repetitions` defaults to 1. The right
   value depends on within-condition variance, which nothing has measured yet
   — it is one of the quantities task #4's power simulation exists to
   estimate. Independent repetitions are what separate "this arm decides
   differently" from "this model is nondeterministic", so 1 is a placeholder,
   not a recommendation.
7. **`SnapshotStatus` completeness criteria (§23.2).** This implementation
   treats a snapshot as `DEGRADED`/`INVALID` based on missing *price* data for
   actively-tradable instruments only, and treats missing news/macro/FX as
   normal rather than degrading. If the specification intends a stricter or
   different completeness rule, `marketlab.snapshots.builder._compute_status`
   is the single function to change; nothing downstream assumes more than the
   three-value `SnapshotStatus` enum already exposes.
