"""The one supported order of a cycle's steps (§16, §17.5, §30.6).

Until this module existed, the sequence lived in a test and in a comment. That
is a real hazard rather than a tidiness complaint:
:data:`marketlab.accounting.positions._SEQUENCE_RANK` encodes the same order
for the position fold, and the two agreeing was a convention nobody enforced.
A driver that settled after filling, or applied a split after a sale, would
produce books that balance and are wrong — the exact failure the integration
test caught once already.

The order, and why
------------------
1. **Reference data.** A ticker change is a fact about the world, identical
   for every condition. Applying it first means every arm reads one universe.
2. **Settlement.** Cash owed from T-N arrives before anything can spend it.
3. **Corporate actions on each book.** Entitlement is settled *before* this
   session's fills, so buying on the ex-date does not earn the dividend.
4. **Fills.** Orders decided last session execute now — never at the instant
   they were decided (§16.2).
5. **Decisions.** Only now, against a book whose state is already final for
   this session.
6. **Placement.** New orders, scheduled for their next eligible window.

Split in three, for the replay
------------------------------
:meth:`CycleDriver.open_cycle` and :meth:`CycleDriver.place` are separate from
:meth:`CycleDriver.decide` because a replay (§12.5) cannot re-elicit a model.
It reads the sealed decisions and runs the same two halves around them, so the
execution path a replay checks is the path the run took rather than a second
implementation of it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from marketlab.core.instants import Instant
from marketlab.execution.corporate import AppliedAction, CorporateActionApplier
from marketlab.execution.engine import ExecutionEngine, portfolio_id_for
from marketlab.execution.types import ExecutionReport, Order
from marketlab.experiments.arms import ArmId
from marketlab.experiments.runner import ArmExecution, CycleResult, CycleRunner, RunConfig
from marketlab.retrieval.types import RetrievalIndex

__all__ = ["CycleDriver", "CycleReport", "OpenedCycle", "portfolios_for"]


def portfolios_for(config: RunConfig) -> dict[tuple[ArmId, int], str]:
    """One book per (arm, repetition), derived the same way everywhere."""
    return {
        (unit.arm_id, unit.repetition): portfolio_id_for(
            config.run_id, str(unit.arm_id), unit.repetition
        )
        for unit in config.units()
    }


@dataclass(frozen=True, slots=True)
class OpenedCycle:
    """What happened to the books before anyone decided anything."""

    reference_actions: tuple[AppliedAction, ...]
    settled: Mapping[str, tuple[str, ...]]
    portfolio_actions: Mapping[str, tuple[AppliedAction, ...]]
    executions: Mapping[str, ExecutionReport]


@dataclass(frozen=True, slots=True)
class CycleReport:
    """One whole cycle, from reference data to newly placed orders."""

    opened: OpenedCycle
    cycle: CycleResult
    placed: Mapping[str, tuple[Order, ...]]


@dataclass(slots=True)
class CycleDriver:
    """Runs one cycle's steps in the order the platform supports."""

    runner: CycleRunner
    engine: ExecutionEngine
    applier: CorporateActionApplier

    @property
    def config(self) -> RunConfig:
        return self.runner.config

    def portfolios(self) -> dict[tuple[ArmId, int], str]:
        return portfolios_for(self.config)

    # -- the three halves ----------------------------------------------------

    def open_cycle(self, index: RetrievalIndex, *, as_of: Instant) -> OpenedCycle:
        """Steps 1-4: bring every book up to date before any decision."""
        reference_actions = self.applier.apply_to_reference_data(index)
        settled: dict[str, tuple[str, ...]] = {}
        portfolio_actions: dict[str, tuple[AppliedAction, ...]] = {}
        executions: dict[str, ExecutionReport] = {}

        for portfolio_id in self.portfolios().values():
            settled[portfolio_id] = self.engine.settle_due(portfolio_id=portfolio_id, now=as_of)
            portfolio_actions[portfolio_id] = self.applier.apply_to_portfolio(
                index, portfolio_id=portfolio_id
            )
            executions[portfolio_id] = self.engine.execute_due(
                portfolio_id=portfolio_id, index=index, now=as_of
            )

        return OpenedCycle(
            reference_actions=reference_actions,
            settled=settled,
            portfolio_actions=portfolio_actions,
            executions=executions,
        )

    def decide(self, *, cycle_index: int, snapshot_id: str, as_of: Instant) -> CycleResult:
        """Step 5. Delegated whole to the runner, which owns arm isolation."""
        return self.runner.run_cycle(cycle_index=cycle_index, snapshot_id=snapshot_id, as_of=as_of)

    def place(
        self,
        executions: Sequence[ArmExecution],
        *,
        index: RetrievalIndex,
        as_of: Instant,
    ) -> dict[str, tuple[Order, ...]]:
        """Step 6. Takes the decisions rather than producing them, so a replay
        can hand over the sealed ones."""
        books = self.portfolios()
        placed: dict[str, tuple[Order, ...]] = {}
        for execution in executions:
            portfolio_id = books[(execution.arm_id, execution.repetition)]
            placed[portfolio_id] = self.engine.place_orders(
                execution.outcome.trade_intents,
                portfolio_id=portfolio_id,
                bundle_id=execution.bundle_id,
                index=index,
                decided_at=as_of,
            )
        return placed

    # -- all six steps -------------------------------------------------------

    def run(self, *, cycle_index: int, snapshot_id: str, as_of: Instant) -> CycleReport:
        """One cycle, end to end."""
        index = self.runner.builder.load_index(snapshot_id)
        opened = self.open_cycle(index, as_of=as_of)
        cycle = self.decide(cycle_index=cycle_index, snapshot_id=snapshot_id, as_of=as_of)
        placed = self.place(cycle.executions, index=index, as_of=as_of)
        return CycleReport(opened=opened, cycle=cycle, placed=placed)
