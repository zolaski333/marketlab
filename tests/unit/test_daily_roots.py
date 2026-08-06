"""Tests for daily root hashes (§24.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from marketlab.audit.roots import DailyRootService
from marketlab.core.clock import FrozenClock
from marketlab.core.failures import IntegrityError
from marketlab.core.instants import instant_from_datetime
from marketlab.storage.database import Database
from marketlab.storage.events import EventStore

DAY_ONE = "2026-08-03"
DAY_TWO = "2026-08-04"


def at(day: str, hour: int) -> str:
    year, month, date = (int(part) for part in day.split("-"))
    return instant_from_datetime(datetime(year, month, date, hour, tzinfo=UTC))


@pytest.fixture
def events(session: Session, clock: FrozenClock) -> EventStore:
    return EventStore(session, clock)


@pytest.fixture
def roots(session: Session, clock: FrozenClock) -> DailyRootService:
    return DailyRootService(session, clock)


def test_a_day_without_events_has_no_root(roots: DailyRootService) -> None:
    """A root over emptiness would be indistinguishable from a day whose events
    were deleted."""
    assert roots.compute(DAY_ONE) is None
    assert roots.seal(DAY_ONE) is None


def test_root_commits_to_the_days_boundaries_and_count(
    events: EventStore, roots: DailyRootService
) -> None:
    events.append("A", {"n": 1}, at(DAY_ONE, 9))
    events.append("B", {"n": 2}, at(DAY_ONE, 14))
    last = events.append("C", {"n": 3}, at(DAY_ONE, 20))

    root = roots.compute(DAY_ONE)
    assert root is not None
    assert (root.first_seq, root.last_seq, root.event_count) == (1, 3, 3)
    assert root.chain_head_hash == last.event_hash


def test_days_are_partitioned_correctly(events: EventStore, roots: DailyRootService) -> None:
    events.append("A", {"n": 1}, at(DAY_ONE, 9))
    events.append("B", {"n": 2}, at(DAY_TWO, 9))
    events.append("C", {"n": 3}, at(DAY_TWO, 18))

    first = roots.compute(DAY_ONE)
    second = roots.compute(DAY_TWO)
    assert first is not None and second is not None
    assert first.event_count == 1
    assert second.event_count == 2
    assert first.root_hash != second.root_hash


def test_sealing_is_idempotent(events: EventStore, roots: DailyRootService) -> None:
    events.append("A", {"n": 1}, at(DAY_ONE, 9))
    first = roots.seal(DAY_ONE)
    second = roots.seal(DAY_ONE)
    assert first == second


def test_root_changes_when_the_day_gains_an_event(
    events: EventStore, roots: DailyRootService
) -> None:
    events.append("A", {"n": 1}, at(DAY_ONE, 9))
    before = roots.compute(DAY_ONE)
    events.append("B", {"n": 2}, at(DAY_ONE, 10))
    after = roots.compute(DAY_ONE)

    assert before is not None and after is not None
    assert before.root_hash != after.root_hash


def test_resealing_a_changed_day_raises_rather_than_overwriting(
    events: EventStore, roots: DailyRootService
) -> None:
    """Divergence means history changed after it was committed to — which is
    exactly the event this mechanism exists to surface, not to paper over."""
    events.append("A", {"n": 1}, at(DAY_ONE, 9))
    roots.seal(DAY_ONE)

    events.append("B", {"n": 2}, at(DAY_ONE, 10))
    with pytest.raises(IntegrityError, match="changed after it was committed"):
        roots.seal(DAY_ONE)


def test_verify_all_detects_tampering_after_sealing(
    events: EventStore, roots: DailyRootService, database: Database
) -> None:
    events.append("A", {"probability": "0.60"}, at(DAY_ONE, 9))
    events.append("B", {"probability": "0.70"}, at(DAY_ONE, 10))
    roots.seal(DAY_ONE)
    assert roots.verify_all() == 1

    with database.migration_mode(reason="test tamper", author="test"):
        with database.engine.begin() as conn:
            conn.execute(
                text("UPDATE events SET event_hash = :h WHERE seq = 2"),
                {"h": "b" * 64},
            )

    with pytest.raises(IntegrityError, match="Daily root mismatch"):
        roots.verify_all()


def test_verify_all_detects_a_day_emptied_after_sealing(
    events: EventStore, roots: DailyRootService, database: Database
) -> None:
    events.append("A", {"n": 1}, at(DAY_ONE, 9))
    roots.seal(DAY_ONE)

    with database.migration_mode(reason="test tamper", author="test"):
        with database.engine.begin() as conn:
            conn.execute(text("DELETE FROM events"))

    with pytest.raises(IntegrityError, match="that day now has none"):
        roots.verify_all()


def test_daily_roots_are_themselves_append_only(
    events: EventStore, roots: DailyRootService, database: Database
) -> None:
    """A published commitment that could be quietly rewritten commits to nothing."""
    events.append("A", {"n": 1}, at(DAY_ONE, 9))
    roots.seal(DAY_ONE)

    from sqlalchemy.exc import IntegrityError as SqlIntegrityError

    with pytest.raises(SqlIntegrityError, match="append-only"), database.engine.begin() as conn:
        conn.execute(text("UPDATE daily_roots SET root_hash = 'x'"))


def test_day_of_extracts_the_utc_calendar_day(roots: DailyRootService) -> None:
    assert roots.day_of(at(DAY_ONE, 23)) == DAY_ONE
