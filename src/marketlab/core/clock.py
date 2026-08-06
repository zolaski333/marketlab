"""Injectable clocks (§6.1).

Nothing in the scientific path calls ``datetime.now()`` directly. Wall-clock
access is a dependency like any other, and making it injectable is what allows
a test to pin a DST boundary, or a replay to reproduce a run whose behaviour
depended on the time it happened to execute.

Three implementations cover the three modes the platform runs in:

``SystemClock``
    Real time. Used by live collection and by prospective cycles.
``FrozenClock``
    A fixed instant, optionally advanced by the test itself. Used by unit and
    property tests.
``ReplayClock``
    Replays a recorded sequence of instants, so a deterministic replay observes
    exactly the times the original run observed (§12.5).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from marketlab.core.instants import Instant, instant_from_datetime, to_datetime

__all__ = ["Clock", "FrozenClock", "ReplayClock", "SystemClock"]


def _coerce_instant(value: datetime | Instant) -> Instant:
    """Normalise either accepted spelling of a point in time.

    Instant literals are round-tripped through the parser so an invalid string
    fails at construction rather than at first use.
    """
    if isinstance(value, datetime):
        return instant_from_datetime(value)
    return instant_from_datetime(to_datetime(value))


@runtime_checkable
class Clock(Protocol):
    """Source of the current instant."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""
        ...

    def now_instant(self) -> Instant:
        """Return the current instant in canonical string form."""
        ...


class SystemClock:
    """Wall-clock time, in UTC."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_instant(self) -> Instant:
        return instant_from_datetime(self.now())


class FrozenClock:
    """A clock pinned to a fixed instant, advanced only on request."""

    __slots__ = ("_current",)

    def __init__(self, frozen_at: datetime | Instant) -> None:
        self._current = _coerce_instant(frozen_at)

    def now(self) -> datetime:
        return to_datetime(self._current)

    def now_instant(self) -> Instant:
        return self._current

    def set_to(self, value: datetime | Instant) -> None:
        """Reposition the clock."""
        self._current = _coerce_instant(value)

    def advance(self, delta: timedelta) -> None:
        """Move the clock forward (or backward, for negative deltas)."""
        self._current = instant_from_datetime(self.now() + delta)


class ReplayClock:
    """Replays a recorded sequence of instants in order.

    Raises once the recording is exhausted rather than falling back to wall
    time: a replay that needs more timestamps than the original run produced is
    not reproducing that run, and silently diverging would hide it.
    """

    __slots__ = ("_last", "_remaining")

    def __init__(self, instants: Iterable[Instant]) -> None:
        self._remaining: deque[Instant] = deque(instants)
        self._last: Instant | None = None

    def now(self) -> datetime:
        return to_datetime(self.now_instant())

    def now_instant(self) -> Instant:
        if not self._remaining:
            raise RuntimeError(
                "ReplayClock exhausted: the replay requested more timestamps than "
                "the recorded run produced, so it is no longer reproducing it."
            )
        self._last = self._remaining.popleft()
        return self._last

    @property
    def exhausted(self) -> bool:
        return not self._remaining
