"""Turning an arm's grants into the text it actually receives (§13, §18, §19).

This closes the gap ``docs/ROADMAP.md`` has been recording since task 9: the
conditions were declared, ordered, isolated and sealed, but every one of them
received nothing, so a run produced six genuinely indistinguishable arms. This
is where they stop being indistinguishable.

Reading and writing are separate objects on purpose
----------------------------------------------------
:class:`GrantedMaterialsProvider` only reads. It is called *before* a decision,
inside the cycle runner, and a getter that also wrote would make "what did this
condition know" depend on when it was asked.

:class:`MemoryRecorder` only writes, and the driver calls it *after* a cycle
has been sealed. Keeping the write outside the provider is also what makes the
strict recall cutoff meaningful: an episode cannot be recorded and then
recalled within one decision, because nothing in the decision path can record.

Nothing here reads another condition's scope
--------------------------------------------
Every method keys on ``memory_scope_id(run_id, arm, repetition)``. That is the
same isolation the ledger gets from ``portfolio_id``, and it is asserted
directly rather than assumed: ``tests/unit/test_memory_materials.py`` gives two
arms different histories and checks neither can see the other's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from marketlab.agents.decision import DecisionOutcome
from marketlab.core.instants import Instant
from marketlab.core.money import Money
from marketlab.experiments.arms import ArmId, ArmSpec, MaterialGrant
from marketlab.memory.rendering import (
    placebo_episodes,
    placebo_rules,
    render_memory,
    render_reflection,
)
from marketlab.memory.store import DEFAULT_RECALL_LIMIT, Episode, MemoryStore, memory_scope_id
from marketlab.reflection.engine import (
    DEFAULT_REFLECTION_INTERVAL,
    Reflection,
    ReflectionEngine,
)

__all__ = ["GrantedMaterialsProvider", "MemoryRecorder"]

_PLACEBO_RULE_COUNT = 3
"""Rules a placebo reflection carries.

Fixed rather than matched to the genuine reflection's rule count, because
matching it would require reading the genuine reflection — which for arm C'
would mean reading arm C's. Three is the middle of what
:func:`marketlab.reflection.engine.derive_rules` typically produces; the
resulting length difference is bounded and measured in
``tests/unit/test_memory_materials.py``.
"""


@dataclass(slots=True)
class GrantedMaterialsProvider:
    """Supplies each condition exactly what its arm grants — and nothing else."""

    run_id: str
    memory: MemoryStore
    reflection: ReflectionEngine
    recall_limit: int = DEFAULT_RECALL_LIMIT

    def materials_for(
        self,
        arm: ArmSpec,
        *,
        cycle_id: str,
        as_of: Instant,
        repetition: int,
    ) -> str | None:
        scope_id = memory_scope_id(self.run_id, str(arm.arm_id), repetition)
        sections = [
            self._memory_section(arm, scope_id, as_of),
            self._reflection_section(arm, scope_id, as_of),
        ]
        rendered = [section for section in sections if section]
        return "\n\n".join(rendered) if rendered else None

    def _memory_section(self, arm: ArmSpec, scope_id: str, as_of: Instant) -> str | None:
        if arm.memory is MaterialGrant.NONE:
            return None
        if arm.memory is MaterialGrant.GENUINE:
            episodes = self.memory.recall(scope_id, before=as_of, limit=self.recall_limit)
            return render_memory(episodes) if episodes else None

        # PLACEBO: sized from this condition's *own* record, and only from its
        # shape - episode_shapes reads two integer columns and never the
        # payload, so no genuine content can reach a placebo.
        shape = self.memory.episode_shapes(scope_id, before=as_of, limit=self.recall_limit)
        if not shape:
            return None
        return render_memory(placebo_episodes(scope_id=scope_id, as_of=as_of, shape=shape))

    def _reflection_section(self, arm: ArmSpec, scope_id: str, as_of: Instant) -> str | None:
        if arm.reflection is MaterialGrant.NONE:
            return None
        if arm.reflection is MaterialGrant.GENUINE:
            reflection = self.reflection.latest(scope_id, before=as_of)
            return render_reflection(reflection) if reflection is not None else None

        if self.memory.episode_count(scope_id, before=as_of) == 0:
            return None
        return render_reflection(
            placebo_rules(scope_id=scope_id, as_of=as_of, count=_PLACEBO_RULE_COUNT)
        )


@dataclass(slots=True)
class MemoryRecorder:
    """Writes what each condition did, and reflects on a fixed cadence."""

    run_id: str
    memory: MemoryStore
    reflection: ReflectionEngine
    reflection_interval: int = DEFAULT_REFLECTION_INTERVAL
    recall_limit: int = DEFAULT_RECALL_LIMIT
    reflected_scopes: set[str] = field(default_factory=set)

    def record(
        self,
        *,
        arm_id: ArmId,
        repetition: int,
        cycle_id: str,
        bundle_id: str,
        as_of: Instant,
        outcome: DecisionOutcome,
        equity: Money | None = None,
    ) -> Episode:
        """Remember one sealed decision."""
        return self.memory.record(
            memory_scope_id(self.run_id, str(arm_id), repetition),
            cycle_id=cycle_id,
            bundle_id=bundle_id,
            as_of=as_of,
            outcome=outcome,
            equity=equity,
        )

    def maybe_reflect(
        self, *, arm_id: ArmId, repetition: int, as_of: Instant, cycle_index: int
    ) -> Reflection | None:
        """Reflect if this cycle is a reflection cycle.

        Runs for every arm, including those whose condition grants no
        reflection. That is deliberate: producing the artefact for everyone and
        *withholding* it from arms A and B keeps the difference between
        conditions to what they are shown, rather than to how much work was
        done on their behalf. It also means enabling reflection for an arm
        later does not silently change what the platform computed.
        """
        if self.reflection_interval <= 0:
            return None
        if (cycle_index + 1) % self.reflection_interval != 0:
            return None
        scope_id = memory_scope_id(self.run_id, str(arm_id), repetition)
        episodes = self.memory.recall(scope_id, before=as_of, limit=self.recall_limit)
        reflection = self.reflection.reflect(scope_id, episodes, as_of=as_of)
        if reflection is not None:
            self.reflected_scopes.add(scope_id)
        return reflection
