"""Tests for market trading calendars (§6.4, §16.2, §30.7)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from marketlab.core.failures import ConfigurationError
from marketlab.core.instants import Instant, instant_from_datetime, to_datetime
from marketlab.instruments.calendars import (
    CalendarRegistry,
    TwentyFourSevenCalendar,
    WeekdaySessionCalendar,
)

NY = ZoneInfo("America/New_York")


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> Instant:
    return instant_from_datetime(datetime(year, month, day, hour, minute, tzinfo=UTC))


@pytest.fixture
def nyse_like() -> WeekdaySessionCalendar:
    return WeekdaySessionCalendar(
        code="SYNTH_US_EQUITY",
        version="v1",
        iana_tz="America/New_York",
        session_open=time(9, 30),
        session_close=time(16, 0),
        holidays=frozenset({date(2026, 12, 25)}),
        early_closes={date(2026, 11, 27): time(13, 0)},
    )


# -- construction validation -------------------------------------------------


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="Unknown IANA timezone"):
        WeekdaySessionCalendar(
            code="X",
            version="v1",
            iana_tz="Not/AZone",
            session_open=time(9),
            session_close=time(16),
        )


def test_open_after_close_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="must be before"):
        WeekdaySessionCalendar(
            code="X", version="v1", iana_tz="UTC", session_open=time(16), session_close=time(9)
        )


# -- is_open ------------------------------------------------------------


def test_regular_weekday_session_is_open(nyse_like: WeekdaySessionCalendar) -> None:
    # 2026-08-03 is a Monday. 14:30 UTC = 10:30 EDT, inside the session.
    assert nyse_like.is_open(at(2026, 8, 3, 14, 30))


def test_outside_session_hours_is_closed(nyse_like: WeekdaySessionCalendar) -> None:
    assert not nyse_like.is_open(at(2026, 8, 3, 2, 0))  # the middle of the local night


def test_weekend_is_closed(nyse_like: WeekdaySessionCalendar) -> None:
    assert not nyse_like.is_open(at(2026, 8, 1, 15, 0))  # Saturday
    assert not nyse_like.is_open(at(2026, 8, 2, 15, 0))  # Sunday


def test_configured_holiday_is_closed(nyse_like: WeekdaySessionCalendar) -> None:
    assert not nyse_like.is_open(at(2026, 12, 25, 15, 0))


def test_early_close_shortens_the_session(nyse_like: WeekdaySessionCalendar) -> None:
    # 2026-11-27 (a Friday) closes at 13:00 local (18:00 UTC in EST) instead of
    # the usual 16:00 local.
    assert nyse_like.is_open(at(2026, 11, 27, 17, 30))  # 12:30 local, before early close
    assert not nyse_like.is_open(at(2026, 11, 27, 18, 30))  # 13:30 local, after early close


# -- DST ------------------------------------------------------------------


def test_dst_shifts_the_utc_session_open_by_one_hour(nyse_like: WeekdaySessionCalendar) -> None:
    """§30.7: the same 09:30 local open lands at a different UTC instant in
    winter (EST, UTC-5) than in summer (EDT, UTC-4)."""
    winter_open = nyse_like.next_eligible_execution(at(2026, 1, 5, 0, 0))  # Monday, January
    summer_open = nyse_like.next_eligible_execution(at(2026, 7, 6, 0, 0))  # Monday, July

    assert to_datetime(winter_open).hour == 14  # 09:30 EST -> 14:30 UTC
    assert to_datetime(summer_open).hour == 13  # 09:30 EDT -> 13:30 UTC


# -- next_eligible_execution (§16.2) -------------------------------------


def test_next_eligible_execution_is_always_strictly_after_the_decision(
    nyse_like: WeekdaySessionCalendar,
) -> None:
    decision = at(2026, 8, 3, 14, 30)  # during a session
    execution = nyse_like.next_eligible_execution(decision)
    assert execution > decision


def test_mid_session_decision_executes_at_the_next_sessions_open(
    nyse_like: WeekdaySessionCalendar,
) -> None:
    """A decision made during today's session executes at TOMORROW's open, not
    later today — §16.2's 'first eligible window strictly after decision'."""
    decision = at(2026, 8, 3, 14, 30)  # Monday, during the session
    execution = nyse_like.next_eligible_execution(decision)
    assert to_datetime(execution).astimezone(NY).date() == date(2026, 8, 4)


