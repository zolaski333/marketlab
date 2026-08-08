"""Reproducible randomness that survives an interpreter upgrade (§12.5, §21.4).

Two places in this platform need to draw at random and be able to draw the
*same* thing again years later: the arm execution order (§13.4) and the block
bootstrap (§21.4). Both are scientific artefacts — a replay that reshuffles the
arms has not replayed the run, and a bootstrap interval that cannot be
recomputed cannot be checked.

Why not :mod:`random`
---------------------
:class:`random.Random` guarantees reproducibility of ``random()`` for a given
seed. It does **not** guarantee that ``shuffle``, ``sample`` or ``choices``
consume that stream the same way forever; those are implementation details and
have changed between CPython versions before. A replay may run on a different
interpreter than the run it is checking, which makes that guarantee the wrong
shape for this platform.

So the construction is written down instead of imported: a SHA-256 counter
stream, big-endian 32-bit words, rejection sampling for the uniform draw, and
a Fisher-Yates that walks from the end. Nothing here depends on anything but
the hash function, and all of it is pinned by tests.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from typing import Final

from marketlab.core.failures import ConfigurationError

__all__ = ["DeterministicRng"]

_WORD_BYTES: Final = 4
_WORD_MAX: Final = 1 << (8 * _WORD_BYTES)


class DeterministicRng:
    """A draw sequence fixed entirely by its seed material.

    Stateful by design: successive draws consume successive bytes of the
    stream, so two callers that want independent sequences take two instances
    with different seed material rather than sharing one.
    """

    __slots__ = ("_seed_material", "_stream")

    def __init__(self, seed_material: str) -> None:
        self._seed_material = seed_material
        self._stream = _key_stream(seed_material)

    @property
    def seed_material(self) -> str:
        return self._seed_material

    def below(self, bound: int) -> int:
        """Draw uniformly from ``range(bound)``.

        Rejection sampling rather than a plain modulo: ``value % bound`` over
        the full 32-bit range biases the low values whenever ``bound`` does not
        divide 2**32 — a tilt nobody would notice by reading the output, and
        one that would quietly bias every bootstrap block start.
        """
        if bound < 1:
            raise ConfigurationError(f"bound must be >= 1, got {bound}", bound=bound)
        if bound == 1:
            return 0
        limit = (_WORD_MAX // bound) * bound
        while True:
            value = 0
            for _ in range(_WORD_BYTES):
                value = (value << 8) | next(self._stream)
            if value < limit:
                return value % bound

    def draws_below(self, bound: int, count: int) -> tuple[int, ...]:
        """``count`` independent draws from ``range(bound)``."""
        if count < 0:
            raise ConfigurationError(f"count must be >= 0, got {count}", count=count)
        return tuple(self.below(bound) for _ in range(count))

    def shuffled[T](self, items: Sequence[T]) -> tuple[T, ...]:
        """Fisher-Yates, walking from the last position down."""
        result = list(items)
        for index in range(len(result) - 1, 0, -1):
            swap_with = self.below(index + 1)
            result[index], result[swap_with] = result[swap_with], result[index]
        return tuple(result)


def _key_stream(seed_material: str) -> Iterator[int]:
    """An endless deterministic byte stream derived from ``seed_material``."""
    counter = 0
    while True:
        block = hashlib.sha256(f"{seed_material}#{counter}".encode()).digest()
        yield from block
        counter += 1
