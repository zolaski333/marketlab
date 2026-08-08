"""Tests for the one supported order of a cycle's steps (§16, §17.5).

The order is the whole point of the driver, so it is asserted directly rather
than inferred from a downstream number. A driver that settled after filling,
or applied a split after a sale, would produce books that balance and are
wrong — and the integration test only catches that when the synthetic world
happens to script the right collision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from marketlab.core.failures import SnapshotStatus
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.execution.types import ExecutionReport
from marketlab.experiments.arms import ArmId
from marketlab.experiments.driver import CycleDriver, portfolios_for
from marketlab.experiments.runner import ArmExecution, CycleResult, RunConfig
from marketlab.retrieval.types import RetrievalIndex

AS_OF = instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC))
CONFIG = RunConfig(run_id="DRIVER_RUN", arms=(ArmId.A, ArmId.B))
INDEX = RetrievalIndex(
    snapshot_id="snap-1",
    cutoff=AS_OF,
    status=SnapshotStatus.COMPLETE,
    universe=(),
    evidence=(),
)


@dataclass
class _Spy:
    """Records what the driver called, and in what order."""

    calls: list[str] = field(default_factory=list)


@dataclass
class _Engine:
    spy: _Spy

    def settle_due(self, *, portfolio_id: str, now: Instant) -> tuple[str, ...]:
        self.spy.calls.append(f"settle:{portfolio_id[:6]}")
        return ()

    def execute_due(
        self, *, portfolio_id: str, index: RetrievalIndex, now: Instant
    ) -> ExecutionReport:
        self.spy.calls.append(f"fill:{portfolio_id[:6]}")
        return ExecutionReport(fills=(), rejections=())

    def place_orders(
        self,
        intents: Any,
        *,
        portfolio_id: str,
        bundle_id: str,
        index: RetrievalIndex,
        decided_at: Instant,
    ) -> tuple[Any, ...]:
        self.spy.calls.append(f"place:{portfolio_id[:6]}")
        return ()


@dataclass
class _Applier:
    spy: _Spy

    def apply_to_reference_data(self, index: RetrievalIndex) -> tuple[Any, ...]:
        self.spy.calls.append("reference")
        return ()

    def apply_to_portfolio(self, index: RetrievalIndex, *, portfolio_id: str) -> tuple[Any, ...]:
        self.spy.calls.append(f"corporate:{portfolio_id[:6]}")
        return ()


@dataclass
class _Runner:
    spy: _Spy
    config: RunConfig = CONFIG
    builder: Any = None

    def run_cycle(self, *, cycle_index: int, snapshot_id: str, as_of: Instant) -> CycleResult:
        self.spy.calls.append("decide")
        return CycleResult(
            cycle_id="cycle-1",
            run_id=self.config.run_id,
            cycle_index=cycle_index,
            snapshot_id=snapshot_id,
            as_of=as_of,
            executions=(),
        )


@dataclass
class _Builder:
    index: RetrievalIndex

    def load_index(self, snapshot_id: str) -> RetrievalIndex:
        return self.index


def _driver(spy: _Spy) -> CycleDriver:
    return CycleDriver(
        runner=_Runner(spy, builder=_Builder(INDEX)),  # type: ignore[arg-type]
        engine=_Engine(spy),  # type: ignore[arg-type]
        applier=_Applier(spy),  # type: ignore[arg-type]
    )


def test_reference_data_is_applied_before_any_book_is_touched() -> None:
    """Every arm must read one universe. A ticker change applied per book
    would be the same fact written six times, and could land differently."""
    spy = _Spy()
    _driver(spy).open_cycle(INDEX, as_of=AS_OF)
    assert spy.calls[0] == "reference"


def test_each_book_settles_then_takes_corporate_actions_then_fills() -> None:
    """Entitlement is settled before this session's fills, which is what makes
    buying on the ex-date not earn the dividend."""
    spy = _Spy()
    _driver(spy).open_cycle(INDEX, as_of=AS_OF)
    per_book = [call.split(":") for call in spy.calls if ":" in call]
    for portfolio_id in {book for _, book in per_book}:
        steps = [step for step, book in per_book if book == portfolio_id]
        assert steps == ["settle", "corporate", "fill"]


def test_decisions_come_after_every_book_is_up_to_date() -> None:
    """A decision taken against a book that has not yet settled or been
    adjusted is a decision taken on stale equity."""
    spy = _Spy()
    _driver(spy).run(cycle_index=0, snapshot_id="snap-1", as_of=AS_OF)
    decide = spy.calls.index("decide")
    assert all(spy.calls.index(call) < decide for call in spy.calls if call.startswith("fill:"))
    assert all(spy.calls.index(call) < decide for call in spy.calls if call.startswith("settle:"))


def test_orders_are_placed_only_after_the_decision() -> None:
    spy = _Spy()
    driver = _driver(spy)
    driver.run(cycle_index=0, snapshot_id="snap-1", as_of=AS_OF)
    driver.place(
        [
            ArmExecution(
                bundle_id="bundle-1",
                arm_id=ArmId.A,
                repetition=0,
                position=0,
                model_id="m",
                content_hash="h",
                context_blob_hash=None,
                outcome=_empty_outcome(),
            )
        ],
        index=INDEX,
        as_of=AS_OF,
    )
    assert spy.calls.index("decide") < spy.calls.index(
        next(call for call in spy.calls if call.startswith("place:"))
    )


def test_every_arm_and_repetition_gets_its_own_book() -> None:
    config = RunConfig(run_id="R", arms=(ArmId.A, ArmId.B), repetitions=2)
    books = portfolios_for(config)
    assert len(books) == 4
    assert len(set(books.values())) == 4


def _empty_outcome() -> Any:
    from marketlab.agents.decision import DecisionOutcome

    return DecisionOutcome(
        snapshot_id="snap-1",
        forecasts=(),
        trade_intents=(),
        failures=(),
        tool_calls_made=0,
        model_turns=1,
    )
