"""Tests for the append-only event log and its hash chain (§P4, §24.1).

The first test in the "ordering" section is the one that matters most: it
reproduces the exact situation that made a previous implementation's chain
unsound — several events sharing a single timestamp.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SqlIntegrityError
from sqlalchemy.orm import Session

from marketlab.core.clock import FrozenClock
from marketlab.core.failures import IntegrityError
from marketlab.core.instants import instant_from_datetime
from marketlab.storage.database import Database
from marketlab.storage.events import GENESIS_HASH, EventStore

CUTOFF = instant_from_datetime(datetime(2026, 8, 3, 20, 0, tzinfo=UTC))


@pytest.fixture
def store(session: Session, clock: FrozenClock) -> EventStore:
    return EventStore(session, clock)


# ---------------------------------------------------------------------------
# Chain construction
# ---------------------------------------------------------------------------


def test_first_event_links_to_genesis(store: EventStore) -> None:
    event = store.append("CYCLE_OPENED", {"cycle": 1}, CUTOFF)
    assert event.seq == 1
    assert event.previous_hash == GENESIS_HASH


def test_each_event_links_to_its_predecessor(store: EventStore) -> None:
    first = store.append("A", {"n": 1}, CUTOFF)
    second = store.append("B", {"n": 2}, CUTOFF)
    third = store.append("C", {"n": 3}, CUTOFF)

    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert second.previous_hash == first.event_hash
    assert third.previous_hash == second.event_hash
    assert store.verify_chain() == 3


def test_appending_the_same_event_twice_is_idempotent(store: EventStore) -> None:
    """Resuming an interrupted cycle must not duplicate history (§30.6)."""
    first = store.append("ORDER_PLACED", {"order": "o1"}, CUTOFF, cycle_id="c1")
    again = store.append("ORDER_PLACED", {"order": "o1"}, CUTOFF, cycle_id="c1")

    assert again.seq == first.seq
    assert again.event_hash == first.event_hash
    assert store.count() == 1


def test_events_differing_only_in_payload_are_distinct(store: EventStore) -> None:
    store.append("ORDER_PLACED", {"order": "o1"}, CUTOFF)
    store.append("ORDER_PLACED", {"order": "o2"}, CUTOFF)
    assert store.count() == 2


def test_events_differing_only_in_arm_are_distinct(store: EventStore) -> None:
    """Arms record structurally identical events; they must not collapse."""
    store.append("DECISION_SEALED", {"x": 1}, CUTOFF, arm_id="B")
    store.append("DECISION_SEALED", {"x": 1}, CUTOFF, arm_id="C")
    assert store.count() == 2


# ---------------------------------------------------------------------------
# Ordering — the defect this design corrects
# ---------------------------------------------------------------------------


def test_chain_is_sound_when_every_event_shares_one_timestamp(store: EventStore) -> None:
    """All arms of a cycle are stamped with the same cutoff.

    Ordering the chain by timestamp leaves their relative order undefined, so a
    timestamp-ordered implementation can link to an arbitrary parent and a
    verifier can walk them in a different order. Sequencing by a monotonic
    counter removes the ambiguity entirely.
    """
    events = [store.append("DECISION", {"arm": arm}, CUTOFF, arm_id=arm) for arm in "ABCDEF"]

    assert [e.seq for e in events] == [1, 2, 3, 4, 5, 6]
    assert len({e.occurred_at for e in events}) == 1, "precondition: one shared timestamp"
    for earlier, later in itertools.pairwise(events):
        assert later.previous_hash == earlier.event_hash
    assert store.verify_chain() == 6


def test_chain_order_is_insertion_order_not_domain_time(store: EventStore) -> None:
    """A late-arriving observation about an earlier moment still appends at the
    head; the chain records *when we learned*, not what it is about."""
    later_domain_time = instant_from_datetime(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))
    earlier_domain_time = instant_from_datetime(datetime(2026, 8, 1, 20, 0, tzinfo=UTC))

    first = store.append("OBSERVED", {"n": 1}, later_domain_time)
    second = store.append("OBSERVED", {"n": 2}, earlier_domain_time)

    assert second.seq > first.seq
    assert second.previous_hash == first.event_hash
    assert store.verify_chain() == 2


def test_iteration_follows_chain_order(store: EventStore) -> None:
    for index in range(5):
        store.append("STEP", {"n": index}, CUTOFF)
    assert [e.payload["n"] for e in store.iter_events()] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


def test_altered_payload_is_detected(store: EventStore, database: Database) -> None:
    store.append("FORECAST", {"probability": "0.60"}, CUTOFF)
    store.append("FORECAST", {"probability": "0.55"}, CUTOFF)

    # Rewrite history behind the ORM, with triggers lifted — simulating an
    # attacker or an operator with direct database access.
    with database.migration_mode(reason="test tamper", author="test"):
        with database.engine.begin() as conn:
            conn.execute(
                text("UPDATE events SET payload_json = :new WHERE seq = 1"),
                {"new": '{"probability":"0.95"}'},
            )

    with pytest.raises(IntegrityError, match="Payload altered at seq 1"):
        store.verify_chain()


def test_altered_event_hash_is_detected(store: EventStore, database: Database) -> None:
    store.append("A", {"n": 1}, CUTOFF)
    with database.migration_mode(reason="test tamper", author="test"):
        with database.engine.begin() as conn:
            conn.execute(text("UPDATE events SET event_hash = :h WHERE seq = 1"), {"h": "f" * 64})

    with pytest.raises(IntegrityError, match="Event hash mismatch at seq 1"):
        store.verify_chain()


def test_deleted_event_is_detected_as_a_sequence_gap(store: EventStore, database: Database) -> None:
    """Removing a row leaves a hole the verifier reports rather than skipping."""
    for index in range(4):
        store.append("STEP", {"n": index}, CUTOFF)

    with database.migration_mode(reason="test tamper", author="test"):
        with database.engine.begin() as conn:
            conn.execute(text("DELETE FROM events WHERE seq = 2"))

    with pytest.raises(IntegrityError, match="expected seq 2, found 3"):
        store.verify_chain()


def test_reordering_events_is_detected(store: EventStore, database: Database) -> None:
    """seq is inside the hashed material, so moving a row invalidates it."""
    store.append("A", {"n": 1}, CUTOFF)
    store.append("B", {"n": 2}, CUTOFF)

    with database.migration_mode(reason="test tamper", author="test"):
        with database.engine.begin() as conn:
            conn.execute(text("UPDATE events SET seq = 99 WHERE seq = 2"))

    with pytest.raises(IntegrityError):
        store.verify_chain()


def test_empty_log_verifies(store: EventStore) -> None:
    assert store.verify_chain() == 0


# ---------------------------------------------------------------------------
# Append-only enforcement
# ---------------------------------------------------------------------------


def test_update_on_events_is_refused_by_the_database(store: EventStore, database: Database) -> None:
    """Enforcement is a trigger, not a convention: raw SQL is refused too."""
    store.append("A", {"n": 1}, CUTOFF)
    with pytest.raises(SqlIntegrityError, match="append-only"), database.engine.begin() as conn:
        conn.execute(text("UPDATE events SET event_type = 'X' WHERE seq = 1"))


def test_delete_on_events_is_refused_by_the_database(store: EventStore, database: Database) -> None:
    store.append("A", {"n": 1}, CUTOFF)
    with pytest.raises(SqlIntegrityError, match="append-only"), database.engine.begin() as conn:
        conn.execute(text("DELETE FROM events WHERE seq = 1"))


def test_migration_mode_requires_a_reason_and_an_author(database: Database) -> None:
    """§P6: a manual intervention without attribution is what is forbidden."""
    with pytest.raises(Exception, match="requires a reason"):
        with database.migration_mode(reason="  ", author="someone"):
            pass
    with pytest.raises(Exception, match="requires an author"):
        with database.migration_mode(reason="a reason", author=""):
            pass


def test_migration_mode_restores_triggers_even_when_it_fails(database: Database) -> None:
    """A migration that raises must not leave the record writable."""
    before = database.installed_triggers()
    assert before, "precondition: triggers installed"

    with pytest.raises(RuntimeError, match="migration blew up"):
        with database.migration_mode(reason="test", author="test"):
            assert database.installed_triggers() == []
            raise RuntimeError("migration blew up")

    assert database.installed_triggers() == before


def test_timestamps_are_recorded_from_the_injected_clock(session: Session) -> None:
    clock = FrozenClock(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    store = EventStore(session, clock)

    first = store.append("A", {"n": 1}, CUTOFF)
    clock.advance(timedelta(hours=3))
    second = store.append("B", {"n": 2}, CUTOFF)

    assert first.recorded_at == "2026-01-01T12:00:00.000000Z"
    assert second.recorded_at == "2026-01-01T15:00:00.000000Z"
    # Domain time is unaffected by when the row happened to be written.
    assert first.occurred_at == second.occurred_at == CUTOFF
