# Contributing

## Getting a working checkout

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync --all-extras
```

The five quality gates. All of them must pass; CI runs them on Linux and
Windows on every push.

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
```

## The rules that are not negotiable

This project exists because a previous implementation of it shipped a
validation report asserting "All 24 acceptance criteria PASS", citing 14 test
modules **that did not exist**, with quality gates that were never installed
and an "exact replay" verifier that returned success unconditionally. Every
convention below is a direct response to some part of that.

### 1. Completeness is claimed in exactly one place

`docs/ROADMAP.md`. Not in a module docstring, not in a class name, not in a
commit message, and not in a passing test suite. If a capability is not listed
as `done` there, it is not done — and every `done` row names the test file that
substantiates it.

`partial` is used freely. `done` is not.

### 2. A test must be able to fail

Before you trust a new guard, break the thing it guards and watch it go red.
Several of the tests in this repository exist *because* that mutation check was
run and something did not fail. If you cannot make your test fail by
introducing the bug it claims to catch, it is not testing anything.

Guards that scan or enumerate need an anti-vacuity test of their own — see
`test_the_scan_would_notice_a_kind_nobody_tested` and
`test_the_token_search_would_catch_a_leak_if_one_existed`.

### 3. Nothing invents a value it does not have

No default probability, no assumed-flat price, no imputed cell, no ROPE chosen
by a library. Missing data produces an explicit status that is **counted**, and
the count is reported. `AnalysisPlan` cannot be constructed without a
pre-registered region of practical equivalence, and that is deliberate.

If you find yourself writing `or 0.0`, `except: pass`, or a fallback that lets
a computation proceed with a number nobody chose, stop.

### 4. History is append-only, and that is enforced by the database

Never "fix" a failing append-only trigger by removing the table from
`APPEND_ONLY_TABLES`. A correction is a new superseding version, never an edit.

### 5. The decision path may not hold a database session

`agents/`, `retrieval/` and `forecasting/` cannot import SQLAlchemy. This is
what makes point-in-time correctness structural rather than careful. If your
change needs data there, it goes through a repository whose methods already
bake in the cutoff — or it goes in a different package.

### 6. Nothing on the model path may know which arm it is

No field, no parameter, no string. `tests/security/test_condition_isolation.py`
inspects real requests from a real cycle. A condition may be *granted*
different material; it may never be *told* which condition it is.

## Style

- Docstrings explain **why**, not what. The what is in the code below them.
- When a choice departs from the specification, record it in the
  "Decisions taken that depart from the specification" table in the roadmap,
  with the reason.
- When a value is a placeholder rather than a motivated choice, say so where it
  is defined *and* in the roadmap's open questions. There are ten of those
  right now; adding an eleventh is better than quietly pretending a round
  number was reasoned.
- Tests are named as sentences describing the property, not the function under
  test.

## Commits

Scoped and self-explanatory. The message says what changed and why it matters
scientifically, not just mechanically. If a change fixes something a test
found, say which test and what it found.
