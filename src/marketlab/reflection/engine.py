"""Periodic strategic reflection (§19).

The other half of what the study tests. Where
:mod:`marketlab.memory.store` gives a condition its raw history, this gives it
a *distillation* — and the difference between the two is exactly what arms B
and D are meant to separate (see
:class:`marketlab.experiments.arms.Channel`).

Deterministic, for the same reason the policy fake is
------------------------------------------------------
A real deployment would ask a model to reflect. Phase 1 has no real provider,
and a reflection produced by a random or opaque process could not be replayed
(§12.5) or reasoned about when an arm's results looked odd. So the rules here
are closed-form functions of the condition's own episodes, in the same spirit
as :class:`~marketlab.models.deterministic.DeterministicPolicyModel`: a
reproducible stand-in with an honest shape, replaceable by a model-authored
reflection without any caller changing.

What it can and cannot say
--------------------------
These rules are about **the agent's own behaviour** — how persistently it has
taken one side, how much its stated probabilities move, how often its output
was malformed. They deliberately say nothing about whether its forecasts came
true, because forecast resolution is task 12 and does not exist yet. A rule
claiming a hit rate today would be fabricated, which is precisely the failure
mode this project was rebuilt to avoid.

That limitation is honest rather than crippling: coherence and stability are
themselves among the outcomes §21 cares about, and an agent told "you have
been long the same instrument for eight straight cycles" has been handed a
real strategic observation.

Periodic, not per-cycle
-----------------------
Reflection runs every ``interval`` cycles; between runs a condition carries
the most recent one (:meth:`ReflectionEngine.latest`). Reflecting every cycle
would make "reflection" indistinguishable from "a longer prompt", which is the
confound the B'/C' placebos exist to rule out.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from statistics import fmean, pstdev
from typing import Any, Final

from sqlalchemy import Integer, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.core.canonical import canonical_bytes
from marketlab.core.clock import Clock
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant
from marketlab.memory.store import Episode
from marketlab.storage.base import Base, HashStr, InstantStr, JsonStr
from marketlab.storage.blobs import BlobStore

__all__ = [
    "DEFAULT_REFLECTION_INTERVAL",
    "MIN_EPISODES_TO_REFLECT",
    "Reflection",
    "ReflectionEngine",
    "ReflectionRow",
    "StrategyRule",
    "derive_rules",
]

DEFAULT_REFLECTION_INTERVAL: Final = 5
"""Cycles between reflections. A placeholder magnitude — see ``docs/ROADMAP.md``."""

MIN_EPISODES_TO_REFLECT: Final = 3
"""Below this, every rule would be an observation about noise."""

_PERSISTENCE_THRESHOLD: Final = 0.75
"""Fraction of recent decisions on one side before persistence is worth naming."""

_STABLE_PROBABILITY_SPREAD: Final = 0.02
"""Standard deviation below which stated probabilities are barely moving."""


@dataclass(frozen=True, slots=True)
class StrategyRule:
    """One distilled observation about a condition's own behaviour."""

    rule_id: str
    statement: str
    support: int
    """How many episodes the observation rests on."""


@dataclass(frozen=True, slots=True)
class Reflection:
    """What one reflection produced."""

    reflection_id: str
    scope_id: str
    as_of: Instant
    rules: tuple[StrategyRule, ...]

    @property
    def is_empty(self) -> bool:
        return not self.rules


class ReflectionRow(Base):
    """One stored reflection. Append-only."""

    __tablename__ = "reflections"

    reflection_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    scope_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    as_of: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    recorded_at: Mapped[str] = mapped_column(InstantStr, nullable=False)
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_blob_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    episode_ids_json: Mapped[str] = mapped_column(JsonStr, nullable=False)
    """Exactly which episodes this reflection was drawn from, so a replay can
    check it was not derived from anything dated after its own cutoff."""


