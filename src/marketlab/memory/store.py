"""Persistent episodic memory (§18).

This is one half of the thing the study is actually testing. Everything before
it built a platform that could measure a difference between conditions; this is
where a difference can first exist, because arms B and C are given something
arms A and D are not.

What an episode is
------------------
One decision, as the condition itself made it: what it forecast, what it
intended to trade, what it said, and what its book was worth at the time. Not
a market observation — the retrieval tools already give every arm those, and
duplicating them into memory would make memory a slower copy of the snapshot
rather than a record of the agent's own history.

The recall cutoff is strict, and that is not pedantry
-----------------------------------------------------
:meth:`MemoryStore.recall` returns episodes dated **strictly before** the
requested instant. ``<=`` would be wrong in a way that is easy to miss and
fatal when it happens: a cycle interrupted after its episode was written and
then resumed would recall *its own decision* as prior history and be asked to
decide again with the answer in hand. That is not a subtle bias, it is
look-ahead within a single cycle, and §30.6's resume guarantee is exactly what
makes it reachable.

One memory per condition, per repetition
----------------------------------------
Scope is ``(run, arm, repetition)``. Two repetitions of arm B must accumulate
independent histories — if they shared one, the second repetition would start
with the first's experience and the within-condition variance the design needs
to measure would collapse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.agents.decision import DecisionOutcome
from marketlab.core.canonical import canonical_bytes
from marketlab.core.clock import Clock
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant
from marketlab.core.money import Money
from marketlab.storage.base import Base, HashStr, InstantStr, ShortStr
from marketlab.storage.blobs import BlobStore

__all__ = [
    "Episode",
    "EpisodeForecast",
    "EpisodeIntent",
    "MemoryEpisodeRow",
    "MemoryStore",
    "memory_scope_id",
]

DEFAULT_RECALL_LIMIT = 8
"""How many past episodes a recall returns by default.

