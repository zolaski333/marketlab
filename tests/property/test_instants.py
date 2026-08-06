"""Property tests for canonical instants (§6.1).

The load-bearing property is the third one: the platform orders events, snapshot
members and ledger entries by their stored timestamp *as text*. If lexicographic
order ever diverges from chronological order, `ORDER BY` silently returns
history out of sequence — which would corrupt the audit chain and the
point-in-time cutoff without raising anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from marketlab.core.instants import (
    INSTANT_WIDTH,
    instant_from_datetime,
    is_instant,
    parse_instant,
    to_datetime,
)

# Timezone-aware datetimes across a wide range of offsets, so the UTC
# normalisation is exercised rather than assumed.
aware_datetimes = st.datetimes(
    min_value=datetime(1990, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.sampled_from(
        [
            UTC,
            timezone(timedelta(hours=9)),
            timezone(timedelta(hours=-5)),
            timezone(timedelta(hours=5, minutes=30)),
            timezone(timedelta(hours=-3, minutes=-30)),
            timezone(timedelta(hours=14)),
        ]
    ),
)


@given(aware_datetimes)
def test_round_trip_preserves_the_instant(value: datetime) -> None:
    """datetime -> instant -> datetime returns the same point in time."""
    restored = to_datetime(instant_from_datetime(value))
    assert restored == value.astimezone(UTC)


@given(aware_datetimes)
def test_canonical_form_is_fixed_width(value: datetime) -> None:
    """Every instant has identical length, which is what makes text order sound."""
    text = instant_from_datetime(value)
    assert len(text) == INSTANT_WIDTH
    assert text.endswith("Z")
    assert is_instant(text)


@given(aware_datetimes, aware_datetimes)
def test_lexicographic_order_matches_chronological_order(first: datetime, second: datetime) -> None:
    """Sorting instants as strings sorts them as times.

    This is the property that a variable-width ISO format violates: with
    microseconds omitted when zero, "…:00Z" sorts *after* "…:00.5Z" because
    'Z' (0x5A) exceeds '.' (0x2E).
    """
    left = instant_from_datetime(first)
    right = instant_from_datetime(second)
    assert (left < right) == (first.astimezone(UTC) < second.astimezone(UTC))
    assert (left == right) == (first.astimezone(UTC) == second.astimezone(UTC))


def test_the_variable_width_format_would_have_broken_ordering() -> None:
    """Regression guard documenting why the fixed-width format exists."""
    earlier = datetime(2026, 8, 1, 16, 0, 0, 0, tzinfo=UTC)
    later = datetime(2026, 8, 1, 16, 0, 0, 500_000, tzinfo=UTC)
    assert earlier < later

    # The naive spelling: isoformat() drops a zero microsecond field.
    naive_earlier = earlier.isoformat().replace("+00:00", "Z")
    naive_later = later.isoformat().replace("+00:00", "Z")
    assert naive_earlier > naive_later, "precondition: the old format mis-sorts"

    # The canonical spelling orders correctly.
    assert instant_from_datetime(earlier) < instant_from_datetime(later)


@given(st.datetimes(min_value=datetime(1990, 1, 1), max_value=datetime(2100, 1, 1)))
def test_naive_datetimes_are_rejected(value: datetime) -> None:
    """A datetime without a timezone has no defined position on the timeline."""
    with pytest.raises(ValueError, match="Naive datetime rejected"):
        instant_from_datetime(value)


@pytest.mark.parametrize(
    "text",
    [
        "2026-08-01T16:00:00Z",  # no microseconds
        "2026-08-01T16:00:00.5Z",  # short microseconds
        "2026-08-01T16:00:00+00:00",  # explicit UTC offset
        "2026-08-01T18:00:00+02:00",  # non-UTC offset
        "2026-08-01t16:00:00z",  # lowercase designator
    ],
)
def test_upstream_variants_normalise_to_canonical_form(text: str) -> None:
    """Provider timestamps arrive in many spellings; all normalise or raise."""
    normalised = parse_instant(text)
    assert is_instant(normalised)
    assert len(normalised) == INSTANT_WIDTH


@pytest.mark.parametrize(
    "text",
    [
        "2026-08-01T16:00:00",  # no timezone at all
        "2026-08-01",  # date only, no timezone
    ],
)
def test_timestamps_without_a_timezone_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match=r"without timezone|Naive"):
        parse_instant(text)


def test_unparseable_text_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unparseable instant"):
        parse_instant("not a timestamp")
