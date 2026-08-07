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

Last verified: 535 tests passing, `mypy --strict` clean on 65 source files.

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
| Virtual execution at the next eligible window (§16.2) | `done` | `tests/unit/test_execution_engine.py` |
| Sizing, fees, spread and liquidity caps (§16.3–§16.5) | `done` | `tests/unit/test_execution_policy.py` |
| Double-entry ledger with enforced balance (§17.1, §17.2) | `done` | `tests/unit/test_ledger.py` |
| Append-only position lots and FIFO cost basis (§17.4) | `done` | `tests/unit/test_positions.py` |
| Multi-currency valuation with explicit logged rates (§17.3) | `partial` | `tests/unit/test_execution_engine.py`; no FX *conversion*, see gaps |
| Settlement at T+N on the instrument's own calendar | `done` | `tests/unit/test_execution_engine.py` |
| Corporate actions applied to books and reference data (§17.5) | `done` | `tests/unit/test_corporate_actions.py` |
| Whole cycle wired end to end over the synthetic world | `done` | `tests/integration/test_execution_wiring.py` |
| Persistent episodic memory with a strict point-in-time recall (§18) | `done` | `tests/unit/test_memory_materials.py` |
| Periodic strategic reflection (§19) | `done` | `tests/unit/test_memory_materials.py` |
| Matched placebo memory and reflection for B′ / C′ (§13) | `done` | `tests/unit/test_memory_materials.py` |
| Imposed, isolated forecast panel (§15) | `done` | `tests/unit/test_panel.py` |
| Six conditions wired end to end and genuinely distinguishable | `done` | `tests/integration/test_materials_wiring.py` |
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
- **A portfolio must be funded in each currency it trades; there is no FX
  conversion.** Buying the EUR-quoted instrument requires EUR cash, and a
  short EUR balance is an ordinary rejection rather than an automatic
  conversion from USD. Valuation *does* cross currencies, using the rate
  carried in the frozen snapshot, so equity is one number. Auto-conversion
  would need an FX translation account and an FX gain/loss decomposition;
  building it before anything needs it would be an unused abstraction, and
  funding per currency is honest in the meantime.
- **Short selling is not modelled.** A SELL with nothing held is rejected as
  `NOTHING_TO_SELL`, and `PositionBook.close_quantity` refuses an over-close
  rather than opening a negative position. This bounds what the study can
  observe: an arm that is confidently bearish on something it does not hold
  can express that only by not buying.
- **No leverage, no margin, no interest.** `target_weight` is capped at 1 and
  cash never goes negative. Uninvested cash earns nothing, which understates
  every arm's return equally.
- **A dividend's cash arrives on the ex-date, not the pay date.** Entitlement
  — who holds the position, and how much they are owed — is exact. The few
  sessions of float between ex and pay are collapsed, which matters only for
  interest this study does not model.
- **Fills use the execution session's bar, because the synthetic world prints
  one bar per session.** `execute_after` therefore selects *which* session
  fills an order, not which intraday price. A real Phase 3 feed with opening
  prints would make this exact without changing the interface.
- **Fee magnitudes are placeholders.** 5bp/1.00 minimum for equities and so
  on are in the spirit of retail brokerage, not a calibrated cost model.
  They are pre-registered in the sense that every arm pays the same, which is
  what the comparison needs; they are not claimed to be realistic.
- **Sizing is a fixed fraction of equity, identical for every arm.** This is
  deliberate (see `marketlab.execution.policy`) but it bounds the claim: the
  study measures direction and timing quality, **not portfolio construction**.
  An arm cannot win by sizing, and cannot demonstrate skill at sizing either.
- **The cycle's step order is the caller's responsibility, not the
  platform's.** `tests/integration/test_execution_wiring.py` drives it —
  reference data, then settlement, corporate actions, fills, decisions — and
  `marketlab.accounting.positions._SEQUENCE_RANK` records the same order for
  the fold. Nothing yet *enforces* that a driver calls them in that order; the
  CLI (task 13) is where that sequence should become a single supported entry
  point rather than a convention two places agree on.

- **Granted material reaches the model, but the shipped fake ignores it.**
  `DeterministicPolicyModel` is a closed-form function of the closing price
  and does not read `injected_context`, so a run today shows the arms
  different things and gets identical decisions from all six. This is
  deliberate and pinned by a test: a fake that branched on its injected
  context would manufacture a memory effect out of nothing — the exact defect
  the audit found in this project's predecessor. **The consequence for the
  study is blunt: no arm comparison run against the fake means anything about
  memory or reflection.** What is established is that the channel is live —
  `tests/integration/test_materials_wiring.py` drives a test double that does
  read its context and gets different decisions per arm through the same
  pipeline. Demonstrating a real effect needs a real provider (Phase 3).
- **Reflection says nothing about whether forecasts came true.** The rules are
  about the condition's own behaviour — persistence on one side, churn,
  probabilities that barely move, recurring malformed output. Hit rates and
  calibration need forecast resolution, which is task 12. A rule claiming
  accuracy today would be fabricated, and a test asserts none of them does.
- **Reflection is closed-form, not model-authored.** A real deployment would
  ask a model to reflect. The deterministic version exists so a reflection can
  be replayed and reasoned about; swapping in a model-authored one changes no
  caller, but it is a different treatment and would need re-piloting.
