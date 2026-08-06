"""Shared declarative base and column conventions.

Column type choices worth stating once, because they are scientific decisions
rather than ORM preferences:

``Instant`` columns are ``TEXT``
    Timestamps are stored as canonical fixed-width UTC strings (see
    :mod:`marketlab.core.instants`). SQLite has no native timestamp type, and a
    fixed-width text column has the property the platform needs: ``ORDER BY``
    over it is chronological order.

Monetary columns are ``TEXT``
    Never ``REAL``. A binary float cannot represent 0.01 exactly, and §17.2
    forbids floats in accounting. Amounts round-trip through
    :func:`marketlab.core.money.decimal_to_str`, which emits plain positional
    notation.

Probability columns are ``REAL``
    Forecast probabilities are *measurements*, not money. Float is appropriate
    and the ban in §17.2 does not extend to them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import MetaData, String, Text
from sqlalchemy.orm import DeclarativeBase

__all__ = ["Base", "DecimalStr", "HashStr", "InstantStr", "JsonStr", "ShortStr"]

# Deterministic constraint names, so migrations can reference them by name
# rather than by whatever the backend happened to generate.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every persisted table."""

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = self.__mapper__.primary_key_from_instance(self)
        return f"<{type(self).__name__} {pk}>"


# Column type aliases, named for intent so a reader can tell at a glance what a
# TEXT column actually holds.
InstantStr: Any = String(27)
"""Canonical UTC instant, fixed width."""

HashStr: Any = String(64)
"""Lowercase SHA-256 hex digest."""

ShortStr: Any = String(64)
"""Identifier, code, or enum value."""

DecimalStr: Any = String(48)
"""Exact decimal in plain positional notation."""

JsonStr: Any = Text
"""Canonical JSON document."""
