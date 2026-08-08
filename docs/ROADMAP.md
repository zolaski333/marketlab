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

Last verified: 844 tests passing, `mypy --strict` clean on 84 source files —
including from a **fresh clone** with `uv sync --all-extras --frozen`, which is
what a stranger actually does.
Run automatically on every push and pull request, on Linux and Windows, by
`.github/workflows/ci.yml` — and `tests/test_quality_gates.py` checks that the
tools are declared, are executable in the running interpreter, and are actually
invoked by that workflow. The predecessor's report claimed gates that were
never installed; that specific claim is now one a machine can contradict.

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
| Architecture docs and ADRs | `partial` | this file; module docstrings carry the reasoning. `docs/adr/` is still empty — task #5 |

### Known gaps in what is marked `done`

- **`FailureScope` / `ObservedAgentFailure` are types without call sites.** They
  are the contract the execution and model layers will be held to; nothing
  enforces them yet.
- **No Alembic migration exists.** `create_schema()` builds the schema directly.
  Migrations become necessary only once a study is live and the schema changes;
  `Database.migration_mode()` is the audited window they will run in.
- **The decision-path isolation lint protects `agents/`, `retrieval/`,
  `forecasting/`.** All three are now populated, so the guard does real work
  for each (`tests/security/test_decision_path_isolation.py` scans real files,
  not empty directories). `forecasting/` was the last to fill, at task 11, and
  the guard shaped its design rather than being retrofitted to it: the panel
  had to be persisted from `evaluation/panels.py` precisely because
  `forecasting/` may not hold a session. That is what writing the guard before
  the code it guards is for.

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
| Imposed panel persisted and elicited inside the cycle (§15.4) | `done` | `tests/unit/test_panel_store.py`, `tests/unit/test_cycle_runner.py` |
| One supported cycle step order, shared by the run and the replay | `done` | `tests/unit/test_cycle_driver.py` |
| Deterministic total-return forecast resolution, five statuses (§20) | `done` | `tests/unit/test_resolution.py`, `tests/unit/test_resolution_store.py` |
| Proper scoring rule and calibration table (§21.1) | `done` | `tests/unit/test_scoring.py` |
| Paired analysis: (date, instrument) pairing, cross-sectional aggregation, real block bootstrap, TOST/ROPE, multiplicity (§21.2–§21.7) | `done` | `tests/unit/test_analysis.py`, `tests/unit/test_bootstrap.py` |
| Resolution and the analysis plan wired end to end over the synthetic world | `done` | `tests/integration/test_evaluation_wiring.py` |
| Real replay that recomputes and compares (§12.5, §30.4) | `done` | `tests/replay/test_replay_verifier.py` |
| Persisted, unchangeable run configuration (§29.2) | `done` | `tests/cli/test_cli.py` |
| CLI — help, exit codes, structured logs, `--dry-run`, idempotent resume (§29) | `done` | `tests/cli/test_cli.py` |
| Property-based temporality and accounting (§30.2, §30.5) | `done` | `tests/property/test_temporality.py`, `tests/property/test_accounting.py` |
| Injection fixtures routed as far as the prompt (§30.7) | `done` | `tests/security/test_injection_routing.py` |
| Every failure kind, rejection reason and scope exercised (§23, §30.8) | `done` | `tests/unit/test_failure_taxonomy.py`, `tests/unit/test_execution_failures.py` |
| Exact, hand-computed analysis values (§30.9) | `done` | `tests/unit/test_analysis_values.py` |
| Quality gates installed and run in CI (§30.10) | `done` | `tests/test_quality_gates.py`, `.github/workflows/ci.yml` |

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
- **The cycle's step order now lives in `marketlab.experiments.driver`.**
  `CycleDriver.run` is the single supported sequence — reference data,
  settlement, corporate actions, fills, decisions, placement — and
  `tests/unit/test_cycle_driver.py` asserts it directly rather than inferring
  it from a downstream number. The integration test and the replay both go
  through it, so the sequence is no longer a convention two places agree on.
  `marketlab.study.pipeline.open_study` is now the only assembly the CLI, the
  integration tests and the replay use. What is still *not* enforced is that a
  caller uses it at all: nothing stops new code from wiring the components up
  by hand, and only review would catch it.
