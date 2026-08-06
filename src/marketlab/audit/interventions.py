"""Recording manual interventions (§P6).

§P6 forbids *silent* manual intervention, not manual intervention. Studies
running for months need operators to restart a stuck cycle, re-run a failed
ingestion, or correct a misconfiguration. What must never happen is that such an
action leaves no trace, so a reader of the results cannot tell whether the data
arose from the protocol or from someone's hand.

An intervention is therefore an ordinary event in the append-only log: it is
hash-chained with everything else, so it cannot be added retroactively without
breaking the chain, and it appears in the same timeline as the science it
touched.

Every field required by §P6 is mandatory, and the constructor refuses blanks.
An intervention record whose reason is ``""`` documents nothing while giving the
appearance of an audit trail — worse than no field at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from marketlab.core.clock import Clock
from marketlab.core.failures import ConfigurationError, FailureScope
from marketlab.core.instants import Instant
from marketlab.storage.events import EventRecord, EventStore

__all__ = ["INTERVENTION_EVENT_TYPE", "InterventionKind", "InterventionRecorder"]

INTERVENTION_EVENT_TYPE = "MANUAL_INTERVENTION"


class InterventionKind(StrEnum):
    """What class of manual action was taken."""

    RESTART = "RESTART"
    """A stalled run, cycle, or command was restarted."""

    RECONFIGURE = "RECONFIGURE"
    """Configuration was changed mid-study."""

    DATA_CORRECTION = "DATA_CORRECTION"
    """A superseding version was issued for an earlier artefact (§P4)."""

    SCHEMA_MIGRATION = "SCHEMA_MIGRATION"
    """Append-only enforcement was lifted for a migration."""

    EXCLUSION = "EXCLUSION"
    """A cycle, arm or repetition was marked unusable for the primary analysis."""

    OTHER = "OTHER"


@dataclass(frozen=True, slots=True)
class Intervention:
    """A manual action taken on a running study."""

    kind: InterventionKind
    author: str
    reason: str
    affected: tuple[str, ...]
    """Identifiers of the objects touched."""

    effect: str
    """What actually changed, in plain language."""

    scientific_scope: FailureScope
    """How the intervention bears on validity — the field a reader needs most."""


class InterventionRecorder:
    """Writes manual interventions into the append-only log."""

    __slots__ = ("_clock", "_events")

    def __init__(self, events: EventStore, clock: Clock) -> None:
        self._events = events
        self._clock = clock

    def record(
        self,
        *,
        kind: InterventionKind,
        author: str,
        reason: str,
        affected: tuple[str, ...],
        effect: str,
        scientific_scope: FailureScope,
        occurred_at: Instant | None = None,
        run_id: str | None = None,
        cycle_id: str | None = None,
    ) -> EventRecord:
        """Record an intervention. Returns the event that was appended.

        Raises:
            ConfigurationError: if any required field is blank or no affected
                object is named.
        """
        for name, value in (("author", author), ("reason", reason), ("effect", effect)):
            if not value.strip():
                raise ConfigurationError(
                    f"Manual intervention requires a non-empty {name}: a blank field "
                    "gives the appearance of an audit trail while documenting nothing "
                    "(invariant P6).",
                    field=name,
                )
        if not affected:
            raise ConfigurationError(
                "Manual intervention must name at least one affected object (invariant P6)."
            )

        when = occurred_at if occurred_at is not None else self._clock.now_instant()
        return self._events.append(
            INTERVENTION_EVENT_TYPE,
            {
                "kind": str(kind),
                "author": author,
                "reason": reason,
                "affected": sorted(affected),
                "effect": effect,
                "scientific_scope": str(scientific_scope),
            },
            when,
            run_id=run_id,
            cycle_id=cycle_id,
        )

    def all_interventions(self) -> list[EventRecord]:
        """Return every recorded intervention, in chain order.

        Exported alongside results so a reader can see exactly where human
        hands touched the study.
        """
        return list(self._events.iter_events(event_type=INTERVENTION_EVENT_TYPE))
