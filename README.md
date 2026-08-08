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

## Status

Phase 1 — a synthetic vertical slice that runs end to end. See
[docs/ROADMAP.md](docs/ROADMAP.md) for what is built and what is not. Claims
about completeness are tracked there and nowhere else: if a capability is not
listed as done, it is not done.

In particular: the shipped model is a deterministic fake that ignores the
material each condition is granted, so **no arm comparison produced by this
repository today says anything about memory or reflection.** What is
established is that the machinery works and that the channel is live.

## Running a study

Every parameter is pre-registered in a configuration file and is refused if it
changes after the run is declared.

```bash
uv run marketlab run --config configs/synthetic-pilot.yaml --db data/study.db --dry-run
```

```bash
uv run marketlab run --config configs/synthetic-pilot.yaml --db data/study.db
```

Then resolve the forecasts, analyse them against a pre-registered region of
practical equivalence, and check the run reproduces:

```bash
uv run marketlab resolve --run-id SYNTHETIC_PILOT --db data/study.db
```

```bash
uv run marketlab analyse --run-id SYNTHETIC_PILOT --db data/study.db --rope-lower -0.01 --rope-upper 0.01
```

```bash
uv run marketlab replay --run-id SYNTHETIC_PILOT --db data/study.db
```

`run` is idempotent: issue it again and it resumes rather than redoing. `replay`
recomputes every order, fill, ledger balance, position and forecast resolution
into a separate database and exits 4 on any divergence. Add `--json` to any
command for one canonical JSON object per line on stdout.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync --all-extras
```

Quality gates — all five must pass:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
```

## Licence

MIT. Data collected from third-party providers is governed separately; see
`docs/PROVIDER_POLICY.md`.