class ReflectionEngine:
    """Produces, stores and retrieves reflections."""

    __slots__ = ("_blobs", "_clock", "_session")

    def __init__(self, session: Session, clock: Clock, blobs: BlobStore) -> None:
        self._session = session
        self._clock = clock
        self._blobs = blobs

    def reflect(
        self, scope_id: str, episodes: Sequence[Episode], *, as_of: Instant
    ) -> Reflection | None:
        """Distil ``episodes`` into rules and store the result.

        Returns ``None`` when there is too little history to say anything —
        an empty reflection would still be *material*, and handing a condition
        a page saying nothing would confound "reflection" with "more text".

        The caller supplies the episodes rather than this method querying for
        them: they come from :meth:`marketlab.memory.store.MemoryStore.recall`,
        which is the one place the strictly-before cutoff is enforced, and
        re-querying here would be a second chance to get that wrong.
        """
        if len(episodes) < MIN_EPISODES_TO_REFLECT:
            return None
        rules = derive_rules(episodes)
        if not rules:
            return None

        reflection = Reflection(
            reflection_id=derive_id(IdKind.REFLECTION, scope_id=scope_id, as_of=str(as_of)),
            scope_id=scope_id,
            as_of=as_of,
            rules=rules,
        )
        existing = self._session.get(ReflectionRow, reflection.reflection_id)
        if existing is not None:
            return self._load(existing)

        blob = self._blobs.put(canonical_bytes(_to_payload(reflection)))
        self._session.add(
            ReflectionRow(
                reflection_id=reflection.reflection_id,
                scope_id=scope_id,
                as_of=str(as_of),
                recorded_at=str(self._clock.now_instant()),
                rule_count=len(rules),
                payload_blob_hash=blob.digest,
                episode_ids_json=json.dumps([episode.episode_id for episode in episodes]),
            )
        )
        self._session.flush()
        return reflection

    def latest(self, scope_id: str, *, before: Instant) -> Reflection | None:
        """The most recent reflection dated strictly before ``before``.

        Same strict inequality as recall, for the same reason: a resumed cycle
        must not be handed a reflection produced during the cycle it is
        currently deciding.
        """
        row = self._session.execute(
            select(ReflectionRow)
            .where(ReflectionRow.scope_id == scope_id)
            .where(ReflectionRow.as_of < str(before))
            .order_by(ReflectionRow.as_of.desc(), ReflectionRow.reflection_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return self._load(row) if row is not None else None

    def _load(self, row: ReflectionRow) -> Reflection:
        payload = json.loads(self._blobs.get(row.payload_blob_hash))
        return Reflection(
            reflection_id=row.reflection_id,
            scope_id=row.scope_id,
            as_of=Instant(row.as_of),
            rules=tuple(
                StrategyRule(
                    rule_id=str(entry["rule_id"]),
                    statement=str(entry["statement"]),
                    support=int(entry["support"]),
                )
                for entry in payload["rules"]
            ),
        )


# -- the rules ----------------------------------------------------------------


def derive_rules(episodes: Sequence[Episode]) -> tuple[StrategyRule, ...]:
    """Closed-form observations about a condition's own record.

    Pure: no database, no clock, no randomness. Two identical histories
    produce byte-identical rules, which is what makes a reflection replayable.
    """
    rules: list[StrategyRule] = []
    rules.extend(_side_persistence(episodes))
    rules.extend(_side_reversals(episodes))
    rules.extend(_probability_stability(episodes))
    rules.extend(_recurring_failures(episodes))
    return tuple(rules)


def _rule(statement: str, support: int) -> StrategyRule:
    return StrategyRule(
        rule_id=derive_id(IdKind.STRATEGY_RULE, statement=statement),
        statement=statement,
        support=support,
    )


def _side_persistence(episodes: Sequence[Episode]) -> list[StrategyRule]:
    """Naming a position you have held without re-examining it."""
    by_instrument: dict[str, list[str]] = {}
    for episode in episodes:
        for intent in episode.intents:
            by_instrument.setdefault(intent.instrument_id, []).append(intent.side)

    rules: list[StrategyRule] = []
    for instrument_id in sorted(by_instrument):
        sides = by_instrument[instrument_id]
        if len(sides) < MIN_EPISODES_TO_REFLECT:
            continue
        side, count = Counter(sides).most_common(1)[0]
        if count / len(sides) >= _PERSISTENCE_THRESHOLD:
            rules.append(
                _rule(
                    f"You have taken {side} on {instrument_id} in {count} of your last "
                    f"{len(sides)} decisions on it. Check whether the evidence in this "
                    "snapshot still supports that, rather than repeating it by default.",
                    count,
                )
            )
    return rules


def _side_reversals(episodes: Sequence[Episode]) -> list[StrategyRule]:
    """Naming the opposite failure: churning without new information."""
    by_instrument: dict[str, list[str]] = {}
    for episode in episodes:
        for intent in episode.intents:
            by_instrument.setdefault(intent.instrument_id, []).append(intent.side)

    rules: list[StrategyRule] = []
    for instrument_id in sorted(by_instrument):
        sides = by_instrument[instrument_id]
        reversals = sum(1 for previous, current in pairwise(sides) if previous != current)
        if len(sides) >= MIN_EPISODES_TO_REFLECT and reversals >= len(sides) - 1 and reversals > 1:
            rules.append(
                _rule(
                    f"You have reversed your side on {instrument_id} at every one of your "
                    f"last {reversals} opportunities. Frequent reversal costs the spread "
                    "each time; make sure each one is driven by new evidence.",
                    reversals,
                )
            )
    return rules


def _probability_stability(episodes: Sequence[Episode]) -> list[StrategyRule]:
    """Probabilities that never move are not really forecasts."""
    probabilities = [
        forecast.probability_up for episode in episodes for forecast in episode.forecasts
    ]
    if len(probabilities) < MIN_EPISODES_TO_REFLECT:
        return []
    spread = pstdev(probabilities)
    mean = fmean(probabilities)
    if spread < _STABLE_PROBABILITY_SPREAD:
        return [
            _rule(
                f"Your last {len(probabilities)} stated probabilities sit within "
                f"{spread:.3f} of {mean:.3f}. A forecast that never moves carries little "
                "information; say so explicitly when the evidence is genuinely neutral.",
                len(probabilities),
            )
        ]
    return []


def _recurring_failures(episodes: Sequence[Episode]) -> list[StrategyRule]:
    counts = Counter(kind for episode in episodes for kind in episode.failure_kinds)
    return [
        _rule(
            f"{count} of your last {len(episodes)} decisions were recorded as {kind}. "
            "Whatever produced that is costing you decisions, not just tidiness.",
            count,
        )
        for kind, count in sorted(counts.items())
        if count >= 2
    ]


def _to_payload(reflection: Reflection) -> dict[str, Any]:
    return {
        "scope_id": reflection.scope_id,
        "as_of": str(reflection.as_of),
        "rules": [
            {"rule_id": rule.rule_id, "statement": rule.statement, "support": rule.support}
            for rule in reflection.rules
        ],
    }
