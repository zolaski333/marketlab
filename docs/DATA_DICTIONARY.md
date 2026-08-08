# Data dictionary

Every persisted table, every column, and the conventions that make them
readable.

`tests/test_documentation.py` checks this file against `Base.metadata`: a table
or column that exists in the schema and is missing here fails the suite, and so
does one named here that does not exist. This document cannot drift silently.

## 0. Conventions

Read these first — three of them will otherwise look like mistakes.

**All 21 tables are append-only.** Every one carries `BEFORE UPDATE` and
`BEFORE DELETE` triggers that refuse the operation. There is no exception and
no mutable side table. `marketlab.storage.database.APPEND_ONLY_TABLES` is the
list, and `create_schema` verifies every name in it corresponds to a real
table. The single audited escape hatch is `Database.migration_mode()`.
([ADR-0005](adr/0005-append-only-history-hash-chained-by-sequence.md))

**Money is `VARCHAR(48)`, holding an exact decimal string.** Never a float,
never an integer of minor units. Binary floating point cannot represent 0.1,
and a ledger that does not balance to the cent is not a ledger. Quantities and
prices follow the same rule.

**Instants are `VARCHAR(27)`, canonical UTC, fixed width.** Lexicographic order
is chronological order, so `WHERE as_of < ?` is correct without parsing. `day`
on `daily_roots` is `VARCHAR(10)`, a calendar date.

**Hashes are `VARCHAR(64)`, lowercase SHA-256 hex.** A column ending in
`_blob_hash` is a key into the content-addressed blob store; a column ending in
`_hash` without `blob` is a fingerprint computed over canonical JSON.

**There are no foreign key constraints.** Identifiers are *derived* — computed
by `derive_id(IdKind, **parts)` from the facts they identify — so a reference
is reproducible rather than allocated, and a replay computes the same
identifier without needing the original row. The cost is that referential
integrity is enforced by construction and by the replay, not by the database.

**Large payloads are not in these tables.** A decision bundle row is metadata
plus two digests; the decision itself is a blob.

## 1. Provenance and audit

### `blob_metadata` — what each stored blob is, and whether it may be republished

| column | type | notes |
|---|---|---|
| `digest` | hash | **PK.** SHA-256 of the content; the blob's only name. |
| `media_type` | str(64) | |
| `size_bytes` | int | |
| `source_id` | str(64) | indexed. Which provider supplied it. |
| `licence` | str(64) | |
| `redistributable` | bool | **Load-bearing.** Lets a study over vendor data publish its code and results without republishing the vendor's data. |
| `first_seen_at` | instant | indexed. When *the platform* received it — the cutoff compares against this, never against a claimed publication time. |
| `ingested_at` | instant | |
| `ingestion_event_id` | str(64) | indexed. The event that admitted it. |

### `events` — the hash-chained scientific log

| column | type | notes |
|---|---|---|
| `seq` | int | **PK, monotonic.** The chain is ordered by this, *not* by time: the six arms of one cycle share an instant, so timestamp order among them is undefined. |
| `event_id` | str(64) | derived |
| `event_type` | str(64) | indexed |
| `occurred_at` | instant | indexed. When the fact happened. |
| `recorded_at` | instant | When it was written. Distinct on purpose. |
| `payload_json` | text | Canonical JSON. Hashed **as stored bytes**, never re-serialised — re-serialising would mask a payload edited into a form that round-trips. |
| `payload_hash` | hash | |
| `previous_hash` | hash | The link. Altering any historical row invalidates every hash after it. |
| `event_hash` | hash | |
| `run_id` | str(64) | indexed, nullable — not every event belongs to a run |
| `cycle_id` | str(64) | indexed, nullable |
| `arm_id` | str(32) | indexed, nullable. Present here and **never** on the model path. |
| `repetition` | int | nullable |

### `daily_roots` — one summary per day, so a range can be verified without replaying the chain

| column | type | notes |
|---|---|---|
| `day` | str(10) | **PK.** Calendar date. |
| `first_seq` / `last_seq` | int | The segment covered. |
| `event_count` | int | |
| `chain_head_hash` | hash | The chain's head at the end of that day. |
| `root_hash` | hash | Over the day's segment. |
| `computed_at` | instant | |

## 2. Reference data

### `instruments` — admission

| column | type | notes |
|---|---|---|
| `instrument_id` | str(64) | **PK**, derived |
| `asset_class` | str(64) | |
| `admitted_at` | instant | |

### `instrument_versions` — every revision, superseding rather than editing

