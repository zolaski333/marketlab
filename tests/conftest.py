"""Shared fixtures.

Every test gets its own database file and blob root under ``tmp_path``, so no
test can observe another's writes — the same isolation the experimental arms
require of each other (§30.3).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from marketlab.core.clock import FrozenClock
from marketlab.storage.blobs import BlobStore
from marketlab.storage.database import Database

# A fixed instant used as the default "now" wherever a test does not care about
# the specific time but does care that it is deterministic.
REFERENCE_INSTANT = datetime(2026, 8, 3, 20, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(REFERENCE_INSTANT)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "study.db")
    db.create_schema()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def session(database: Database) -> Iterator[Session]:
    with database.session_scope() as active:
        yield active


@pytest.fixture
def blob_store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")
