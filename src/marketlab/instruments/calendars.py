"""Market trading calendars (§6.4, §16.2).

Every calendar answers two questions: is the market open at this instant, and
— the one the execution engine actually depends on — what is the first
eligible execution instant *strictly after* a given decision (§16.2 forbids
executing at or before the moment of decision).

Two implementations cover what the synthetic universe needs:

:class:`WeekdaySessionCalendar`
    A single-session-per-day market (equities, ETFs). Session boundaries are
    expressed as local wall-clock time and converted to UTC through the
    standard library's :mod:`zoneinfo`, which is what gives correct daylight
    saving time handling — the same 09:30 local open lands at a different UTC
    instant in winter than in summer — without depending on
    ``exchange_calendars`` (deferred until a real, changing calendar actually
    needs it; see ``docs/ROADMAP.md``).
:class:`TwentyFourSevenCalendar`
    Always open (crypto). "The first eligible window after a decision" still
    needs a concrete answer for a market that never closes; §6.4 calls for a
    configurable latency, implemented here as advancing to the next top of the
    hour.

Instrument-level suspension (a halt on one specific instrument) is *not*
modelled here — it is a property of the instrument's current version
(``InstrumentStatus.SUSPENDED``), not of the market it trades on. A calendar
only knows about market-wide closures: weekends, holidays, early closes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from marketlab.core.failures import ConfigurationError
from marketlab.core.instants import Instant, instant_from_datetime, to_datetime

__all__ = [
    "CalendarRegistry",
    "MarketCalendar",
    "TwentyFourSevenCalendar",
    "WeekdaySessionCalendar",
]

_MAX_SEARCH_DAYS = 15
"""Bound on how far next_eligible_execution searches forward before giving up.

Generous next to any real holiday cluster; hitting it means the calendar
configuration itself is broken (e.g. every day marked a holiday), not that the
market is legitimately closed that long.
"""


@runtime_checkable
class MarketCalendar(Protocol):
    """A versioned trading calendar (§6.4: "les calendriers doivent être versionnés").

    ``code``/``version`` are declared as read-only properties rather than plain
    attributes: the concrete calendars below are frozen dataclasses, and a
    plain-attribute protocol would structurally require a *settable* field,
    which a frozen dataclass's attributes are not — mypy would reject a
    perfectly good frozen calendar as "not a MarketCalendar".
    """

    @property
    def code(self) -> str: ...
    @property
    def version(self) -> str: ...

    def is_open(self, instant: Instant) -> bool:
        """Whether the market is trading at ``instant``."""
        ...

    def next_eligible_execution(self, decision_at: Instant) -> Instant:
        """The first instant strictly after ``decision_at`` at which an order
        may execute (§16.2)."""
        ...


@dataclass(frozen=True, slots=True)
class TwentyFourSevenCalendar:
    """A market that never closes (crypto spot)."""

    code: str
    version: str

    def is_open(self, instant: Instant) -> bool:
        return True

    def next_eligible_execution(self, decision_at: Instant) -> Instant:
        """The next top-of-the-hour, strictly after ``decision_at``.

        §6.4: "une latence ou prochaine barre d'exécution doit être
        configurée" for markets that never close. An hourly bar is the
        configured latency for the synthetic universe.
        """
        moment = to_datetime(decision_at)
        next_hour = (moment + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return instant_from_datetime(next_hour)


@dataclass(frozen=True, slots=True)
class WeekdaySessionCalendar:
    """A single daily session, closed on weekends and configured holidays.

    ``session_open``/``session_close`` are local wall-clock times in
    ``iana_tz``; the DST-correct UTC instant is derived per calendar date via
    :mod:`zoneinfo`, not assumed fixed.
    """

    code: str
    version: str
    iana_tz: str
    session_open: time
    session_close: time
    holidays: frozenset[date] = field(default_factory=frozenset)
    early_closes: Mapping[date, time] = field(default_factory=dict)
    """Calendar date -> local close time, for shortened sessions (§6.4)."""

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.iana_tz)
        except Exception as exc:  # zoneinfo raises differently across platforms
            raise ConfigurationError(
                f"Unknown IANA timezone {self.iana_tz!r} for calendar {self.code}."
            ) from exc
        if self.session_open >= self.session_close:
            raise ConfigurationError(
                f"Calendar {self.code}: session_open {self.session_open} must be "
                f"before session_close {self.session_close}."
            )

    def _session_bounds_utc(self, local_day: date) -> tuple[datetime, datetime] | None:
        """UTC (open, close) for ``local_day``, or ``None`` if it is not a session day."""
        if local_day.weekday() >= 5:  # Saturday=5, Sunday=6
            return None
        if local_day in self.holidays:
            return None

        tz = ZoneInfo(self.iana_tz)
        close_time = self.early_closes.get(local_day, self.session_close)
        open_local = datetime.combine(local_day, self.session_open, tzinfo=tz)
        close_local = datetime.combine(local_day, close_time, tzinfo=tz)
        return open_local.astimezone(UTC), close_local.astimezone(UTC)

    def is_open(self, instant: Instant) -> bool:
        moment = to_datetime(instant)
        local_day = moment.astimezone(ZoneInfo(self.iana_tz)).date()
        # Also check the neighbouring calendar days, in case a session's UTC
        # span crosses the local midnight boundary used to pick local_day.
        for candidate in (local_day - timedelta(days=1), local_day, local_day + timedelta(days=1)):
            bounds = self._session_bounds_utc(candidate)
            if bounds is not None and bounds[0] <= moment < bounds[1]:
                return True
        return False

    def next_eligible_execution(self, decision_at: Instant) -> Instant:
        return self._next_session_boundary(decision_at, select=lambda bounds: bounds[0])

    def next_session_close(self, after: Instant) -> Instant:
        """The close of the first session whose close is strictly after ``after``.

        A distinct question from :meth:`next_eligible_execution` (which answers
        "when can an order fill"): this is what a decide-at-close cadence uses
        to generate a study's sequence of decision cutoffs, and what
        horizon-based forecast resolution (§20) uses to find "the close of
        session t+N".
        """
        return self._next_session_boundary(after, select=lambda bounds: bounds[1])

    def _next_session_boundary(
        self, after: Instant, *, select: Callable[[tuple[datetime, datetime]], datetime]
    ) -> Instant:
        moment = to_datetime(after)
        local_day = moment.astimezone(ZoneInfo(self.iana_tz)).date()

        for offset in range(_MAX_SEARCH_DAYS):
            candidate = local_day + timedelta(days=offset)
            bounds = self._session_bounds_utc(candidate)
            if bounds is None:
                continue
            boundary = select(bounds)
            if boundary > moment:
                return instant_from_datetime(boundary)

        raise ConfigurationError(
            f"No trading session found within {_MAX_SEARCH_DAYS} days of "
            f"{after} for calendar {self.code}; check its holiday "
            "configuration for an unintended full closure."
        )


class CalendarRegistry:
    """Maps calendar codes to calendar instances.

    A plain injected registry rather than a global: different studies (or a
    study and its tests) may configure different calendars under the same
    code without interfering with each other.
    """

    __slots__ = ("_calendars",)

    def __init__(self) -> None:
        self._calendars: dict[str, MarketCalendar] = {}

    def register(self, calendar: MarketCalendar) -> None:
        if calendar.code in self._calendars:
            raise ConfigurationError(f"Calendar code already registered: {calendar.code!r}")
        self._calendars[calendar.code] = calendar

    def get(self, code: str) -> MarketCalendar:
        try:
            return self._calendars[code]
        except KeyError:
            raise ConfigurationError(f"Unknown calendar code: {code!r}") from None