| column | type | notes |
|---|---|---|
| `version_id` | str(64) | **PK** |
| `instrument_id` | str(64) | indexed |
| `version_number` | int | |
| `ticker` | str(64) | indexed. Tickers are reused across issuers; the id is not. |
| `name` | str(64) | |
| `quote_currency` | str(8) | |
| `native_timezone` | str(64) | |
| `calendar_code` | str(64) | |
| `settlement_days` | int | T+N, counted on this instrument's own calendar. |
| `status` | str(64) | Drives daily tradability (§7.5). |
| `execution_model` | str(64) | |
| `effective_from` | instant | |
| `supersedes_version_id` | str(64) | nullable |
| `created_at` | instant | |

**There is deliberately no `effective_to`.** Storing one would mean mutating
the previous version's row the moment a new one is written — an in-place edit
of a past fact. "Current as of a cutoff" is computed as the latest version
whose `effective_from` does not exceed it. This is a stated departure from
§7.1.

## 3. Point-in-time snapshots

### `snapshots` — one frozen view of the world per cycle

| column | type | notes |
|---|---|---|
| `snapshot_id` | str(64) | **PK**, derived from the run and the instant |
| `run_id` | str(64) | indexed |
| `as_of` | instant | indexed. The cutoff. |
| `status` | str(64) | `COMPLETE` / `DEGRADED` / `INVALID`, on *price* completeness for actively-tradable instruments only. Missing news or FX never degrades a snapshot — real markets have news-free sessions. This is an interpretation of §23.2, not a quotation. |
| `member_count` | int | |
| `manifest_hash` | hash | Over the member set. |
| `universe_blob_hash` | hash | |
| `built_at` | instant | |

### `snapshot_members` — what evidence was visible at that instant

| column | type | notes |
|---|---|---|
| `member_id` | str(64) | **PK** |
| `snapshot_id` | str(64) | indexed |
| `evidence_id` | str(64) | indexed. What a model cites. A citation of an id absent here is a `NONEXISTENT_EVIDENCE` failure. |
| `record_type` | str(64) | |
| `blob_hash` | hash | |
| `first_seen_at` | instant | |

## 4. The experiment

### `runs` — the pre-registration's binding

| column | type | notes |
|---|---|---|
| `run_id` | str(64) | **PK** |
| `world` | str(64) | Only `SYNTHETIC` today; a wrong value fails at declaration rather than defaulting. |
| `config_hash` | hash | indexed. **Fingerprint of every pre-registered parameter.** Re-declaring the run with any of them changed is refused, exit code 4. ([ADR-0011](adr/0011-a-run-is-declared-not-launched.md)) |
| `config_blob_hash` | hash | The configuration itself. |
| `declared_at` | instant | |

### `decision_bundles` — one sealed free decision, per arm per repetition per cycle

| column | type | notes |
|---|---|---|
| `bundle_id` | str(64) | **PK**, derived from run/cycle/arm/repetition |
| `run_id`, `cycle_id`, `snapshot_id`, `arm_id` | str(64) | all indexed |
| `repetition` | int | |
| `position` | int | Where in the Latin square this arm ran. |
| `as_of` | instant | indexed |
| `sealed_at` | instant | |
| `model_id` | str(64) | |
| `content_hash` | hash | indexed. Over the **decision only** — deliberately excluding failures and process metrics, so two identical decisions reached in different numbers of turns hash the same. Good for "did these arms decide alike"; it means a comparison keyed on this alone would not notice one arm emitting three malformed outputs on the way. |
| `payload_blob_hash` | hash | |
| `context_blob_hash` | hash | **nullable — `NULL` is arm A.** The granted material, verbatim, so the treatment is auditable after the fact. |
| `forecast_count`, `trade_intent_count`, `failure_count` | int | |
| `tool_calls_made`, `model_turns` | int | |
| `input_tokens`, `cached_input_tokens`, `output_tokens` | int | As the provider reported. **Zero means unmeasured, not free** — the deterministic fake reports nothing, and `measure_profile` refuses to build a cost profile from a run of zeros. |

### `panel_bundles` — one sealed panel elicitation; **the only pairable artefact**

Two arms that forecast different instruments produce numbers that do not mean
the same thing, so the analysis pairs on this table and not on
`decision_bundles`. ([ADR-0002](adr/0002-imposed-panel-as-unit-of-analysis.md))

