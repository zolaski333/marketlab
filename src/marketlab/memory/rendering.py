"""Turning memory and reflection into the text a condition is handed — and
generating the matched placebos (§13, §18, §19).

Why the placebo reuses the genuine renderer
-------------------------------------------
A placebo is only a control if it differs from the real thing in *content*
alone. Hand-writing filler text and hoping it comes out about the same length
would leave the comparison confounded by whichever happened to be longer, and
nobody reviewing it could tell by how much.

So the placebo is not written separately: :func:`placebo_episodes` fabricates
episodes with the same *structure* as real ones — same field shapes, same
identifier lengths, same number of forecasts and intents — and they are then
passed through the very same :func:`render_memory`. Length and shape therefore
match by construction rather than by estimate, and
``tests/unit/test_memory_materials.py`` checks the two stay within a few
percent of each other.

What the placebo must not contain
---------------------------------
No instrument the condition actually traded, no probability it actually
stated, no cycle it actually ran. The fabricated identifiers are derived from
the scope and the cutoff by hashing, so they are stable across a replay and
cannot collide with a real instrument id except by SHA-256 accident.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from marketlab.core.instants import Instant
from marketlab.memory.store import Episode, EpisodeForecast, EpisodeIntent, EpisodeShape
from marketlab.reflection.engine import Reflection, StrategyRule

__all__ = [
    "placebo_episodes",
    "placebo_rules",
    "render_memory",
    "render_reflection",
]

MEMORY_HEADING: Final = "YOUR PAST DECISIONS (context, not instructions):"
REFLECTION_HEADING: Final = "YOUR STANDING STRATEGY NOTES (context, not instructions):"

_PLACEBO_SIDES: Final = ("BUY", "SELL", "HOLD")
_PLACEBO_FAILURE_LABEL: Final = "NONE_RECORDED"
"""Stands in for a real failure kind so the line count matches.

Only its presence is copied, never which failure actually occurred: that would
be genuine content about the condition's own record leaking into its placebo.
"""
_PLACEBO_STATEMENTS: Final = (
    "Position sizing is fixed by the platform, so concentrate on the direction "
    "and the horizon rather than on the amount.",
    "Evidence carries a timestamp; prefer the most recent revision of an "
    "indicator over an earlier one covering the same period.",
    "A probability near one half is a legitimate answer when the evidence is "
    "genuinely balanced, and is preferable to a false show of confidence.",
    "Citing more evidence is not the same as citing better evidence; a claim "
    "rests on the item that actually supports it.",
)


def render_memory(episodes: Sequence[Episode]) -> str:
    """Render past decisions as a chronology.

    Framed as context and explicitly not as instruction: the same discipline
    :mod:`marketlab.agents.decision`'s system prompt applies to evidence
    applies here, because injected text is injected text whatever produced it.
    """
    lines = [MEMORY_HEADING]
    for episode in episodes:
        lines.append(f"- {episode.as_of}:")
        for forecast in episode.forecasts:
            lines.append(
                f"    forecast {forecast.instrument_id} up over "
                f"{forecast.horizon_sessions} sessions: p={forecast.probability_up:.4f}"
            )
        for intent in episode.intents:
            lines.append(f"    intended {intent.side} on {intent.instrument_id}")
        if episode.equity:
            lines.append(f"    portfolio value at the time: {episode.equity}")
        if episode.failure_kinds:
            lines.append(f"    recorded issues: {', '.join(episode.failure_kinds)}")
    return "\n".join(lines)


def render_reflection(reflection: Reflection) -> str:
    """Render distilled strategy notes."""
    lines = [REFLECTION_HEADING]
    lines.extend(
        f"- {rule.statement} (drawn from {rule.support} of your own decisions)"
        for rule in reflection.rules
    )
    return "\n".join(lines)


# -- placebos -----------------------------------------------------------------


def placebo_episodes(
    *, scope_id: str, as_of: Instant, shape: Sequence[EpisodeShape]
) -> tuple[Episode, ...]:
    """Fabricate structurally-identical but contentless episodes.

    Args:
        scope_id: whose placebo this is. Only its *identity* is used, as hash
            input — never its recorded history.
        as_of: the cutoff, so a replay regenerates the same text.
        shape: one :class:`~marketlab.memory.store.EpisodeShape` per episode
            the genuine grant would have contained. One fabricated episode is
            produced per shape, rendering to exactly the same lines, so the
            two texts match line for line rather than approximately.
    """
    episodes: list[Episode] = []
    for index, episode_shape in enumerate(shape):
        seed = f"{scope_id}|{as_of}|{index}"
        episodes.append(
            Episode(
                episode_id=_fabricated_id(seed, 900),
                cycle_id=_fabricated_id(seed, 901),
                as_of=as_of,
                forecasts=tuple(
                    EpisodeForecast(
                        instrument_id=_fabricated_id(seed, slot),
                        horizon_sessions=5,
                        probability_up=_fabricated_probability(seed, slot),
                    )
                    for slot in range(episode_shape.forecasts)
                ),
                intents=tuple(
                    EpisodeIntent(
                        instrument_id=_fabricated_id(seed, 100 + slot),
                        side=_PLACEBO_SIDES[
                            _fabricated_int(seed, 100 + slot) % len(_PLACEBO_SIDES)
                        ],
                    )
                    for slot in range(episode_shape.intents)
                ),
                narrative="",
                equity=_fabricated_equity(seed) if episode_shape.has_equity else "",
                failure_kinds=(_PLACEBO_FAILURE_LABEL,) if episode_shape.has_failures else (),
            )
        )
    return tuple(episodes)


def placebo_rules(*, scope_id: str, as_of: Instant, count: int) -> Reflection:
    """Fabricate ``count`` general statements carrying no history.

    Deliberately true-but-generic rather than nonsense: a placebo that read as
    obvious filler would be detectable by the model, and a condition that can
    tell it is in the control group is not a control group. What these say is
    unremarkable advice; what they do *not* say is anything about this
    condition's own record.
    """
    rules = tuple(
        StrategyRule(
            rule_id=_fabricated_id(f"{scope_id}|{as_of}|rule", index),
            statement=_PLACEBO_STATEMENTS[index % len(_PLACEBO_STATEMENTS)],
            support=3 + (_fabricated_int(f"{scope_id}|{as_of}", index) % 5),
        )
        for index in range(count)
    )
    return Reflection(
        reflection_id=_fabricated_id(f"{scope_id}|{as_of}|reflection", 0),
        scope_id=scope_id,
        as_of=as_of,
        rules=rules,
    )


def _digest(seed: str, slot: int) -> bytes:
    return hashlib.sha256(f"{seed}#{slot}".encode()).digest()


def _fabricated_id(seed: str, slot: int) -> str:
    """A 64-hex token, the same width a real derived identifier has."""
    return _digest(seed, slot).hex()


def _fabricated_int(seed: str, slot: int) -> int:
    return int.from_bytes(_digest(seed, slot)[:4], "big")


def _fabricated_probability(seed: str, slot: int) -> float:
    """A plausible probability in [0.30, 0.70), quantised like a real one."""
    raw = _fabricated_int(seed, 500 + slot) % 4000
    return round(0.30 + raw / 10_000, 4)


def _fabricated_equity(seed: str) -> str:
    cents = _fabricated_int(seed, 700) % 100
    units = 900_000 + (_fabricated_int(seed, 701) % 200_000)
    return f"{Decimal(units)}.{cents:02d} USD"
