"""Point-in-time cutoff (§6.3, INV-P5).

Every scientific read that can influence a decision is bound to a cutoff: a
single instant beyond which nothing may be observed. A :class:`Cutoff` is the
only sanctioned way to express that boundary — repositories serving the
decision path take one as a required parameter, never an optional one, so
there is no call site where the temporal bound could be forgotten.

This is deliberately a thin value object, not a generic "point-in-time reader"
wrapping arbitrary queries. Each repository already knows its own
point-in-time semantics (which column gates availability, how versions are
superseded); a one-size-fits-all wrapper would either constrain those
repositories to a lowest common denominator or leak back into being just as
easy to bypass as no abstraction at all. What actually has to be true
everywhere is narrower, and is enforced two ways instead:

1. every read method in a decision-path repository requires a ``Cutoff``
   (a convention verified by each repository's own tests), and
2. the decision path never holds a raw database session at all — see
   ``tests/security/test_decision_path_isolation.py``, which fails the build
   the moment ``agents``, ``retrieval`` or ``forecasting`` imports SQLAlchemy
   directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from marketlab.core.instants import Instant, is_instant

__all__ = ["Cutoff"]


@dataclass(frozen=True, slots=True)
class Cutoff:
    """A validated point-in-time boundary.

    ``snapshot_id`` is optional provenance — which frozen snapshot this cutoff
    came from, kept for logging and reconstruction (§9.4). The temporal
    comparison itself only ever uses ``as_of``.
    """

    as_of: Instant
    snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if not is_instant(self.as_of):
            raise ValueError(
                f"Cutoff.as_of must be a canonical instant, got {self.as_of!r}. "
                "A malformed cutoff could compare incorrectly against stored "
                "first_seen_at values and silently leak future data (INV-P5)."
            )

    def allows(self, first_seen_at: Instant) -> bool:
        """Whether data first observed at ``first_seen_at`` may be read now.

        Availability is gated by ``first_seen_at`` — when the platform actually
        received the data — never by a source's claimed publication time
        (§6.2). Equality is allowed: data seen exactly at the cutoff was seen
        no later than it, so it is visible.
        """
        return first_seen_at <= self.as_of
