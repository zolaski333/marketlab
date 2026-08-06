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

Phase 0 — foundations. See [docs/ROADMAP.md](docs/ROADMAP.md) for what is built
and what is not. Claims about completeness are tracked there and nowhere else:
if a capability is not listed as done, it is not done.

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
