"""Tests for the point-in-time cutoff (§6.3, INV-P5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from marketlab.core.cutoff import Cutoff
from marketlab.core.instants import instant_from_datetime

EARLIER = instant_from_datetime(datetime(2026, 8, 1, 12, 0, tzinfo=UTC))
LATER = instant_from_datetime(datetime(2026, 8, 1, 13, 0, tzinfo=UTC))


def test_data_seen_before_the_cutoff_is_allowed() -> None:
    cutoff = Cutoff(as_of=LATER)
    assert cutoff.allows(EARLIER)


def test_data_seen_exactly_at_the_cutoff_is_allowed() -> None:
    """Equality counts as visible: seen no later than the cutoff is the rule."""
    cutoff = Cutoff(as_of=LATER)
    assert cutoff.allows(LATER)


def test_data_seen_after_the_cutoff_is_refused() -> None:
    cutoff = Cutoff(as_of=EARLIER)
    assert not cutoff.allows(LATER)


def test_malformed_instant_is_refused_at_construction() -> None:
    """A cutoff that cannot be compared correctly must not exist at all."""
    with pytest.raises(ValueError, match="canonical instant"):
        Cutoff(as_of="not-an-instant")  # type: ignore[arg-type]


def test_snapshot_id_is_optional_provenance_only() -> None:
    cutoff = Cutoff(as_of=EARLIER, snapshot_id="snap-1")
    assert cutoff.snapshot_id == "snap-1"
    assert cutoff.allows(EARLIER)


def test_cutoff_without_snapshot_id_defaults_to_none() -> None:
    assert Cutoff(as_of=EARLIER).snapshot_id is None


def test_cutoffs_with_equal_fields_are_equal() -> None:
    assert Cutoff(as_of=EARLIER, snapshot_id="s") == Cutoff(as_of=EARLIER, snapshot_id="s")
    assert Cutoff(as_of=EARLIER) != Cutoff(as_of=LATER)