- **A placebo matches shape, not information content.** It is matched line for
  line and to within 2% on length, and contains no instrument, probability or
  failure kind from the genuine record. It is *not* matched on how plausible
  or engaging the text is, which is not measurable here — if a model can tell
  the two apart for reasons other than content, B′ stops being a control.
- **Placebo reflection carries a fixed three rules.** Matching the genuine
  rule count would require reading the genuine reflection, which for C′ means
  reading C's. The resulting length difference is bounded and measured, not
  assumed.
- **Recall depth and reflection cadence are placeholders.** Eight episodes and
  every five cycles are round numbers, like the tool budget. Both trade
  context cost against how much history a condition can see, and both depend
  on the still-open API cost model (task #4).
- **The panel is elicited but not yet persisted or scored.** `PanelAgent`
  produces answers and records `MISSING_PANEL_ITEM` for unanswered items, but
  nothing stores a panel bundle, and scoring the answers is task 12. Today the
  panel is exercised in tests rather than run inside `CycleRunner`.
- **Memory records what a condition decided, not what happened to it.**
  There is no outcome feedback in an episode, because resolution does not
  exist yet. That makes the memory treatment weaker than a real one would be,
  and is the single largest thing task 12 will change about it.
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
| §13 | Arms A/B/C/D plus placebos B′/C′ | The four arms encoded as a **crossed 2×2** of `(memory, reflection)` grants — A neither, B memory, C both, D reflection only — with B′/C′ as matched placebos of B and C | The specification names the six conditions; what each one *grants* is this implementation's reading, **decided and closed** rather than left open. A crossed design is the only arrangement of four arms that separates the memory and reflection effects instead of confounding them: without D, a C-versus-A difference cannot be attributed to either factor. D is coherent because the two channels are defined as separable — memory is raw episodic recall, reflection is distilled strategy produced by a process that reads the run's record. Under D the *reflection process* reads history; the agent does not. See `marketlab.experiments.arms.Channel`. |
| §13.4 | Randomised arm order | A deterministic Fisher-Yates over an explicit SHA-256 key stream, not `random.shuffle` | `random.Random` guarantees reproducibility of `random()` for a seed, but `shuffle`/`sample` are implementation details that have changed between CPython versions. A replay may run years after collection, on a different interpreter. See the `marketlab.experiments.ordering` docstring. |
| §17.4 | Lot-level position tracking | Positions stored as immutable open/close **events**, with lots folded on read | A `quantity_remaining` column decremented on every sale is an in-place edit of a past fact, which §P4 forbids and the append-only triggers refuse. Folding also makes "what did the book hold at instant *t*" answerable for every *t*. See `marketlab.accounting.positions`. |
| §17.1 | Debit/credit entries | One **signed** amount per entry, positive debits | A `direction` enum beside an unsigned amount turns the balance check into a conditional sum, which is a place to get the sign wrong. With signed amounts, "this balances" is literally addition. |
| §13 | Placebo material for B′/C′ | The placebo reuses the **genuine renderer** over fabricated episodes, sized from the arm's own `EpisodeShape` | Hand-writing filler and hoping it came out the same length would leave the comparison confounded by whichever was longer, with no way to tell by how much. Shape is read from integer columns only, so a placebo is structurally incapable of carrying genuine content. |
| §19 | Strategic reflection | Deterministic closed-form rules over the condition's own record, not a model-authored reflection | Phase 1 has no real provider, and a reflection produced by an opaque process could not be replayed (§12.5). Same reasoning as the deterministic policy fake. |
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
5. **Starting capital and target weight.** The integration test funds
   1,000,000 USD + 500,000 EUR per condition at a 5% target weight, and the
   unit tests use 100,000 at 10%. Neither is pre-registered. The figures
   interact with the fee minimum (which makes small orders uneconomic) and
   with the participation cap, so they should be fixed once alongside the
   decision cadence (question 2).
6. **Repetitions per arm.** `RunConfig.repetitions` defaults to 1. The right
   value depends on within-condition variance, which nothing has measured yet
   — it is one of the quantities task #4's power simulation exists to
   estimate. Independent repetitions are what separate "this arm decides
   differently" from "this model is nondeterministic", so 1 is a placeholder,
   not a recommendation.
7. **Recall depth and reflection cadence.** Eight episodes recalled, one
   reflection every five cycles. Both are round numbers. Deeper recall and more
   frequent reflection are a *stronger* treatment, so these choices set the
   effect size the study is powered to detect — they belong with the power
   simulation (task #4), not with an implementation default.
8. **Whether the panel should share the decision's tool budget.** The panel
   currently gets its own, so answering it costs a condition nothing it could
   have spent deciding. The alternative — one budget across both — would make
   the panel a real opportunity cost and might itself differ between arms.
9. **`SnapshotStatus` completeness criteria (§23.2).** This implementation
   treats a snapshot as `DEGRADED`/`INVALID` based on missing *price* data for
   actively-tradable instruments only, and treats missing news/macro/FX as
   normal rather than degrading. If the specification intends a stricter or
   different completeness rule, `marketlab.snapshots.builder._compute_status`
   is the single function to change; nothing downstream assumes more than the
   three-value `SnapshotStatus` enum already exposes.
