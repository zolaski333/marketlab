"""Tests for the experimental condition taxonomy (§13)."""

from __future__ import annotations

import pytest

from marketlab.experiments.arms import (
    ARMS,
    DEFAULT_ARMS,
    ArmId,
    ArmSpec,
    MaterialGrant,
    is_matched_placebo,
    spec_for,
)


def test_every_declared_arm_is_in_the_default_set() -> None:
    assert set(DEFAULT_ARMS) == set(ARMS)


def test_each_spec_is_keyed_by_its_own_id() -> None:
    # A mismatch here would make spec_for(X) silently return arm Y's grants.
    for arm_id, spec in ARMS.items():
        assert spec.arm_id is arm_id


def test_the_control_arm_grants_nothing() -> None:
    control = spec_for(ArmId.A)
    assert control.memory is MaterialGrant.NONE
    assert control.reflection is MaterialGrant.NONE
    assert not control.grants_anything
    assert not control.is_placebo


def test_the_four_genuine_arms_form_a_complete_two_by_two() -> None:
    """Memory and reflection must be crossed, not nested: without the
    reflection-only cell, a C-versus-A difference cannot be attributed to
    either factor."""
    genuine = {(spec.memory, spec.reflection) for spec in ARMS.values() if not spec.is_placebo}
    assert genuine == {
        (MaterialGrant.NONE, MaterialGrant.NONE),
        (MaterialGrant.GENUINE, MaterialGrant.NONE),
        (MaterialGrant.GENUINE, MaterialGrant.GENUINE),
        (MaterialGrant.NONE, MaterialGrant.GENUINE),
    }


@pytest.mark.parametrize("arm_id", [ArmId.B_PRIME, ArmId.C_PRIME])
def test_every_placebo_is_correctly_matched_to_its_counterpart(arm_id: ArmId) -> None:
    placebo = spec_for(arm_id)
    assert placebo.placebo_of is not None
    assert is_matched_placebo(placebo, spec_for(placebo.placebo_of))


def test_a_placebo_that_grants_an_extra_channel_is_not_matched() -> None:
    # The failure mode this guards against: someone adds a placebo for B and
    # hands it reflection material too, making it a seventh condition rather
    # than a control for B.
    over_granting = ArmSpec(
        arm_id=ArmId.B_PRIME,
        label="B'",
        memory=MaterialGrant.PLACEBO,
        reflection=MaterialGrant.PLACEBO,
        placebo_of=ArmId.B,
        rationale="deliberately wrong",
    )
    assert not is_matched_placebo(over_granting, spec_for(ArmId.B))


def test_a_placebo_that_withholds_a_channel_is_not_matched() -> None:
    under_granting = ArmSpec(
        arm_id=ArmId.C_PRIME,
        label="C'",
        memory=MaterialGrant.PLACEBO,
        reflection=MaterialGrant.NONE,
        placebo_of=ArmId.C,
        rationale="deliberately wrong",
    )
    assert not is_matched_placebo(under_granting, spec_for(ArmId.C))


def test_a_genuine_arm_is_not_a_placebo_for_anything() -> None:
    fake = ArmSpec(
        arm_id=ArmId.B_PRIME,
        label="B'",
        memory=MaterialGrant.GENUINE,
        reflection=MaterialGrant.NONE,
        placebo_of=ArmId.B,
        rationale="deliberately wrong",
    )
    assert not is_matched_placebo(fake, spec_for(ArmId.B))


def test_matching_is_rejected_when_the_counterpart_is_the_wrong_arm() -> None:
    assert not is_matched_placebo(spec_for(ArmId.B_PRIME), spec_for(ArmId.C))


def test_arm_ids_are_ascii_so_they_are_safe_as_keys() -> None:
    # The prime notation belongs in the display label; identifiers become
    # column values, event routing keys and file names.
    for arm_id in ARMS:
        assert str(arm_id).isascii()
        assert "'" not in str(arm_id)
    assert spec_for(ArmId.B_PRIME).label == "B'"


def test_the_arm_table_cannot_be_mutated_at_runtime() -> None:
    with pytest.raises(TypeError):
        ARMS[ArmId.A] = spec_for(ArmId.C)  # type: ignore[index]
