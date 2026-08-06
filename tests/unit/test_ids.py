"""Tests for deterministic identifier derivation (§16.7, §30.6)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from marketlab.core.ids import IdKind, derive_id, short_id


@given(st.text(max_size=20), st.integers())
def test_derivation_is_stable(snapshot: str, repetition: int) -> None:
    """The same inputs always produce the same id — this is what makes an
    interrupted cycle resumable without duplicating orders."""
    first = derive_id(IdKind.ORDER, snapshot=snapshot, repetition=repetition)
    second = derive_id(IdKind.ORDER, snapshot=snapshot, repetition=repetition)
    assert first == second


def test_keyword_order_does_not_matter() -> None:
    assert derive_id(IdKind.FILL, a="1", b="2") == derive_id(IdKind.FILL, b="2", a="1")


def test_namespaces_separate_identical_parts() -> None:
    """A decision bundle and a panel bundle built from the same parts must not
    collide (§15.4 keeps them strictly distinct objects)."""
    parts = {"snapshot": "s1", "arm": "C", "repetition": 1}
    assert derive_id(IdKind.DECISION_BUNDLE, **parts) != derive_id(IdKind.PANEL_BUNDLE, **parts)


def test_every_discriminating_part_changes_the_id() -> None:
    """Regression guard for the defect this design prevents.

    A previous implementation derived bundle ids from (snapshot, condition)
    while omitting the repetition, which would have collapsed every repetition
    of an arm onto one bundle the moment repetitions were enabled.
    """
    base = derive_id(IdKind.DECISION_BUNDLE, snapshot="s1", arm="C", repetition=1)
    other_repetition = derive_id(IdKind.DECISION_BUNDLE, snapshot="s1", arm="C", repetition=2)
    other_arm = derive_id(IdKind.DECISION_BUNDLE, snapshot="s1", arm="B", repetition=1)
    other_snapshot = derive_id(IdKind.DECISION_BUNDLE, snapshot="s2", arm="C", repetition=1)
    assert len({base, other_repetition, other_arm, other_snapshot}) == 4


def test_ids_are_full_length_sha256_hex() -> None:
    identifier = derive_id(IdKind.FORECAST, x="1")
    assert len(identifier) == 64
    assert all(c in "0123456789abcdef" for c in identifier)


def test_partless_derivation_is_refused() -> None:
    """A namespace-only id would be identical for every entity of that kind."""
    with pytest.raises(ValueError, match="at least one discriminating part"):
        derive_id(IdKind.ORDER)


def test_short_id_is_display_only() -> None:
    identifier = derive_id(IdKind.ORDER, x="1")
    assert short_id(identifier) == identifier[:12]
    assert len(short_id(identifier, 8)) == 8