A placeholder, like the tool budget: it trades context cost against how far
back a condition can see, and the right value depends on the same API cost
model (task #4) that is still open. Recorded in ``docs/ROADMAP.md``.
"""


def memory_scope_id(run_id: str, arm_id: str, repetition: int) -> str:
    """The memory one condition accumulates. Distinct per repetition."""
    return derive_id(IdKind.MEMORY_SCOPE, run_id=run_id, arm_id=arm_id, repetition=repetition)


class MemoryEpisodeRow(Base):
    """One remembered decision. Append-only, like everything scientific."""

    __tablename__ = "memory_episodes"

    episode_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    scope_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    cycle_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    bundle_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    as_of: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    recorded_at: Mapped[str] = mapped_column(InstantStr, nullable=False)
    payload_blob_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    forecast_count: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_count: Mapped[int] = mapped_column(Integer, nullable=False)
    equity: Mapped[str] = mapped_column(ShortStr, nullable=False, default="")


@dataclass(frozen=True, slots=True)
class EpisodeForecast:
    instrument_id: str
    horizon_sessions: int
    probability_up: float


@dataclass(frozen=True, slots=True)
class EpisodeIntent:
    instrument_id: str
    side: str


@dataclass(frozen=True, slots=True)
class Episode:
    """One past decision, as the condition that made it would recall it."""

    episode_id: str
    cycle_id: str
    as_of: Instant
    forecasts: tuple[EpisodeForecast, ...]
    intents: tuple[EpisodeIntent, ...]
    narrative: str
    equity: str
    failure_kinds: tuple[str, ...]


class MemoryStore:
    """Records and recalls one study's episodic memory."""

    __slots__ = ("_blobs", "_clock", "_session")

    def __init__(self, session: Session, clock: Clock, blobs: BlobStore) -> None:
        self._session = session
        self._clock = clock
        self._blobs = blobs

    # -- writing -------------------------------------------------------------

    def record(
        self,
        scope_id: str,
        *,
        cycle_id: str,
        bundle_id: str,
        as_of: Instant,
        outcome: DecisionOutcome,
        equity: Money | None = None,
    ) -> Episode:
        """Remember one decision. Idempotent by derived ``episode_id``."""
        episode = Episode(
            episode_id=derive_id(IdKind.EPISODE, scope_id=scope_id, bundle_id=bundle_id),
            cycle_id=cycle_id,
            as_of=as_of,
            forecasts=tuple(
                EpisodeForecast(f.instrument_id, f.horizon_sessions, f.probability_up)
                for f in outcome.forecasts
            ),
            intents=tuple(
                EpisodeIntent(t.instrument_id, str(t.side)) for t in outcome.trade_intents
            ),
            narrative="",
            equity=str(equity) if equity is not None else "",
            failure_kinds=tuple(str(f.kind) for f in outcome.failures),
        )
        if self._session.get(MemoryEpisodeRow, episode.episode_id) is not None:
            return episode

        blob = self._blobs.put(canonical_bytes(_to_payload(episode)))
        self._session.add(
            MemoryEpisodeRow(
                episode_id=episode.episode_id,
                scope_id=scope_id,
                cycle_id=cycle_id,
                bundle_id=bundle_id,
                as_of=str(as_of),
                recorded_at=str(self._clock.now_instant()),
                payload_blob_hash=blob.digest,
                forecast_count=len(episode.forecasts),
                intent_count=len(episode.intents),
                equity=episode.equity,
            )
        )
        self._session.flush()
        return episode

    # -- reading -------------------------------------------------------------

    def recall(
        self, scope_id: str, *, before: Instant, limit: int = DEFAULT_RECALL_LIMIT
    ) -> tuple[Episode, ...]:
        """The most recent episodes dated **strictly before** ``before``.

        Returned oldest first, so the text built from them reads as a
        chronology rather than in reverse.
        """
        if limit <= 0:
            return ()
        rows = list(
            self._session.execute(
                select(MemoryEpisodeRow)
                .where(MemoryEpisodeRow.scope_id == scope_id)
                .where(MemoryEpisodeRow.as_of < str(before))
                .order_by(MemoryEpisodeRow.as_of.desc(), MemoryEpisodeRow.episode_id.desc())
                .limit(limit)
            ).scalars()
        )
        rows.reverse()
        return tuple(self._load(row) for row in rows)

    def episode_count(self, scope_id: str, *, before: Instant) -> int:
        """How many episodes this condition could recall."""
        rows = self._session.execute(
            select(MemoryEpisodeRow.episode_id)
            .where(MemoryEpisodeRow.scope_id == scope_id)
            .where(MemoryEpisodeRow.as_of < str(before))
        ).scalars()
        return len(list(rows))

    def episode_shapes(
        self, scope_id: str, *, before: Instant, limit: int = DEFAULT_RECALL_LIMIT
    ) -> tuple[tuple[int, int], ...]:
        """``(forecast_count, intent_count)`` per recallable episode, oldest first.

        This is how the placebo generator sizes its output to match the
        genuine article. It reads **two integer columns and nothing else** —
        never the payload blob — so it is structurally incapable of leaking
        content into a placebo, rather than merely intended not to. A future
        change that tried to would have to reach for the blob store, which
        this method does not hold a reference to at all.
        """
        if limit <= 0:
            return ()
        rows = list(
            self._session.execute(
                select(MemoryEpisodeRow.forecast_count, MemoryEpisodeRow.intent_count)
                .where(MemoryEpisodeRow.scope_id == scope_id)
                .where(MemoryEpisodeRow.as_of < str(before))
                .order_by(MemoryEpisodeRow.as_of.desc(), MemoryEpisodeRow.episode_id.desc())
                .limit(limit)
            )
        )
        rows.reverse()
        return tuple((int(row[0]), int(row[1])) for row in rows)

    def _load(self, row: MemoryEpisodeRow) -> Episode:
        payload = json.loads(self._blobs.get(row.payload_blob_hash))
        return _from_payload(row, payload)


def _to_payload(episode: Episode) -> dict[str, Any]:
    return {
        "cycle_id": episode.cycle_id,
        "as_of": str(episode.as_of),
        "forecasts": [
            {
                "instrument_id": f.instrument_id,
                "horizon_sessions": f.horizon_sessions,
                "probability_up": f.probability_up,
            }
            for f in episode.forecasts
        ],
        "intents": [{"instrument_id": i.instrument_id, "side": i.side} for i in episode.intents],
        "narrative": episode.narrative,
        "equity": episode.equity,
        "failure_kinds": list(episode.failure_kinds),
    }


def _from_payload(row: MemoryEpisodeRow, payload: dict[str, Any]) -> Episode:
    return Episode(
        episode_id=row.episode_id,
        cycle_id=str(payload["cycle_id"]),
        as_of=Instant(str(payload["as_of"])),
        forecasts=tuple(
            EpisodeForecast(
                instrument_id=str(entry["instrument_id"]),
                horizon_sessions=int(entry["horizon_sessions"]),
                probability_up=float(entry["probability_up"]),
            )
            for entry in payload["forecasts"]
        ),
        intents=tuple(
            EpisodeIntent(instrument_id=str(entry["instrument_id"]), side=str(entry["side"]))
            for entry in payload["intents"]
        ),
        narrative=str(payload["narrative"]),
        equity=str(payload["equity"]),
        failure_kinds=tuple(str(kind) for kind in payload["failure_kinds"]),
    )