- **Resolution runs as a pass over a run, not as a step inside the cycle.**
  Correct for a completed study and safe to re-run (it is idempotent and only
  ever writes terminal verdicts), but a prospective study resolving as it goes
  would want it per cycle. Nothing consumes outcomes during a run today —
  precisely because memory outcome feedback is deferred (open question 9) —
  so the distinction currently costs nothing.

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
- **Reflection still says nothing about whether forecasts came true, and
  memory still records only what a condition decided.** Resolution now exists
  (task 12), so the raw material for outcome feedback is there — but wiring it
  into the granted material is a **change to the treatment**, not a missing
  feature. §13 defines what each arm is given; an arm that is shown its own
  hit rate is a different arm from one shown its own past decisions, and the
  difference would have to be re-piloted rather than slipped in. It would also
  have to be done point-in-time (only outcomes whose target session is
  strictly before the recall cutoff) and the placebo would have to match the
  *resolved* forecast count, not just the forecast count. Raised as open
  question 10 for the study owner rather than decided here. A test still
  asserts that no reflection rule claims accuracy today.
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
- **The panel runs only when `CycleRunner` is given a `PanelStore`.** Left
  unset, no panel is elicited at all — deliberately, so that a study with no
  panel has *nothing recorded* rather than an empty panel recorded as though
  it had been asked. It also means a run configured without one produces
  nothing `marketlab.analysis.pairing` can compare, and the CLI (task 13) is
  where that should stop being possible by accident.
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
- **A run's configuration is persisted and is unchangeable.**
  `marketlab.study.config.StudyConfig` holds every pre-registered parameter,
  is written to the `runs` table under its `run_id` on first use, and is
  checked by fingerprint on every later one — re-running with a different
  target weight is refused rather than honoured. What is still *not* captured
  is the trading calendars, which are objects with behaviour rather than
  values; they are named by `world` and rebuilt by the world builder. Honest
  for a fixed synthetic script, and a real Phase 3 universe would need a
  persisted calendar registry of its own.
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

- **The horizon grid is the run's own decision cadence, not each instrument's
  trading calendar.** "In N sessions" is resolved against the ordered instants
  at which the run actually decided, read back from its snapshots. A
  per-instrument calendar grid is more faithful for a universe spanning
  several calendars, but resolves to instants at which no snapshot exists and
  therefore no price was ever frozen. `marketlab.evaluation.resolution.SessionGrid`
  is the single type to change. Stated as an interpretation, not a quotation
  of §20.
- **A flat close is scored as "not up".** A rise is strictly positive. Rare at
  four decimal places, but the convention had to be fixed in advance rather
  than settled once someone noticed a tie in the data.
- **A dividend's cash is not reinvested in the total-return calculation.** It
  is added to the terminal value at its face amount, matching what the ledger
  actually does with it (credit cash, earn nothing). A reinvested-dividend
  convention would be a different, equally defensible definition; it is not
  the one implemented.
- **The log score is deliberately not offered.** It is proper and better at
  punishing overconfidence, but infinite at `p ∈ {0, 1}`, which real models do
  emit — so every implementation clips, and the clip value silently sets how
  much a single confident error is worth. That is a pre-registration decision
  with real consequences for the primary metric, and
  `marketlab.evaluation.scoring` will not invent one. `ABSOLUTE_ERROR` is
  offered as a robustness check and is documented as **improper**.
- **Calibration bin edges are a placeholder.** Five equal bins, like the tool
  budget and the recall depth. Empty bins are omitted rather than reported as
  a zero rate, so a coarse choice cannot manufacture a finding.