def test_next_eligible_execution_skips_a_weekend(nyse_like: WeekdaySessionCalendar) -> None:
    after_friday_close = at(2026, 8, 7, 21, 0)
    execution = nyse_like.next_eligible_execution(after_friday_close)
    assert to_datetime(execution).astimezone(NY).date() == date(2026, 8, 10)  # the following Monday


def test_next_eligible_execution_skips_a_holiday(nyse_like: WeekdaySessionCalendar) -> None:
    before_holiday = at(2026, 12, 24, 21, 0)  # Thursday, after close
    execution = nyse_like.next_eligible_execution(before_holiday)
    # Dec 25 (holiday) and the following weekend are both skipped.
    assert to_datetime(execution).astimezone(NY).date() == date(2026, 12, 28)


def test_no_session_within_the_search_window_raises() -> None:
    always_closed = WeekdaySessionCalendar(
        code="ALWAYS_CLOSED",
        version="v1",
        iana_tz="UTC",
        session_open=time(9, 0),
        session_close=time(17, 0),
        holidays=frozenset(date(2026, 1, 1) + timedelta(days=i) for i in range(20)),
    )
    with pytest.raises(ConfigurationError, match="No trading session found"):
        always_closed.next_eligible_execution(at(2026, 1, 1, 0, 0))


# -- next_session_close ----------------------------------------------------


def test_next_session_close_is_strictly_after_and_same_day_as_open(
    nyse_like: WeekdaySessionCalendar,
) -> None:
    decision = at(2026, 8, 3, 12, 0)  # Monday, before the session opens
    close = nyse_like.next_session_close(decision)
    assert close > decision
    assert to_datetime(close).astimezone(NY).date() == date(2026, 8, 3)
    assert to_datetime(close).astimezone(NY).time() == time(16, 0)


def test_next_session_close_during_a_session_is_todays_close(
    nyse_like: WeekdaySessionCalendar,
) -> None:
    mid_session = at(2026, 8, 3, 14, 30)
    close = nyse_like.next_session_close(mid_session)
    assert to_datetime(close).astimezone(NY).date() == date(2026, 8, 3)


def test_next_session_close_after_todays_close_is_the_next_sessions_close(
    nyse_like: WeekdaySessionCalendar,
) -> None:
    after_close = at(2026, 8, 3, 21, 0)
    close = nyse_like.next_session_close(after_close)
    assert to_datetime(close).astimezone(NY).date() == date(2026, 8, 4)


def test_next_session_close_reflects_an_early_close(nyse_like: WeekdaySessionCalendar) -> None:
    close = nyse_like.next_session_close(at(2026, 11, 27, 12, 0))
    assert to_datetime(close).astimezone(NY).time() == time(13, 0)


# -- 24/7 -----------------------------------------------------------------


def test_twenty_four_seven_is_always_open() -> None:
    calendar = TwentyFourSevenCalendar(code="SYNTH_CRYPTO", version="v1")
    assert calendar.is_open(at(2026, 8, 1, 3, 0))  # a Saturday
    assert calendar.is_open(at(2026, 12, 25, 3, 0))  # a holiday, for any equity calendar


def test_twenty_four_seven_next_execution_is_the_next_hour_boundary() -> None:
    calendar = TwentyFourSevenCalendar(code="SYNTH_CRYPTO", version="v1")
    assert calendar.next_eligible_execution(at(2026, 8, 1, 14, 23)) == at(2026, 8, 1, 15, 0)


def test_twenty_four_seven_next_execution_is_strictly_after_an_exact_hour() -> None:
    """Deciding exactly on the hour must not resolve to that same hour."""
    calendar = TwentyFourSevenCalendar(code="SYNTH_CRYPTO", version="v1")
    decision = at(2026, 8, 1, 14, 0)
    execution = calendar.next_eligible_execution(decision)
    assert execution == at(2026, 8, 1, 15, 0)
    assert execution > decision


# -- registry ---------------------------------------------------------------


def test_registry_resolves_by_code(nyse_like: WeekdaySessionCalendar) -> None:
    registry = CalendarRegistry()
    registry.register(nyse_like)
    assert registry.get("SYNTH_US_EQUITY") is nyse_like


def test_registry_rejects_duplicate_codes(nyse_like: WeekdaySessionCalendar) -> None:
    registry = CalendarRegistry()
    registry.register(nyse_like)
    with pytest.raises(ConfigurationError, match="already registered"):
        registry.register(nyse_like)


def test_registry_rejects_unknown_code() -> None:
    with pytest.raises(ConfigurationError, match="Unknown calendar code"):
        CalendarRegistry().get("NOPE")
