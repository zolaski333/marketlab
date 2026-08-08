"""Tests for deterministic total-return forecast resolution (§20).

:class:`MarketRecord` is a plain value, so every case here builds the world it
needs by hand — a split, a dividend, a delisting, a hole in the data — rather
than hoping the synthetic fixture happens to contain one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from marketlab.core.failures import ConfigurationError
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.evaluation.resolution import (
    CASH_DIVIDEND,
    STOCK_SPLIT,
    CorporateEvent,
    MarketRecord,
    Resolution,
    ResolutionStatus,
    SessionGrid,
    resolve_forecast,
)
from marketlab.instruments.types import InstrumentStatus

ALPHA = "id-alpha"
BETA = "id-beta"


def at(day: int) -> Instant:
    return instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC) + timedelta(days=day))


GRID = SessionGrid(tuple(at(day) for day in range(6)))


def _record(
    closes: dict[tuple[str, Instant], str] | None = None,
    *,
    events: tuple[CorporateEvent, ...] = (),
    statuses: dict[tuple[str, Instant], InstrumentStatus] | None = None,
    grid: SessionGrid = GRID,
) -> MarketRecord:
    """A world in which ALPHA closes at 100 every session unless overridden."""
    default = {(ALPHA, moment): Decimal("100") for moment in grid.instants}
    default.update({key: Decimal(value) for key, value in (closes or {}).items()})
    default_statuses = {(ALPHA, moment): InstrumentStatus.ACTIVE for moment in grid.instants}
    default_statuses.update(statuses or {})
    return MarketRecord(
        grid=grid, closes=default, statuses=default_statuses, corporate_events=events
    )


def _resolve(
    record: MarketRecord, *, horizon: int = 2, anchor: Instant | None = None
) -> Resolution:
    return resolve_forecast(
        record,
        instrument_id=ALPHA,
        anchor_at=anchor if anchor is not None else at(0),
        horizon_sessions=horizon,
    )


# ---------------------------------------------------------------------------
# The session grid
# ---------------------------------------------------------------------------


def test_the_grid_advances_by_whole_sessions() -> None:
    assert GRID.advance(at(1), 3) == at(4)


def test_advancing_past_the_end_of_the_grid_is_not_an_error() -> None:
    """It is the ordinary case for a forecast made near the end of a run: the
    horizon simply has not elapsed yet."""
    assert GRID.advance(at(4), 3) is None


def test_an_anchor_that_is_not_on_the_grid_is_a_wiring_error() -> None:
    with pytest.raises(ConfigurationError, match="not one of this run"):
        GRID.advance(at(99), 1)


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def test_a_rise_resolves_up() -> None:
    resolution = _resolve(_record({(ALPHA, at(2)): "110"}))
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.outcome is not None
    assert resolution.outcome.outcome_up is True
    assert resolution.outcome.total_return == Decimal("0.1")


def test_a_fall_resolves_down() -> None:
    resolution = _resolve(_record({(ALPHA, at(2)): "90"}))
    assert resolution.outcome is not None
    assert resolution.outcome.outcome_up is False


def test_an_unchanged_close_is_scored_as_not_up() -> None:
    """The pre-registered tie rule (§20.3): a rise is strictly positive."""
    resolution = _resolve(_record())
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.outcome is not None
    assert resolution.outcome.total_return == 0
    assert resolution.outcome.outcome_up is False


def test_the_horizon_is_counted_on_the_grid_not_in_calendar_days() -> None:
    record = _record({(ALPHA, at(1)): "200", (ALPHA, at(2)): "50"})
    assert _resolve(record, horizon=1).outcome.total_return == 1  # type: ignore[union-attr]
    assert _resolve(record, horizon=2).outcome.total_return == Decimal("-0.5")  # type: ignore[union-attr]


def test_a_forecast_whose_horizon_has_not_elapsed_is_pending() -> None:
    resolution = _resolve(_record(), horizon=3, anchor=at(4))
    assert resolution.status is ResolutionStatus.PENDING
    assert resolution.target_at is None
    assert resolution.is_terminal is False


def test_a_zero_horizon_is_rejected_rather_than_resolved() -> None:
    with pytest.raises(ConfigurationError, match="not a forecast"):
        _resolve(_record(), horizon=0)


# ---------------------------------------------------------------------------
# Total return, which is the whole point
# ---------------------------------------------------------------------------


def test_a_split_does_not_look_like_a_crash() -> None:
    """The defect this module exists to prevent.

    ``EQ_US_ALPHA``'s raw quote genuinely halves on its split session, so a
    comparison of two raw closes would score a 2-for-1 split as a 50% loss for
    every arm that forecast it — a scientific error that would look exactly
    like every arm being bad at forecasting.
    """
    record = _record(
        {(ALPHA, at(2)): "50"},
        events=(CorporateEvent(at(1), ALPHA, STOCK_SPLIT, Decimal("2")),),
    )
    resolution = _resolve(record)
    assert resolution.outcome is not None
    assert resolution.outcome.split_factor == 2
    assert resolution.outcome.total_return == 0
    assert resolution.outcome.outcome_up is False


def test_a_dividend_counts_towards_the_return() -> None:
    record = _record(
        {(ALPHA, at(2)): "98"},
        events=(CorporateEvent(at(1), ALPHA, CASH_DIVIDEND, Decimal("3")),),
    )
    resolution = _resolve(record)
    assert resolution.outcome is not None
    assert resolution.outcome.dividends == 3
    assert resolution.outcome.total_return == Decimal("0.01")
    assert resolution.outcome.outcome_up is True


def test_a_dividend_going_ex_on_the_anchor_session_is_not_earned() -> None:
    """The same entitlement rule the ledger applies: a holder who buys at the
    ex-date close has already missed it. Counting it here would credit the
    forecast with cash the book never received."""
    record = _record(
        {(ALPHA, at(2)): "98"},
        events=(CorporateEvent(at(0), ALPHA, CASH_DIVIDEND, Decimal("3")),),
    )
    resolution = _resolve(record)
    assert resolution.outcome is not None
    assert resolution.outcome.dividends == 0
    assert resolution.outcome.outcome_up is False


def test_a_dividend_on_the_target_session_is_earned() -> None:
    record = _record(
        {(ALPHA, at(2)): "98"},
        events=(CorporateEvent(at(2), ALPHA, CASH_DIVIDEND, Decimal("3")),),
    )
    assert _resolve(record).outcome.dividends == 3  # type: ignore[union-attr]


def test_a_dividend_after_a_split_is_paid_on_the_split_adjusted_units() -> None:
    """One unit at the anchor is two units by the time the dividend goes ex."""
    record = _record(
        {(ALPHA, at(3)): "50"},
        events=(
            CorporateEvent(at(1), ALPHA, STOCK_SPLIT, Decimal("2")),
            CorporateEvent(at(2), ALPHA, CASH_DIVIDEND, Decimal("1")),
        ),
    )
    resolution = _resolve(record, horizon=3)
    assert resolution.outcome is not None
    assert resolution.outcome.dividends == 2
    assert resolution.outcome.total_return == Decimal("0.02")


def test_another_instruments_corporate_action_is_ignored() -> None:
    record = _record(
        {(ALPHA, at(2)): "50"},
        events=(CorporateEvent(at(1), BETA, STOCK_SPLIT, Decimal("2")),),
    )
    assert _resolve(record).outcome.total_return == Decimal("-0.5")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# The four ways a forecast fails to resolve
# ---------------------------------------------------------------------------


def test_a_missing_target_price_is_unresolvable_never_assumed_flat() -> None:
    record = _record()
    holed = MarketRecord(
        grid=record.grid,
        closes={key: value for key, value in record.closes.items() if key != (ALPHA, at(2))},
        statuses=record.statuses,
        corporate_events=(),
    )
    resolution = _resolve(holed)
    assert resolution.status is ResolutionStatus.UNRESOLVABLE
    assert resolution.outcome is None
    assert "target" in resolution.detail


def test_a_missing_anchor_price_is_unresolvable() -> None:
    record = _record()
    holed = MarketRecord(
        grid=record.grid,
        closes={key: value for key, value in record.closes.items() if key != (ALPHA, at(0))},
        statuses=record.statuses,
        corporate_events=(),
    )
    assert _resolve(holed).status is ResolutionStatus.UNRESOLVABLE


def test_a_delisted_instrument_is_censored_not_merely_missing() -> None:
    """Right-censoring is a different fact from a data gap: the series ended,
    it did not break. The analysis counts them separately."""
    record = _record(statuses={(ALPHA, at(2)): InstrumentStatus.DELISTED})
    resolution = _resolve(record)
    assert resolution.status is ResolutionStatus.CENSORED_BY_DELISTING


def test_censoring_wins_over_a_missing_price_because_it_explains_it() -> None:
    record = _record(statuses={(ALPHA, at(2)): InstrumentStatus.DELISTED})
    holed = MarketRecord(
        grid=record.grid,
        closes={key: value for key, value in record.closes.items() if key != (ALPHA, at(2))},
        statuses=record.statuses,
        corporate_events=(),
    )
    assert _resolve(holed).status is ResolutionStatus.CENSORED_BY_DELISTING


def test_a_suspended_instrument_still_resolves_if_it_has_a_price() -> None:
    """A halt interrupts a series; it does not end it."""
    record = _record({(ALPHA, at(2)): "110"}, statuses={(ALPHA, at(2)): InstrumentStatus.SUSPENDED})
    assert _resolve(record).status is ResolutionStatus.RESOLVED


def test_a_non_positive_price_is_invalid_source_data() -> None:
    assert _resolve(_record({(ALPHA, at(2)): "0"})).status is ResolutionStatus.INVALID_SOURCE_DATA


def test_a_non_positive_split_ratio_is_invalid_source_data() -> None:
    record = _record(events=(CorporateEvent(at(1), ALPHA, STOCK_SPLIT, Decimal("0")),))
    assert _resolve(record).status is ResolutionStatus.INVALID_SOURCE_DATA


def test_every_terminal_status_carries_the_target_it_was_judged_against() -> None:
    """So a stored verdict can be re-checked without re-deriving the grid."""
    for record in (
        _record({(ALPHA, at(2)): "110"}),
        _record({(ALPHA, at(2)): "0"}),
        _record(statuses={(ALPHA, at(2)): InstrumentStatus.DELISTED}),
    ):
        resolution = _resolve(record)
        assert resolution.is_terminal
        assert resolution.target_at == at(2)
