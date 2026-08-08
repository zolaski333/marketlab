# Failure policy

What happens when something goes wrong, decided in advance, because the
alternative is deciding it while looking at the data.

`tests/test_documentation.py` checks that every member of every enum described
here is actually named in this file. A failure kind added to the code and not
to this document fails the suite.

## 1. The distinction the whole taxonomy rests on

**An agent failure is data. A platform failure is a bug.**

When the model emits malformed JSON, cites an evidence id that does not exist,
hallucinates a ticker, or refuses to answer — that is an *observation about the
model*, and precisely the kind of observation this study exists to collect. It
must be recorded and analysed.

When a hash chain breaks or the ledger will not balance, that is a defect. It
must stop something.

Confusing the two is not hypothetical. This project's predecessor returned
`None` on hallucinated tickers and wrapped execution in
`except ExecutionRefusedError: pass`. Its database contained **zero** failure
events for a study that deliberately injected three distinct agent failures.

So the distinction is encoded in the type system rather than in a convention:

- `ObservedAgentFailure` is a **frozen dataclass, not an exception.** It is
  impossible to `raise`, and therefore impossible to `except … : pass` out of
  existence. It is constructed, recorded, and carried in the result of the
  operation that produced it.
- Platform failures are exceptions deriving from `MarketLabError`, each
  carrying its own `FailureScope` so the blast radius is recorded rather than
  inferred at the call site.

## 2. Requirement levels and blast radius

### `RequirementLevel` — how binding a requirement is

| level | meaning |
|---|---|
| `INV` | Scientific invariant. Violation invalidates the affected unit. |
| `REQ` | Required for the main study; failure follows a pre-registered policy. |
| `FID` | Simulation fidelity. Absence is documented, not disqualifying. |
| `OPS` | Operational robustness, ergonomics, maintenance, observability. |

### `FailureScope` — how much a failure destroys

| scope | meaning | what the analysis does |
|---|---|---|
| `RUN_FATAL` | The whole execution must stop. | Nothing proceeds. Investigate before continuing. |
| `CYCLE_INVALID` | The cycle is unusable for the primary analysis. | Every arm's cell for that date is dropped, so no arm is advantaged. |
| `CONDITION_MISSING` | One arm or repetition is missing. | Complete-case pairing drops the pairs involving that arm; other contrasts on that date survive. |
| `DEGRADED_VALID` | Usable, flagged. | Included, and the flag is reported. |
| `OBSERVED_AGENT_FAILURE` | An agent error retained as an experimental result. | **Included as data.** Counted per kind and per arm and reported as a secondary outcome. |

`CONDITION_MISSING` is currently **recorded but not acted on** beyond the
complete-case rule: a provider outage produces a `MissingCondition`, an event,
and no bundle. §23.4's fuller paired policy is satisfied by
[ADR-0010](adr/0010-complete-cases-only.md) — drop, count, report by reason,
never impute — and nothing more elaborate is enforced.

### `SnapshotStatus` — how complete a frozen view is

| status | when |
|---|---|
| `COMPLETE` | Every actively-tradable instrument has a fresh price bar this session. |
| `DEGRADED` | A partial price gap. Usable, flagged. |
| `INVALID` | No active instrument priced at all. |

Missing news, macro or FX **never** degrades a snapshot: real markets have
news-free sessions, and the synthetic world scripts one deliberately at session
5. This is this implementation's interpretation of §23.2, not a quotation of
it, and `snapshots.builder._compute_status` is the single function to change if
a stricter rule is wanted.

## 3. Agent failures, retained as results

Thirteen kinds. Every one is counted per arm and reported.

### Malformed output

| kind | what it means |
|---|---|
| `MALFORMED_JSON` | The response could not be parsed at all. |
| `SCHEMA_VIOLATION` | It parsed, and was not the shape asked for. |
| `TRUNCATED_OUTPUT` | It stopped mid-structure. |
| `PROBABILITY_OUT_OF_RANGE` | A stated probability outside `[0, 1]`. The answer is rejected, not clipped — clipping would invent a belief the model did not state. |

### Ungrounded claims — the evidential fidelity measures

| kind | what it means |
|---|---|
| `UNRESOLVED_INSTRUMENT` | A ticker or name resolving to no instrument. A hallucination (§7.2). |
| `NONEXISTENT_EVIDENCE` | A claim citing an evidence id absent from the snapshot (§14.5). |
| `FUTURE_EVIDENCE` | A claim citing evidence dated after the cutoff. |

`FUTURE_EVIDENCE` is a check on the **platform**, not on the model. Every
`evidence_id` reachable through a `RetrievalIndex` has already passed the
cutoff filter, so the model cannot cite future evidence by any legitimate path
— and `tests/unit/test_failure_taxonomy.py` asserts it is never produced. The
kind is kept because it is the observable that would fire if the point-in-time
guarantee ever developed a hole. **A non-zero count is a platform defect, not a
finding about the model**, and any run producing one is invalid.

These three are the raw material for the *evidential fidelity* candidate
metric, which [POWER.md](POWER.md) explicitly declines to simulate: citation
validity is a property of model behaviour, not of forecast skill, and inventing
a miscitation model would mean assuming what the study is supposed to observe.

### Non-answers

