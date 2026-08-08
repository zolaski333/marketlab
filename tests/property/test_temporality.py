"""Property tests for point-in-time correctness (§6.2, §6.3, §20, INV-P5).

``test_instants.py`` establishes that stored timestamps order correctly.
This file is about what the platform *does* with that order: the cutoff that
decides what an agent may see, and the session grid that decides which future
price a forecast is judged against.

Both are places where an off-by-one is invisible in the output. A cutoff that
admitted evidence dated one microsecond late would leak the future into every
decision and change nothing an eye could catch; a grid that resolved a
5-session forecast against session 6 would shift every horizon in the study.
Examples are the wrong tool for that, because the failing case is precisely the
boundary nobody thought to write down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from marketlab.core.cutoff import Cutoff
from marketlab.core.failures import ConfigurationError
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.evaluation.resolution import (
    STOCK_SPLIT,
    CorporateEvent,
    MarketRecord,
    ResolutionStatus,
    SessionGrid,
    resolve_forecast,
)
from marketlab.instruments.types import InstrumentStatus

INSTRUMENT = "id-alpha"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

offsets = st.integers(min_value=0, max_value=200_000)
grid_sizes = st.integers(min_value=1, max_value=40)
horizons = st.integers(min_value=1, max_value=25)


def at(offset: int) -> Instant:
    """A distinct instant per offset, an hour apart so ordering is unambiguous."""
    return instant_from_datetime(EPOCH + timedelta(hours=offset))


@st.composite
def grids(draw: st.DrawFn) -> SessionGrid:
    size = draw(grid_sizes)
    return SessionGrid(tuple(at(offset) for offset in range(size)))


# ---------------------------------------------------------------------------
# The cutoff
# ---------------------------------------------------------------------------


@given(offsets, offsets)
def test_a_cutoff_admits_exactly_what_was_seen_at_or_before_it(
    seen: int, cutoff_offset: int
) -> None:
    """INV-P5, stated as the only thing it can mean: visibility is ``<=`` on
    the instant, with no tolerance and no rounding."""
    cutoff = Cutoff(as_of=at(cutoff_offset))
    assert cutoff.allows(at(seen)) is (seen <= cutoff_offset)


@given(offsets)
def test_a_cutoff_admits_its_own_instant(offset: int) -> None:
    """A decision taken at the close may use that close. If this became a
    strict comparison, every cycle would silently decide on stale data."""
    assert Cutoff(as_of=at(offset)).allows(at(offset))


@given(offsets, st.integers(min_value=1, max_value=10_000))
def test_a_later_cutoff_never_hides_what_an_earlier_one_showed(offset: int, gap: int) -> None:
    """Visibility is monotone in the cutoff. A study that lost access to a
    fact as time passed could not reconstruct its own earlier decisions."""
    earlier = Cutoff(as_of=at(offset))
    later = Cutoff(as_of=at(offset + gap))
    for seen in (offset, max(offset - 1, 0), offset + gap):
        assert not earlier.allows(at(seen)) or later.allows(at(seen))


# ---------------------------------------------------------------------------
# The session grid
# ---------------------------------------------------------------------------


@given(grids(), horizons, st.data())
def test_advancing_lands_on_a_later_grid_point_or_nowhere(
    grid: SessionGrid, horizon: int, data: st.DataObject
) -> None:
    position = data.draw(st.integers(min_value=0, max_value=len(grid.instants) - 1))
    anchor = grid.instants[position]
    target = grid.advance(anchor, horizon)
    if target is None:
        assert position + horizon >= len(grid.instants)
        return
    assert target > anchor
    assert target == grid.instants[position + horizon]


@given(grids(), horizons, st.data())
def test_advancing_by_a_larger_horizon_never_lands_earlier(
    grid: SessionGrid, horizon: int, data: st.DataObject
) -> None:
    position = data.draw(st.integers(min_value=0, max_value=len(grid.instants) - 1))
    anchor = grid.instants[position]
    near = grid.advance(anchor, horizon)
    far = grid.advance(anchor, horizon + 1)
    assume(far is not None)
    assert near is not None and far > near


@given(grids(), offsets)
def test_an_anchor_off_the_grid_is_always_refused(grid: SessionGrid, offset: int) -> None:
    assume(at(offset) not in grid.instants)
    with pytest.raises(ConfigurationError):
        grid.advance(at(offset), 1)


# ---------------------------------------------------------------------------
# Resolution never looks at the wrong instant
# ---------------------------------------------------------------------------


def _record(grid: SessionGrid, closes: dict[Instant, Decimal]) -> MarketRecord:
    return MarketRecord(
        grid=grid,
        closes={(INSTRUMENT, moment): price for moment, price in closes.items()},
        statuses={(INSTRUMENT, moment): InstrumentStatus.ACTIVE for moment in grid.instants},
        corporate_events=(),
    )


@given(grids(), horizons, st.data())
def test_a_resolved_forecast_is_always_judged_against_a_later_session(
    grid: SessionGrid, horizon: int, data: st.DataObject
) -> None:
    """The property the whole of §20 rests on: nothing is ever scored against
    a price from at or before the moment it was forecast."""
    position = data.draw(st.integers(min_value=0, max_value=len(grid.instants) - 1))
    anchor = grid.instants[position]
    closes = {moment: Decimal(100 + index) for index, moment in enumerate(grid.instants)}

    resolution = resolve_forecast(
        _record(grid, closes),
        instrument_id=INSTRUMENT,
        anchor_at=anchor,
        horizon_sessions=horizon,
    )
    if resolution.status is ResolutionStatus.PENDING:
        assert resolution.target_at is None
        return
    assert resolution.target_at is not None
    assert resolution.target_at > anchor


@given(grids(), horizons, st.data())
def test_a_forecast_is_pending_exactly_when_the_grid_has_not_reached_it(
    grid: SessionGrid, horizon: int, data: st.DataObject
) -> None:
    """PENDING and UNRESOLVABLE must never be confused: one means the horizon
    has not elapsed, the other that it has and the data is missing."""
    position = data.draw(st.integers(min_value=0, max_value=len(grid.instants) - 1))
    anchor = grid.instants[position]
    closes = {moment: Decimal("100") for moment in grid.instants}

    resolution = resolve_forecast(
        _record(grid, closes),
        instrument_id=INSTRUMENT,
        anchor_at=anchor,
        horizon_sessions=horizon,
    )
    elapsed = position + horizon < len(grid.instants)
    assert (resolution.status is ResolutionStatus.PENDING) is not elapsed


# ---------------------------------------------------------------------------
# The corporate-action interval
# ---------------------------------------------------------------------------


@given(grids(), st.data())
def test_the_corporate_action_interval_is_open_at_the_anchor_and_closed_at_the_target(
    grid: SessionGrid, data: st.DataObject
) -> None:
    """Half-open ``(anchor, target]``, matching the ledger's own entitlement
    rule. Either boundary slipping would credit a forecast with a dividend the
    book never received, or withhold one it did."""
    assume(len(grid.instants) >= 2)
    low = data.draw(st.integers(min_value=0, max_value=len(grid.instants) - 2))
    high = data.draw(st.integers(min_value=low + 1, max_value=len(grid.instants) - 1))
    anchor, target = grid.instants[low], grid.instants[high]

    events = tuple(
        CorporateEvent(moment, INSTRUMENT, STOCK_SPLIT, Decimal("2")) for moment in grid.instants
    )
    record = MarketRecord(grid=grid, closes={}, statuses={}, corporate_events=events)
    selected = record.events_between(INSTRUMENT, after=anchor, through=target)

    assert all(anchor < event.at <= target for event in selected)
    assert len(selected) == high - low