| column | type | notes |
|---|---|---|
| `panel_bundle_id` | str(64) | **PK**, derived from the decision bundle |
| `decision_bundle_id` | str(64) | indexed. The free decision it accompanies. |
| `run_id`, `cycle_id`, `snapshot_id`, `arm_id` | str(64) | all indexed |
| `repetition` | int | |
| `as_of` | instant | indexed |
| `sealed_at` | instant | |
| `model_id` | str(64) | |
| `item_count` | int | Questions asked. |
| `answered_count` | int | |
| `unanswered_count` | int | Recorded rather than inferred: silence is a result, not a shorter answer set. |
| `tool_calls_made`, `model_turns` | int | Its **own** budget — answering the panel costs an arm nothing it could have spent deciding. |
| `input_tokens`, `cached_input_tokens`, `output_tokens` | int | |
| `content_hash` | hash | indexed |
| `payload_blob_hash` | hash | |

## 5. The treatment

### `memory_episodes` — what one condition decided, in its own scope

| column | type | notes |
|---|---|---|
| `episode_id` | str(64) | **PK** |
| `scope_id` | str(64) | indexed. `memory_scope_id(run, arm, repetition)` — the isolation that stops one arm recalling another's history, the same way `portfolio_id` isolates the books. |
| `cycle_id`, `bundle_id` | str(64) | indexed |
| `as_of` | instant | indexed. Recall is **strictly before** this, never `<=`: an episode is written at the instant of the decision it records, so `<=` would hand a condition its own current decision as history. |
| `recorded_at` | instant | |
| `payload_blob_hash` | hash | |
| `forecast_count`, `intent_count`, `failure_count` | int | These integer columns are what a **placebo** is shaped from — `EpisodeShape` reads only these, so a placebo is structurally incapable of carrying genuine content. |
| `equity` | str(64) | The portfolio value rendered into the memory text. **This is the one channel by which an arm's divergent trajectory reaches its own decisions** — see [ADR-0016](adr/0016-treatment-is-endogenous-to-trajectory.md). |

### `reflections` — distilled strategy, closed-form

| column | type | notes |
|---|---|---|
| `reflection_id` | str(64) | **PK** |
| `scope_id` | str(64) | indexed |
| `as_of` | instant | indexed |
| `recorded_at` | instant | |
| `rule_count` | int | |
| `payload_blob_hash` | hash | |
| `episode_ids_json` | text | Which episodes it was drawn from — the provenance that makes a rule checkable. |

## 6. Execution and the books

### `orders` — an intent, sized and scheduled

| column | type | notes |
|---|---|---|
| `order_id` | str(64) | **PK** |
| `portfolio_id` | str(64) | indexed. One book per arm per repetition. |
| `bundle_id` | str(64) | indexed. Which decision produced it. |
| `instrument_id` | str(64) | indexed |
| `side` | str(64) | |
| `quantity` | decimal str | Sized by the platform at a fixed fraction, identically for every arm. A model states no size. ([ADR-0014](adr/0014-identical-fixed-fraction-sizing.md)) |
| `currency` | str(64) | |
| `decided_at` | instant | indexed |
| `execute_after` | instant | indexed. **Strictly after** the decision — nothing fills at the price it was decided on. |
| `placed_at` | instant | |

### `order_rejections` — why an order did not fill

| column | type | notes |
|---|---|---|
| `rejection_id` | str(64) | **PK** |
| `order_id` | str(64) | indexed |
| `portfolio_id` | str(64) | indexed |
| `instrument_id` | str(64) | |
| `reason` | str(64) | indexed. One of seven `RejectionReason` values, of which three map to an agent failure — see [FAILURE_POLICY.md](FAILURE_POLICY.md) §3. Selling something you do not hold is not a malfunction; shorts simply are not modelled. |
| `detail` | str(64) | |
| `occurred_at` | instant | indexed |

### `fills` — what actually executed

| column | type | notes |
|---|---|---|
| `fill_id` | str(64) | **PK** |
| `order_id` | str(64) | indexed |
| `portfolio_id`, `instrument_id` | str(64) | indexed |
| `side` | str(64) | |
| `quantity` | decimal str | What filled. |
| `requested_quantity` | decimal str | What was asked. Both are kept so a partial fill is visible without a join. |
| `price` | decimal str | |
| `gross`, `fee`, `slippage` | decimal str | Decomposed rather than netted, so the cost model is inspectable. |
| `realized_pnl` | decimal str | FIFO against the lots. |
| `currency` | str(64) | |
| `executed_at` | instant | indexed |
| `settles_at` | instant | indexed. T+N on the **instrument's** calendar. |
| `transaction_id` | str(64) | The ledger transaction. |

### `settlements` — cash actually moving, at T+N

| column | type | notes |
|---|---|---|
| `settlement_id` | str(64) | **PK** |
| `fill_id` | str(64) | indexed |
| `portfolio_id` | str(64) | indexed |
| `settled_at` | instant | indexed |
| `transaction_id` | str(64) | |

