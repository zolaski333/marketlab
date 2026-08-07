"""The experimental conditions: arms A/B/C/D and the placebos B'/C' (§13).

What an arm *is*, here
----------------------
An arm is not a label with special-cased behaviour somewhere in the code. It
is a pair of **grants** — what the condition entitles a decision to be given
about memory, and about strategic reflection — and nothing else. Everything
downstream reads those two grants; nothing downstream branches on the arm's
name. That is deliberate: the moment a code path says ``if arm_id == "C"``,
the study is measuring that branch rather than measuring the effect of what
condition C grants.

Why placebos exist at all
-------------------------
If arm B (genuine memory) beats arm A (nothing), two explanations survive:
the *content* of the memory helped, or merely being handed several hundred
extra words of plausible context helped. The second is not the hypothesis
under test. B' grants placebo material — same shape, same rough size, no
genuine retrieved history — so B versus B' isolates the content, and B'
versus A isolates the mere presence of material.

Matching, structurally
----------------------
A placebo is only a placebo if it differs from its counterpart in exactly one
coordinate. Encoding each arm as a
``(memory_grant, reflection_grant)`` pair makes that checkable rather than
asserted: :func:`is_matched_placebo` compares the two specs coordinate by
coordinate, and ``tests/unit/test_arms.py`` runs it over every declared
placebo. A future arm added with a mismatched shape fails that test instead
of quietly entering the study.

Producing the material is *not* this module's job — see
:mod:`marketlab.experiments.context` for the provider protocol and
task 11 for the real memory/reflection implementations.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

__all__ = [
    "ARMS",
    "DEFAULT_ARMS",
    "ArmId",
    "ArmSpec",
    "Channel",
    "MaterialGrant",
    "is_matched_placebo",
    "spec_for",
]


class Channel(StrEnum):
    """The two things a condition may grant, defined so that they are
    genuinely separable rather than nested.

    ``MEMORY``
        *Raw episodic recall.* Past decisions, what happened to them, and the
        evidence they cited — retrieved under the same point-in-time rule as
        everything else. What the agent gets is history.

    ``REFLECTION``
        *Distilled strategy.* Rules and hypotheses produced periodically by a
        process that reads the run's own record. What the agent gets is a
        conclusion, not the material it was drawn from.

    This is what makes arm D (reflection without memory) coherent rather than
    paradoxical: the *reflection process* reads history, the agent under arm D
    does not. D receives the distillate with no ability to recall the
    underlying episodes — which is precisely the cell that separates "having
    been told what works" from "being able to look up what happened".
    """

    MEMORY = "MEMORY"
    REFLECTION = "REFLECTION"


class MaterialGrant(StrEnum):
    """What a condition is entitled to receive for one material channel."""

    NONE = "NONE"
    """Nothing is injected for this channel."""

    GENUINE = "GENUINE"
    """The real thing — retrieved memory, or an actual reflection."""

    PLACEBO = "PLACEBO"
    """Inert material matched in shape and size to the genuine grant (§13)."""


class ArmId(StrEnum):
    """Stable identifier for one experimental condition.

    The values are ASCII: they become database column values, event routing
    keys and file names. The prime notation used in the specification lives in
    :attr:`ArmSpec.label` instead, where it is display text and nothing else.
    """

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    B_PRIME = "B_PRIME"
    C_PRIME = "C_PRIME"


@dataclass(frozen=True, slots=True)
class ArmSpec:
    """One condition, defined entirely by what it grants."""

    arm_id: ArmId
    label: str
    """Display form, including the prime notation (``B'``). Never a key."""

    memory: MaterialGrant
    reflection: MaterialGrant
    placebo_of: ArmId | None
    """The genuine arm this one is the matched placebo for, if any."""

    rationale: str
    """What comparing this arm against its neighbours is supposed to isolate."""

    @property
    def is_placebo(self) -> bool:
        return MaterialGrant.PLACEBO in (self.memory, self.reflection)

    @property
    def grants_anything(self) -> bool:
        return (self.memory, self.reflection) != (MaterialGrant.NONE, MaterialGrant.NONE)


_ARMS: Final[dict[ArmId, ArmSpec]] = {
    ArmId.A: ArmSpec(
        arm_id=ArmId.A,
        label="A",
        memory=MaterialGrant.NONE,
        reflection=MaterialGrant.NONE,
        placebo_of=None,
        rationale=(
            "Control. Decides from the frozen snapshot alone, with no memory of "
            "previous cycles and no reflection. Every other arm is read against this."
        ),
    ),
    ArmId.B: ArmSpec(
        arm_id=ArmId.B,
        label="B",
        memory=MaterialGrant.GENUINE,
        reflection=MaterialGrant.NONE,
        placebo_of=None,
        rationale="Isolates persistent memory: B versus A is the memory main effect.",
    ),
    ArmId.C: ArmSpec(
        arm_id=ArmId.C,
        label="C",
        memory=MaterialGrant.GENUINE,
        reflection=MaterialGrant.GENUINE,
        placebo_of=None,
        rationale=(
            "Memory plus periodic strategic reflection. C versus B is what "
            "reflection adds on top of memory."
        ),
    ),
    ArmId.D: ArmSpec(
        arm_id=ArmId.D,
        label="D",
        memory=MaterialGrant.NONE,
        reflection=MaterialGrant.GENUINE,
        placebo_of=None,
        rationale=(
            "Reflection without persistent memory: the agent is handed the "
            "distilled strategy but cannot recall the episodes it was drawn from. "
            "Completes the 2x2, without which a C-versus-A difference cannot be "
            "attributed to either factor. See Channel for why this is coherent."
        ),
    ),
    ArmId.B_PRIME: ArmSpec(
        arm_id=ArmId.B_PRIME,
        label="B'",
        memory=MaterialGrant.PLACEBO,
        reflection=MaterialGrant.NONE,
        placebo_of=ArmId.B,
        rationale=(
            "Matched placebo for B. B versus B' isolates the *content* of memory "
            "from the mere presence of injected context."
        ),
    ),
    ArmId.C_PRIME: ArmSpec(
        arm_id=ArmId.C_PRIME,
        label="C'",
        memory=MaterialGrant.PLACEBO,
        reflection=MaterialGrant.PLACEBO,
        placebo_of=ArmId.C,
        rationale=(
            "Matched placebo for C. C versus C' isolates the content of memory and "
            "reflection together from the volume of text they occupy."
        ),
    ),
}

ARMS: Final[Mapping[ArmId, ArmSpec]] = MappingProxyType(_ARMS)
"""Every declared condition. A read-only view: an arm table mutated at runtime
would make two cycles of the same run silently different studies."""

DEFAULT_ARMS: Final[tuple[ArmId, ...]] = (
    ArmId.A,
    ArmId.B,
    ArmId.C,
    ArmId.D,
    ArmId.B_PRIME,
    ArmId.C_PRIME,
)
"""Canonical declaration order — *not* execution order, which is randomised or
counterbalanced per cycle (see :mod:`marketlab.experiments.ordering`)."""


def spec_for(arm_id: ArmId) -> ArmSpec:
    """Return the specification of one arm."""
    return ARMS[arm_id]


def is_matched_placebo(placebo: ArmSpec, genuine: ArmSpec) -> bool:
    """Is ``placebo`` a correctly matched placebo for ``genuine``?

    True when the two differ in exactly the way a placebo should: every
    channel the genuine arm leaves empty is empty in the placebo too, every
    channel it grants genuinely is granted as placebo, and at least one
    channel actually is a placebo. A placebo that granted an extra channel —
    or that quietly withheld one — would not be a control for its
    counterpart; it would be a seventh condition nobody declared.
    """
    if placebo.placebo_of is not genuine.arm_id:
        return False
    channels = (
        (placebo.memory, genuine.memory),
        (placebo.reflection, genuine.reflection),
    )
    substituted = False
    for placebo_grant, genuine_grant in channels:
        if genuine_grant is MaterialGrant.NONE:
            if placebo_grant is not MaterialGrant.NONE:
                return False
        elif placebo_grant is MaterialGrant.PLACEBO:
            substituted = True
        else:
            return False
    return substituted
