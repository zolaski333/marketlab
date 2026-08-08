# MarketLab

A prospective, reproducible research platform for studying how autonomous LLM
agents make decisions in a **virtual** multi-asset market environment.

> **No real money.** MarketLab never places a real order, never connects to a
> brokerage account, and never moves funds. All capital, positions and
> executions are virtual. It produces no investment advice.

## The question

> Do persistent memory and periodic reinjected reflection improve the
> probabilistic quality, coherence and stability of an LLM agent's decisions in
> a multi-asset market environment?

Financial performance is a **secondary, exploratory** measure. A positive return
is never treated as evidence of competence.

## Read this before anything else

**No result in this repository says anything about memory or reflection yet.**

The only model shipped here is a deterministic fake: a closed-form function of
the closing price that ignores the material each condition is granted. So all
six experimental arms are shown different things and decide identically, and a
comparison between them is a comparison of nothing.

That is deliberate, and it is pinned by a test. A fake that branched on its
injected context would manufacture a memory effect out of thin air — which is
exactly the defect an audit found in this project's predecessor. What is
established today is that the machinery works and that the channel is live: a
test double that *does* read its context produces different decisions per arm
through the identical pipeline.

Connecting a real model is Phase 3. Until then,
[`docs/ROADMAP.md`](docs/ROADMAP.md) is the only place completeness is claimed.

## Status

**Phase 1 complete**: a synthetic vertical slice that runs end to end.
877 tests, `mypy --strict` clean, five quality gates enforced in CI on Linux and
Windows.

Still open before a real study: a real provider adapter, a real data adapter,
and architecture decision records (task #5). The power simulation and cost
model are done — see [docs/POWER.md](docs/POWER.md) — so the primary metric,
the ROPE and the study duration are now decisions waiting on judgement rather
than on missing evidence.

## How the design works

A crossed 2×2 of two channels, with matched placebos.

| Arm | Memory | Reflection | Role |
|---|---|---|---|
| A | — | — | Control |
| B | genuine | — | Raw episodic recall |
| C | genuine | genuine | Both |
| D | — | genuine | Distilled strategy only |
| B′ | placebo | — | Matched control for B |
| C′ | placebo | placebo | Matched control for C |

Every arm of a cycle reads **the same frozen snapshot object** — not one
rebuilt identically, the same object — so a difference between their decisions
cannot be a difference in what they were shown. Each is asked an **imposed
panel** of identical probability questions, which is the only artefact on which
arms can be paired: two arms that forecast different instruments produce
numbers that do not mean the same thing.

Forecasts are resolved in **total return**, adjusted for splits and dividends.
The synthetic world genuinely halves an instrument's quote on its split
session, so comparing two raw closes would score a 2-for-1 as a 50% loss for
every arm that forecast it.

Five things are structural rather than careful:

- **Point-in-time correctness.** Packages on the decision path cannot import
  SQLAlchemy at all, so they cannot write a query that forgets its cutoff.
- **Append-only history.** Database triggers refuse `UPDATE` and `DELETE` on
  every scientific table; the event log is hash-chained over a monotonic
  sequence.
- **Condition blindness.** Nothing on the model path carries an arm's identity,
  verified by inspecting real requests from a real cycle.
- **Immutable pre-registration.** A run's configuration is fingerprinted on
  first use; re-running it with a changed parameter is refused.
- **Falsifiable replay.** Every order, fill, ledger balance, position and
  forecast verdict is recomputed into a separate database and compared field by
  field.

## Running a study

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync --all-extras
```

Validate a configuration without writing anything at all:

```bash
uv run marketlab run --config configs/synthetic-pilot.yaml --db data/study.db --dry-run
```

Run it. Safe to repeat — it resumes rather than redoing:

```bash
uv run marketlab run --config configs/synthetic-pilot.yaml --db data/study.db
```

Resolve the forecasts, then analyse them against a **pre-registered** region of
practical equivalence. The ROPE has no default, here or in the library:

```bash
uv run marketlab resolve --run-id SYNTHETIC_PILOT --db data/study.db
```

```bash
uv run marketlab analyse --run-id SYNTHETIC_PILOT --db data/study.db --rope-lower -0.01 --rope-upper 0.01
```

Check the run reproduces. Exits non-zero on any divergence:

```bash
uv run marketlab replay --run-id SYNTHETIC_PILOT --db data/study.db
```

Add `--json` to any command for one canonical JSON object per line on stdout,
with progress kept on stderr.

A full six-arm, twenty-session study against the deterministic fake takes about
eleven seconds and produces 120 decisions, 120 panels and 1 176 resolved
forecasts.

## Development

The five quality gates. All must pass; CI runs them on both operating systems
on every push.

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
```

`tests/test_quality_gates.py` checks that those tools are declared, are
executable in the running interpreter, and are actually invoked by the
workflow — because the predecessor's validation report claimed gates that were
never installed, and nothing in that repository could contradict it.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the conventions that are not
negotiable, and [SECURITY.md](SECURITY.md) for the threat model, including the
prompt-injection fixture the synthetic world scripts deliberately.

## Documentation

| Document | What it is for |
|---|---|
| [docs/ROADMAP.md](docs/ROADMAP.md) | **The only place completeness is claimed.** Every `done` row names the test that substantiates it; every gap is stated explicitly. |
| [docs/PRE_REGISTRATION.md](docs/PRE_REGISTRATION.md) | The study's design, fixed in advance. A complete frame; the remaining decisions are marked open. |
| [docs/POWER.md](docs/POWER.md) | Power curves, effective sample size and API cost, produced by running the real analysis pipeline over simulated worlds. The numbers behind the design decisions. |
| [docs/adr/](docs/adr/) | Architecture decision records. Empty, and the README there says why. |

## Licence

MIT. See [LICENSE](LICENSE).

If you use this in research, see [CITATION.cff](CITATION.cff) — and please
state which model was used and which pre-registration applied.
