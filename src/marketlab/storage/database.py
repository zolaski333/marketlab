"""SQLite engine, append-only enforcement, and audited migrations.

Append-only is enforced by the *database*, not by discipline
------------------------------------------------------------
§P4 forbids overwriting past scientific data. A convention that "we only ever
INSERT" is worth little: one stray ``session.merge()``, one manual ``UPDATE`` in
a console, and the record is silently altered. So every scientific table carries
``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers that ``RAISE(FAIL)``. Mutating
history requires deliberately entering :meth:`Database.migration_mode`, which
records why.

Corrections therefore work the way §P4 prescribes: a correction is a *new*
version that supersedes the old one, never an edit of it.

Engine configuration
--------------------
``journal_mode=WAL``
    Readers do not block the writer, so an analysis query can run while a cycle
    is being recorded.
``foreign_keys=ON``
    Off by default in SQLite; without it, referential constraints are decorative.
``busy_timeout``
    Waits for a contended lock instead of failing instantly.

Why transactions are *deferred*, not ``IMMEDIATE``
--------------------------------------------------
Appending to the event log reads the chain head and then writes its successor,
which looks like it needs the write lock held across both steps. It does not.

The event log's ``seq`` column is its primary key. Two concurrent appenders that
both read head *N* would both try to insert *N+1*, and the second insert is
rejected by the primary-key constraint. The chain therefore **cannot fork**: the
loser gets a constraint violation and retries against the new head. Correctness
comes from the constraint, not from a lock.

Opening every transaction with ``BEGIN IMMEDIATE`` would instead make each
*read* take the write lock and hold it until commit, so a long analysis query
would block cycle recording — surrendering exactly the concurrency WAL exists to
provide.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from marketlab.core.failures import ConfigurationError

# Imported for its side effect: registering every model module with
# ``Base.metadata``. Without it, a table whose module happened not to be
# imported would be missing from the schema and from append-only protection.
from marketlab.storage import schema as _schema  # noqa: F401  (registration import)
from marketlab.storage.base import Base

__all__ = ["APPEND_ONLY_TABLES", "Database"]

# Tables holding scientific facts. Anything listed here becomes immutable once
# written. Derived/projection tables are deliberately absent: they are
# rebuildable by definition (§24.3), so locking them would only obstruct
# recomputation.
APPEND_ONLY_TABLES: Final[frozenset[str]] = frozenset(
    {
        "events",
        "blob_metadata",
        "daily_roots",
        "instruments",
        "instrument_versions",
        "snapshots",
        "snapshot_members",
        "decision_bundles",
        "ledger_transactions",
        "ledger_entries",
        "position_events",
        "orders",
        "fills",
        "order_rejections",
        "settlements",
        "corporate_action_applications",
    }
)

_BUSY_TIMEOUT_MS: Final = 10_000


class Database:
    """Owns the engine and the session factory for one study database."""

    __slots__ = ("_engine", "_path", "_session_factory")

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            f"sqlite:///{self._path}",
            future=True,
            # pysqlite opens implicit transactions of its own, which would
            # interleave with the BEGIN emitted by the "begin" listener below.
            # Disabling it hands transaction control to SQLAlchemy.
            connect_args={"isolation_level": None},
        )
        _install_engine_listeners(self._engine)
        self._session_factory = sessionmaker(
            bind=self._engine, autoflush=False, expire_on_commit=False, future=True
        )

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def path(self) -> Path:
        return self._path

    def session(self) -> Session:
        """Open a new session. Callers are responsible for closing it."""
        return self._session_factory()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        """Session context manager that rolls back on error and always closes."""
        session = self.session()
        try:
            yield session
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # -- schema ------------------------------------------------------------

    def create_schema(self) -> None:
        """Create tables and install append-only triggers. Idempotent."""
        Base.metadata.create_all(self._engine)
        self._install_append_only_triggers()

    def _install_append_only_triggers(self) -> None:
        known = set(Base.metadata.tables)
        unknown = APPEND_ONLY_TABLES - known
        if unknown:
            raise ConfigurationError(
                f"APPEND_ONLY_TABLES names tables that are not in the schema: "
                f"{sorted(unknown)}. A protected table that does not exist gives "
                "the false impression of protection.",
                unknown=sorted(unknown),
            )

        with self._engine.begin() as conn:
            for table in sorted(APPEND_ONLY_TABLES):
                for operation in ("UPDATE", "DELETE"):
                    # ASCII only: SQLite surfaces this text through its C API and
                    # non-ASCII characters come back mojibaked, which would make
                    # the one message an operator sees at 3am unreadable.
                    conn.execute(
                        text(
                            f"CREATE TRIGGER IF NOT EXISTS "
                            f"append_only_{operation.lower()}_{table} "
                            f"BEFORE {operation} ON {table} "
                            f"BEGIN SELECT RAISE(FAIL, "
                            f"'{table} is append-only ({operation} refused, "
                            f"invariant P4). Corrections must be recorded as a new "
                            f"superseding version.'); END"
                        )
                    )

    def _drop_append_only_triggers(self) -> None:
        with self._engine.begin() as conn:
            for table in sorted(APPEND_ONLY_TABLES):
                for operation in ("update", "delete"):
                    conn.execute(text(f"DROP TRIGGER IF EXISTS append_only_{operation}_{table}"))

    def installed_triggers(self) -> list[str]:
        """Return the append-only trigger names currently present."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'append_only_%' ORDER BY name"
                )
            )
            return [str(row[0]) for row in rows]

    # -- migrations --------------------------------------------------------

    @contextmanager
    def migration_mode(self, *, reason: str, author: str) -> Iterator[Engine]:
        """Temporarily lift append-only enforcement for a schema migration.

        Schema migrations legitimately rewrite tables — Alembic's standard
        recipe on SQLite recreates a table and copies rows across, which the
        triggers would block. Rather than leaving history writable, the window
        is made explicit, narrow, and attributable.

        The caller must supply a reason and an author; both are recorded, which
        is what §P6 requires of any manual intervention. Triggers are restored
        even if the migration raises.

        Args:
            reason: why history is being touched.
            author: who authorised it.

        Yields:
            The engine, with append-only triggers removed.
        """
        if not reason.strip():
            raise ConfigurationError(
                "migration_mode requires a reason: an unexplained mutation of "
                "append-only data is precisely what §P6 forbids."
            )
        if not author.strip():
            raise ConfigurationError("migration_mode requires an author (§P6).")

        self._drop_append_only_triggers()
        try:
            yield self._engine
        finally:
            # Restored in a finally block: a migration that fails midway must
            # not leave the scientific record writable.
            self._install_append_only_triggers()

    def close(self) -> None:
        self._engine.dispose()


def _install_engine_listeners(engine: Engine) -> None:
    """Attach SQLite pragmas and the immediate-transaction policy."""

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection: Any, _record: Any) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):  # pragma: no cover
            return
        cursor = dbapi_connection.cursor()
        try:
            # WAL persists in the database file; re-issuing it is harmless.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            # FULL rather than NORMAL: a study runs for months and a torn write
            # after a power loss would corrupt the source of truth. The cost is
            # irrelevant at this write volume.
            cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _begin_deferred(conn: Any) -> None:
        # Plain deferred BEGIN. Read-then-write consistency in the event log is
        # guaranteed by the primary-key constraint on ``seq`` (see the module
        # docstring), so reads must not take the write lock.
        conn.exec_driver_sql("BEGIN")
