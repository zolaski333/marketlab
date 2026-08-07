"""Where an arm's granted material comes from (§13, §18, §19).

This module defines the one legitimate place in the platform where code both
knows which arm is running *and* decides what that arm receives. Everything
downstream of it — :class:`~marketlab.agents.decision.ConditionContext`, the
model request, the model itself — sees only the resulting text, never the
identity of the condition that produced it. Concentrating the knowledge here
is what makes the condition-blindness guarantee auditable: there is exactly
one function to read.

A provider returns **text or nothing**, not a
:class:`~marketlab.agents.decision.ConditionContext`. That is deliberate. If a
provider built the whole context object it could also vary the per-decision
turn budget between arms, which would confound the comparison with an
allowance difference having nothing to do with memory or reflection. Returning
only the material leaves the runner to impose the same budget on every arm by
construction rather than by review.

Phase 1 ships :class:`NullMaterialsProvider` only. The genuine memory and
reflection providers — and the matched placebo generators the ``B'``/``C'``
arms need — are task 11; see ``docs/ROADMAP.md``. A null provider is not a
stub that fakes success: it grants nothing to every arm, which makes every
arm identical, which is a property worth asserting on its own (an A/A test
that would catch any leakage path the field-level guards missed).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from marketlab.core.instants import Instant
from marketlab.experiments.arms import ArmSpec

__all__ = ["ConditionMaterialsProvider", "NullMaterialsProvider"]


@runtime_checkable
class ConditionMaterialsProvider(Protocol):
    """Produces the text one arm is granted for one decision."""

    def materials_for(
        self,
        arm: ArmSpec,
        *,
        cycle_id: str,
        as_of: Instant,
        repetition: int,
    ) -> str | None:
        """Return the material to inject, or ``None`` to grant nothing.

        ``repetition`` is a parameter because repetitions must be independent
        (§30.3): a provider that draws on sampled or generated material has to
        be able to vary it per repetition rather than silently reusing one
        draw, which would understate within-condition variance.

        Implementations must be **pure with respect to the cutoff**: they may
        read only material whose ``first_seen_at`` does not exceed ``as_of``.
        Nothing in this signature can enforce that — the memory subsystem
        (task 11) carries its own point-in-time guard, the same way the
        retrieval path does.
        """
        ...


class NullMaterialsProvider:
    """Grants nothing to any arm.

    The honest Phase 1 default: the memory and reflection subsystems do not
    exist yet, so no provider can truthfully produce their output. Under this
    provider every arm is handed the identical (empty) context, so any
    difference observed between arms is a defect, not an effect — which is
    exactly what ``tests/integration/test_multi_arm_wiring.py`` asserts.
    """

    __slots__ = ()

    def materials_for(
        self,
        arm: ArmSpec,
        *,
        cycle_id: str,
        as_of: Instant,
        repetition: int,
    ) -> str | None:
        return None
