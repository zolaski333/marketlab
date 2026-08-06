"""Canonical UTC instants.

Scientific requirement §6.1: every internal timestamp is stored in UTC with an
explicit timezone. Naive datetimes are forbidden — not by convention, but by
every constructor in this module raising on them.

Why a canonical *string* format matters
---------------------------------------
Timestamps are persisted as TEXT and are ordered by SQL and by Python string
comparison in several hot paths (event chain sequencing, snapshot membership,
point-in-time filtering). ``datetime.isoformat()`` produces a *variable-width*
string: it omits the microsecond field when it is zero. Mixing widths breaks
lexicographic ordering, because the comparison then reaches the ``Z``/``.``
characters at the same index::

    "2026-08-01T16:00:00Z"        # no microseconds
    "2026-08-01T16:00:00.500000Z" # with microseconds

    >>> "2026-08-01T16:00:00Z" < "2026-08-01T16:00:00.500000Z"
    False          # 'Z' (0x5A) sorts after '.' (0x2E) — chronologically wrong

So this module emits a *fixed-width* format with microseconds always present:

    YYYY-MM-DDTHH:MM:SS.ffffffZ   (27 characters)

For that format, lexicographic order is exactly chronological order, which is
what makes ``ORDER BY`` over a timestamp column trustworthy.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final, NewType

__all__ = [
    "INSTANT_PATTERN",
    "INSTANT_WIDTH",
    "Instant",
    "instant_from_datetime",
    "is_instant",
    "parse_instant",
    "to_datetime",
]

# A canonical UTC timestamp string. NewType keeps it distinguishable from an
# arbitrary ``str`` under mypy, so a raw provider timestamp cannot be passed
# where a canonical instant is required without going through a parser.
Instant = NewType("Instant", str)

INSTANT_WIDTH: Final = 27
_INSTANT_RE: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
)
INSTANT_PATTERN: Final = _INSTANT_RE.pattern


def instant_from_datetime(value: datetime) -> Instant:
    """Convert a timezone-aware datetime to a canonical instant.

    Raises:
        ValueError: if ``value`` is naive. A naive datetime has no defined
            position on the timeline; silently assuming UTC (or worse, local
            time) is exactly the class of error §6.1 exists to prevent.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"Naive datetime rejected: {value!r}. All instants must carry an "
            "explicit timezone (§6.1)."
        )
    text = value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return Instant(text)


def parse_instant(text: str) -> Instant:
    """Validate and normalise an instant string.

    Accepts the canonical form directly, and also the common variants produced
    by upstream sources (``+00:00`` offsets, missing or short microseconds),
    normalising them to canonical width. Anything that does not denote an
    unambiguous UTC instant is rejected.
    """
    if _INSTANT_RE.match(text):
        return Instant(text)

    candidate = text.strip()
    if candidate.endswith(("z", "Z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"Unparseable instant: {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"Instant without timezone rejected: {text!r}. Naive timestamps are forbidden (§6.1)."
        )
    return instant_from_datetime(parsed)


def to_datetime(value: Instant) -> datetime:
    """Convert a canonical instant back to a timezone-aware datetime."""
    if not _INSTANT_RE.match(value):
        raise ValueError(f"Not a canonical instant: {value!r}")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def is_instant(text: str) -> bool:
    """Return whether ``text`` is already in canonical instant form."""
    return bool(_INSTANT_RE.match(text))