- **The bootstrap block length is a rule of thumb (`round(n**(1/3))`), not an
  estimate.** Data-driven selectors exist and depend on the autocorrelation
  the study has not measured yet (task #4). The value used is carried on every
  result, so an interval always says which block length produced it.
- **Equivalence is tested off the bootstrap, not off a t-distribution.** TOST
  at level α corresponds to a `1 - 2α` percentile interval, and the ROPE
  decision rule is the three-valued one (inside → equivalent, disjoint →
  different, straddling → inconclusive). This avoids assuming normality on a
  few dozen dates; it inherits the block bootstrap's own coverage properties,
  which are asymptotic and unmeasured at this sample size — another thing
  task #4's power simulation exists to check.
- **Only complete cases are analysed, and imputation is not offered.** A cell
  enters the comparison only if every compared arm has a resolved score for
  it. What is dropped is counted and reported per reason
  (`PairedSample.dropped_by_reason`), which is what makes §23.4's paired
  policy checkable rather than asserted.
- **Repetitions of one arm are averaged within a cell, not stacked.** They are
  two draws from one condition, not two observations of the world. A cell
  where the arms are represented by *different* numbers of repetitions is
  dropped rather than weighted unequally.
- **The analysis compares forecast quality only.** Decision stability under
  identical bundles, evidential fidelity and portfolio outcomes are all
  recorded (`decision_bundles.content_hash`, the citation validation, the
  ledger) but none has a comparison plan yet. Which of them is the *primary*
  metric is open question 1, still waiting on the power simulation.

- **`marketlab replay --run-id X` is now self-contained; the library call is
  not.** The command reads the persisted configuration and rebuilds this
  world's calendars, so a replay needs nothing but the database.
  `ReplayVerifier` itself still takes a `ReplayConfig`, and one constructed by
  hand with a different policy will report divergences — correct, since that
  would genuinely be a different study, but it is a foot-gun the CLI avoids
  and a direct caller does not.
- **A replay cannot re-elicit a model, by construction.** Sealed decisions and
  panels are inputs to it, exactly as the raw market data is; their
  fingerprints are re-derived from the payloads behind them, and everything
  downstream — sizing, filling, settling, bookkeeping, corporate actions,
  resolution — is recomputed from nothing. The model factory a replay hands
  every runner raises if constructed, so this is structural rather than a
  convention.
- **Ledger and position comparison is on balances, not on individual rows.**
  An entry id embeds its transaction id, which legitimately differs when
  nothing else does. What must match to the cent is what the books say; a
  replay that produced the same balances by a different sequence of postings
  would not be flagged.

- **The CLI runs one world.** `StudyConfig.world` accepts only `SYNTHETIC`,
  and a Phase 3 configuration would need a second world builder plus the
  persisted calendar registry noted above. Rejected loudly rather than
  silently defaulted, so a configuration naming a world that does not exist
  fails at declaration.
- **The failure-taxonomy guard checks that a member is *named* by a test, not
  that it is asserted on.** A weak check, deliberately kept because it is
  cheap and it fired: adding it surfaced four members nothing tested. It would
  not catch a test that mentions a kind in a comment and asserts nothing, and
  no automated check would; that is what review is for.
- **`tests/test_quality_gates.py` checks the gates are declared, installed and
  invoked — not that CI is green.** A workflow file can exist and every run of
  it can fail. The badge for that is GitHub's, not this repository's, and this
  file does not claim it.
- **CI has never run on GitHub.** `origin/main` does not exist; nothing has
  ever been pushed. The workflow's exact command sequence has been executed
  twice locally on Windows — once in the working tree and once in a fresh
  `git clone` with `uv sync --frozen`, which is the stronger check — but the
  Linux leg of the matrix remains untested in practice. `.gitattributes` now
  forces LF so that the formatting gate cannot differ between the two, which
  was the most likely way that first run would have failed. The first push is
  where the claim becomes real.
- **There is no coverage threshold.** `pytest-cov` is installed and unused. A
  percentage target tends to be met by testing what is easy rather than what
  is load-bearing, and this suite's guards — condition isolation, the
  append-only triggers, the replay, the taxonomy scan — are chosen for what
  they would catch rather than for what they touch. Worth revisiting if the
  suite ever stops being read.
- **Publication-readiness is checked, not assumed.**
  `tests/test_release_readiness.py` verifies that the LICENSE matches the
  declared metadata, that every README link resolves, that the README carries
  the "no real money" and "no result here is about memory yet" statements, and
  that the pre-registration's status banner agrees with how many of its
  decisions are still open. It does **not** check that the documentation is
  good — only that specific load-bearing statements are present and mutually
  consistent.
- **The pre-registration is a frame, not a pre-registration.**
  `docs/PRE_REGISTRATION.md` is complete in structure and marked *not yet
  binding*: six decisions are still open, three of which wait on task #4. It
  becomes a real pre-registration when those are filled in a signed, tagged
  commit made **before** the first confirmatory run.
- **Structured logs are per-command records, not a log stream.** `--json`
  emits one canonical JSON object per result on stdout; there is no
  correlation id, no severity, and no timestamp on each line. Enough for
  `| jq` and for a scheduler's exit code, not enough for a log aggregator.
  §29.4 is satisfied in the sense that matters here (machine-readable output,
  results separated from progress) and not in the sense a production service
  would need.

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
| §20 | "In N sessions", on the instrument's calendar | N points on the **run's own decision grid**, read back from its snapshots | The synthetic world prices every instrument at the US equity session close, so an EU or 24/7 calendar resolves to instants at which no snapshot exists and no price was ever frozen. The grid is identical for every arm, deterministic, and reconstructible from persisted artefacts alone — and immune to a missing bar silently turning a 5-session horizon into a 6-session one. `SessionGrid` is the single type a real multi-calendar study would change. |
| §20.3 | `PENDING` among the resolution statuses | Computed but **never persisted** | Persisting it would mean either updating that row when the horizon elapses — which the append-only triggers refuse — or leaving a stale row claiming an already-resolved forecast is still open. Pending is the absence of a verdict, so it is represented by the absence of a row. |
| §21.4 | Bootstrap by blocks | Moving block bootstrap over an explicit SHA-256 key stream, sharing `core/rng.py` with the arm ordering | Same reasoning as §13.4: `random.shuffle`/`choices` are implementation details that have changed between CPython versions, and a published interval must be recomputable years later on a different interpreter. |
| §21.7 | TOST against a ROPE | TOST read off the **bootstrap distribution**, with the three-valued ROPE decision rule | A parametric TOST assumes normality on a few dozen dates. Reading both one-sided tests off the same block bootstrap that produced the interval keeps one set of distributional assumptions instead of two. "Inconclusive" is kept as a distinct verdict rather than collapsed into "no difference", which is how underpowered studies come to claim null results. |
| §21 | An analysis plan | A plan object that **cannot be constructed without a ROPE** | §21.7 requires "no practically useful effect" to be reachable, which requires a pre-registered region of practical equivalence. Giving `Rope` a default would let the library make a scientific claim on the study owner's behalf; making it a required field makes the omission impossible to overlook. |
| §29.2 | A run is launched with parameters | A run is **declared**, and re-declaring it with different parameters is refused | The specification does not say the configuration is immutable; this implementation makes it so. A study whose parameters can be edited between cycles is not pre-registered, it is one that was tuned while its results were visible. `marketlab.study.config.StudyRegistry` compares fingerprints and raises, the same conflict detection `SnapshotBuilder.build` applies to snapshots. |
| §29 | A CLI | Commands that all go through one assembly (`open_study`) and one cycle order (`CycleDriver`) | A command line that built its own component graph would be the fourth assembly in this repository, and the fourth is where two of them start to differ in a way nobody notices. |
| §30.10 | Quality gates that pass | Gates whose *installation* is itself tested | The predecessor's report claimed ruff and mypy gates that were never installed, and nothing in the repository could contradict it. `tests/test_quality_gates.py` runs the tools and reads the CI workflow, so the claim is falsifiable by a machine rather than by a reader's trust. |
| §12.5 | Exact replay | Replay of everything **downstream of the model**, into a separate database, compared field by field | A real provider is not a pure function, so re-running the decision loop would produce a different decision and report a divergence that is not a defect. Sealed decisions are inputs; their fingerprints are still re-derived from the payloads behind them. Every mistake the platform itself can make lies downstream of the model, and all of it is recomputed. See the `marketlab.replay.verifier` docstring. |

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
   reachable conclusion. That requires a pre-registered ROPE, and none is
   defined yet. As of task 12 this is enforced rather than merely noted:
   `marketlab.analysis.plan.AnalysisPlan` has no default `rope` and cannot be
   constructed without one, so no analysis can be run until the study owner
   fixes the band. A ROPE on the Brier scale is not intuitive — 0.01 is a
   large effect there — so the number should come out of the power simulation
   (task #4) alongside the primary metric.
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
9. **Should an episode carry what happened to it?** Resolution now exists, so
   a condition's memory *could* be shown that its 0.7 on Alpha five sessions
   ago turned out right. That would be a materially stronger treatment, and it
   is very likely the version a real deployment would use. It is not
   implemented, because it changes what arms B and C *are*: §13 defines each
   arm by what it is granted, and an arm shown its own hit rate is a different
   arm from one shown its own past decisions. It needs the study owner's
   decision, a point-in-time rule (only outcomes whose target session is
   strictly before the recall cutoff, or the agent sees the future), and a
   placebo matched on the *resolved* forecast count. Deliberately deferred
   rather than slipped in.
10. **`SnapshotStatus` completeness criteria (§23.2).** This implementation
   treats a snapshot as `DEGRADED`/`INVALID` based on missing *price* data for
   actively-tradable instruments only, and treats missing news/macro/FX as
   normal rather than degrading. If the specification intends a stricter or
   different completeness rule, `marketlab.snapshots.builder._compute_status`
   is the single function to change; nothing downstream assumes more than the
   three-value `SnapshotStatus` enum already exposes.