| kind | what it means |
|---|---|
| `REFUSAL` | The model declined. A result, not an error. |
| `MISSING_PANEL_ITEM` | No probability produced for a mandated panel item (§15.5). Recorded explicitly so silence is never read as a shorter answer set. |
| `BUDGET_EXHAUSTED` | The per-cycle tool or evidence budget ran out. Scoped as an agent observation because how an agent spends a fixed budget is part of what is being measured. |

### Failures visible only at execution

| kind | what it means |
|---|---|
| `INSUFFICIENT_CASH` | Ordering more than the book can pay for. |
| `NON_TRADABLE_INSTRUMENT` | An order on a `RESEARCH_ONLY` / `SUSPENDED` / unvaluable instrument (§7.5). |
| `UNSUPPORTED_EXECUTION` | No honest execution model exists for the requested instrument (§16.4). |

## 4. Rejections, which are mostly not failures

`RejectionReason` says why an order did not fill. **A rejection is an execution
outcome, not necessarily an agent error.**

| reason | maps to an agent failure? |
|---|---|
| `NOT_TRADABLE` | **yes** → `NON_TRADABLE_INSTRUMENT` |
| `UNSUPPORTED_EXECUTION` | **yes** → `UNSUPPORTED_EXECUTION` |
| `INSUFFICIENT_CASH` | **yes** → `INSUFFICIENT_CASH` |
| `NOTHING_TO_SELL` | no |
| `BELOW_MINIMUM_SIZE` | no |
| `NO_EXECUTION_QUOTE` | no |
| `LIQUIDITY_EXHAUSTED` | no |

The four that do not map are the interesting half of this table.

**`NOTHING_TO_SELL`** — an agent that says SELL on something it does not hold
has not malfunctioned. Short positions are simply not modelled, and counting
this as an agent failure would score arms for being bearish, which is a bias
against exactly the behaviour the platform cannot express
([LIMITATIONS.md](LIMITATIONS.md) §2).

**`BELOW_MINIMUM_SIZE`** — the platform sized the order, not the agent.

**`NO_EXECUTION_QUOTE`** and **`LIQUIDITY_EXHAUSTED`** — an empty market is not
the agent's doing.

## 5. Platform exceptions

| exception | default scope | raised when |
|---|---|---|
| `MarketLabError` | `RUN_FATAL` | Base class. Carries a context dict. |
| `ConfigurationError` | `RUN_FATAL` | Malformed or internally inconsistent configuration — including a replay's model factory being constructed, which must never happen. |
| `TemporalLeakError` | `CYCLE_INVALID` | An attempt to read data dated after the active cutoff (INV-P5). |
| `AccountingError` | `RUN_FATAL` | The ledger would be left unbalanced. Never recoverable: an unbalanced ledger means every later number is wrong. |
| `IntegrityError` | `RUN_FATAL` | A hash, chain link or reference failed verification. |
| `ImmutabilityError` | `RUN_FATAL` | An attempt to mutate append-only scientific data. |
| `SnapshotError` | `CYCLE_INVALID` | The exogenous snapshot could not be built or is unusable. |
| `ModelProviderError` | `CONDITION_MISSING` | The provider was unreachable after the allowed retries. **Deliberately not caught by `DecisionAgent`** — a missing provider means the whole decision is missing, which is a run-level concern, not an agent-level observation to record and continue past. |
| `BudgetError` | `OBSERVED_AGENT_FAILURE` | A per-cycle budget was exceeded. The one exception whose scope makes it *data*. |

**Mid-turn budget exhaustion stops the whole decision, not just that turn.**
`DecisionAgent` does not let the model finalise with whatever partial evidence
it gathered before hitting the cap. Simpler, and worth revisiting once real
provider cost and latency tradeoffs are known.

## 6. Exit codes

Semantic, not merely zero/non-zero, so a scheduler can act on them.

| code | name | meaning |
|---|---|---|
| 0 | `OK` | The command did what it said. |
| 1 | `FAILED` | An unexpected platform failure. A bug, or an environment problem. |
| 2 | `USAGE` | Bad invocation. Reserved for the argument parser. |
| 3 | `CONFIGURATION` | The configuration is invalid, missing, or contradicts a declared run. |
| 4 | `INTEGRITY` | **A scientific check failed** — a broken hash chain, a replay divergence, a run re-declared with different parameters. The data is not to be trusted until a human looks at it. |
| 5 | `NO_DATA` | Well-formed, but nothing to act on. |

`NO_DATA` is distinct from `OK` on purpose: *"resolved 0 forecasts because none
exist"* must not look like *"resolved 0 forecasts because none were due"*.

`INTEGRITY` groups three things that feel different and are not: each means the
record no longer supports the claims made about it.

## 7. How this is kept non-vacuous

A taxonomy nothing exercises is a taxonomy that quietly stops matching the
code.

`tests/unit/test_failure_taxonomy.py` scans the test suite and requires every
member of every enum here to be **named** by some test.

That check is deliberately weak: it verifies a member is mentioned, not that it
is asserted on. It would not catch a test that names a kind in a comment and
asserts nothing, and no automated check would — that is what review is for. It
was kept because it is cheap and **it fired**: adding it surfaced four members
nothing tested at all (`INSUFFICIENT_CASH`, `NOTHING_TO_SELL`,
`BELOW_MINIMUM_SIZE`, and the platform failure scopes), which is what
`tests/unit/test_execution_failures.py` now covers.

The scan carries a vacuity sentinel — a failure kind that does not exist,
assembled at runtime so it cannot itself be matched by the scan of the file
that defines it. If the sentinel ever passes, the scan has stopped scanning.
