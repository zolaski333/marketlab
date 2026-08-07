"""Execution order of the arms within one cycle (§13.4, §30.3).

Why the order is not simply A, B, C, D, B', C'
-----------------------------------------------
The arms of a cycle share a machine, a provider, a rate limiter and a wall
clock. Anything that drifts over the few minutes a cycle takes — provider-side
load, a model version rolling out mid-cycle, a cache warming up — lands on
whichever arm happens to run last. Held fixed, that drift is perfectly
confounded with the condition. Varying the order per cycle turns a systematic
bias into noise that averages out, which is the entire reason experimental
designs bother with order at all.

Two policies, both fully deterministic
--------------------------------------
``LATIN_SQUARE``
    Cyclic rotation by the cycle index. Over any *n* consecutive cycles (with
    *n* execution units), every unit occupies every position exactly once —
    exact balance, not balance in expectation. This is the default: with the
    cycle counts a study of this kind realistically reaches, exact balance
    beats a random assignment that may happen to be lopsided.
``RANDOMIZED``
    A seeded shuffle, for when balance must not be predictable from the cycle
    index alone.

Both are reproducible from ``(seed, cycle_index)``, because §12.5 requires a
replay to reconstruct the original run — including the order it ran in.

Why the shuffle is not :mod:`random`
------------------------------------
:class:`random.Random` guarantees reproducibility of ``random()`` for a given
seed, but the *algorithms built on it* — ``shuffle``, ``sample`` — are
implementation details that have changed between CPython versions before.
Reproducibility here has to survive an interpreter upgrade years after the
data was collected, so the shuffle is a Fisher-Yates over an explicit SHA-256
key stream with rejection sampling: a fixed, written-down construction that
depends on nothing but the hash function.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from enum import StrEnum
from typing import Final

from marketlab.core.failures import ConfigurationError

__all__ = ["OrderPolicy", "execution_order"]

_WORD_BYTES: Final = 4
_WORD_MAX: Final = 1 << (8 * _WORD_BYTES)


class OrderPolicy(StrEnum):
    """How the execution order of one cycle's units is decided."""

    LATIN_SQUARE = "LATIN_SQUARE"
    RANDOMIZED = "RANDOMIZED"


def execution_order[T](
    units: Sequence[T],
    *,
    policy: OrderPolicy,
    cycle_index: int,
    seed: str,
) -> tuple[T, ...]:
    """Return ``units`` in the order this cycle should execute them.

    Args:
        units: the (arm, repetition) units to order. Order of the input is the
            canonical declaration order; it is never the execution order.
        policy: see :class:`OrderPolicy`.
        cycle_index: 0-based position of this cycle within its run.
        seed: run-level seed material, mixed into the randomised policy.

    Raises:
        ConfigurationError: if ``cycle_index`` is negative. A negative index
            would make the rotation wrap backwards and silently produce a
            different balance than the one the design assumes.
    """
    if cycle_index < 0:
        raise ConfigurationError(
            f"cycle_index must be non-negative, got {cycle_index}", cycle_index=cycle_index
        )
    if len(units) <= 1:
        return tuple(units)

    if policy is OrderPolicy.LATIN_SQUARE:
        count = len(units)
        return tuple(units[(position + cycle_index) % count] for position in range(count))

    stream = _key_stream(f"{seed}|cycle={cycle_index}")
    shuffled = list(units)
    for index in range(len(shuffled) - 1, 0, -1):
        swap_with = _uniform_below(stream, index + 1)
        shuffled[index], shuffled[swap_with] = shuffled[swap_with], shuffled[index]
    return tuple(shuffled)


def _key_stream(seed_material: str) -> Iterator[int]:
    """Yield an endless deterministic byte stream derived from ``seed_material``."""
    counter = 0
    while True:
        block = hashlib.sha256(f"{seed_material}#{counter}".encode()).digest()
        yield from block
        counter += 1


def _uniform_below(stream: Iterator[int], bound: int) -> int:
    """Draw uniformly from ``range(bound)``.

    Rejection sampling rather than a plain modulo: taking ``value % bound``
    over the full 32-bit range biases the low indices whenever ``bound`` does
    not divide 2**32, which for six arms would tilt the shuffle towards a
    particular ordering in a way nobody would notice by reading the output.
    """
    if bound <= 1:
        return 0
    limit = (_WORD_MAX // bound) * bound
    while True:
        value = 0
        for _ in range(_WORD_BYTES):
            value = (value << 8) | next(stream)
        if value < limit:
            return value % bound