### `ledger_transactions` — one balanced posting

| column | type | notes |
|---|---|---|
| `transaction_id` | str(64) | **PK** |
| `portfolio_id` | str(64) | indexed |
| `transaction_type` | str(64) | indexed |
| `occurred_at` | instant | indexed |
| `recorded_at` | instant | |
| `reference_json` | text | What it was for. |
| `entry_count` | int | |

### `ledger_entries` — signed amounts that must sum to zero

| column | type | notes |
|---|---|---|
| `entry_id` | str(64) | **PK** |
| `transaction_id` | str(64) | indexed |
| `portfolio_id` | str(64) | indexed |
| `account_code` | str(64) | indexed |
| `currency` | str(64) | indexed. Balance is enforced **per currency** — there is no FX conversion. |
| `subject` | str(64) | |
| `amount` | decimal str | **Signed**, positive debits. A `direction` enum beside an unsigned amount turns the balance check into a conditional sum, which is a place to get the sign wrong. With signed amounts, "this balances" is literally addition. |
| `occurred_at` | instant | indexed |
| `memo` | str(64) | |

### `position_events` — immutable open/close events, lots folded on read

| column | type | notes |
|---|---|---|
| `event_id` | str(64) | **PK** |
| `portfolio_id`, `instrument_id`, `lot_id` | str(64) | indexed |
| `occurred_at` | instant | indexed |
| `sequence` | str(64) | Orders events sharing an instant. |
| `quantity_delta` | decimal str | Signed. |
| `unit_cost` | decimal str | |
| `currency` | str(64) | |
| `reason` | str(64) | |
| `reference_id` | str(64) | The fill or corporate action behind it. |

A `quantity_remaining` column decremented on every sale would be an in-place
edit of a past fact. Folding also makes "what did the book hold at instant *t*"
answerable for every *t*.

### `corporate_action_applications` — a split or dividend applied, once

| column | type | notes |
|---|---|---|
| `application_id` | str(64) | **PK**, derived — which is what makes application **idempotent**: re-running a cycle cannot pay the same dividend twice. |
| `scope_id` | str(64) | indexed. The portfolio or the reference repository. |
| `evidence_id` | str(64) | indexed. The snapshot record that justified it. |
| `instrument_id` | str(64) | indexed |
| `action_type` | str(64) | |
| `applied_at` | instant | indexed |
| `detail` | str(64) | |

## 7. Evaluation

### `forecast_resolutions` — a terminal verdict on one forecast

| column | type | notes |
|---|---|---|
| `resolution_id` | str(64) | **PK** |
| `forecast_id` | str(64) | indexed |
| `run_id` | str(64) | indexed |
| `source` | str(64) | indexed. `DECISION` or `PANEL`. **Only `PANEL` rows are confirmatory.** |
| `source_bundle_id` | str(64) | indexed |
| `arm_id` | str(64) | indexed |
| `repetition` | int | |
| `instrument_id` | str(64) | indexed |
| `horizon_sessions` | int | indexed |
| `probability_up` | float | The only float in the schema, and correctly so: it is a stated belief, not money. |
| `anchor_at`, `target_at` | instant | indexed. The half-open window `(anchor, target]`. |
| `status` | str(64) | indexed. `RESOLVED` / `DELISTED` / `EXPIRED` / `SUSPENDED`. **`PENDING` is computed and never written** — persisting it would need either a later `UPDATE`, which the triggers refuse, or a stale row claiming an already-resolved forecast is open. Pending is the absence of a row. |
| `outcome_up` | bool | **Nullable — `NULL` on every censoring status.** |
| `anchor_close`, `target_close` | decimal str | |
| `split_factor` | decimal str | The synthetic world genuinely halves a quote on its split session. |
| `dividends` | decimal str | Face value, not reinvested — matching what the ledger actually does with the cash. |
| `total_return` | decimal str | `(split_factor × target_close + dividends) / anchor_close − 1`. Stored so a published score can be checked without recomputing. |
| `detail` | str(64) | |
| `resolved_at` | instant | |

## 8. What is not stored

- **No `PENDING` resolutions** — see above.
- **No aggregated scores or analysis results.** `marketlab analyse` recomputes
  from resolutions every time. A cached result is a result that can disagree
  with its inputs.
- **No coverage of trading calendars.** They are objects with behaviour rather
  than values, named by `world` and rebuilt by the world builder. This is the
  largest gap in the configuration fingerprint: a change to calendar *code*
  would change the study without changing its fingerprint.
- **No credentials, ever.** Phase 1 handles none; a Phase 3 adapter's keys
  belong in the environment and never in a config, a payload or `data/`.
