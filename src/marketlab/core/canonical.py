"""Canonical JSON serialisation.

Every scientific hash in the platform — snapshot manifests, decision bundles,
event payloads, the audit chain — is taken over the output of this module. The
only property that matters is that *the same logical value always produces the
same bytes*, on any machine, in any process, in any Python run.

Design choices that determinism depends on:

* **Keys sorted** at every depth, so dict insertion order is irrelevant.
* **No insignificant whitespace** (``separators=(",", ":")``).
* **UTF-8 preserved** rather than ``\\uXXXX``-escaped, with the result encoded to
  bytes exactly once, at the hashing boundary.
* **Decimals as plain strings** via :func:`decimal_to_str`, never floats: a
  Decimal that round-trips through binary floating point is no longer the same
  number (§17.2).
* **NaN/Infinity rejected.** Standard ``json`` emits the non-standard literals
  ``NaN``/``Infinity``, which are not valid JSON and whose presence in a hashed
  payload signals a computation that already went wrong.
* **Set iteration order never observed.** Sets are rejected rather than sorted,
  because their element ordering is not part of the value the caller intended.

The stdlib ``json`` encoder is used deliberately in preference to a faster
third-party encoder: this is a correctness-critical, low-volume path, and full
control over the emitted bytes is worth more than throughput.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from marketlab.core.instants import instant_from_datetime
from marketlab.core.money import Money, decimal_to_str

__all__ = [
    "canonical_bytes",
    "canonical_hash",
    "canonical_json",
    "to_canonical_value",
]


def to_canonical_value(value: Any) -> Any:
    """Reduce ``value`` to JSON primitives with deterministic ordering."""
    # bool must precede int: bool is a subclass of int.
    if value is None or isinstance(value, bool | str | int):
        return value

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(
                f"Non-finite float rejected by canonical JSON: {value!r}. "
                "A NaN or infinity in a hashed payload indicates an upstream "
                "computation error."
            )
        return value

    if isinstance(value, Decimal):
        return decimal_to_str(value)

    if isinstance(value, Money):
        return {"amount": value.to_str(), "currency": value.currency}

    if isinstance(value, datetime):
        return str(instant_from_datetime(value))

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Enum):
        return to_canonical_value(value.value)

    # Pydantic models and anything else exposing a dict projection.
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return to_canonical_value(dump(mode="python"))

    if isinstance(value, Mapping):
        items: list[tuple[str, Any]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"Canonical JSON requires string keys, got {type(key).__name__}. "
                    "Non-string keys have no stable ordering across runs."
                )
            items.append((key, to_canonical_value(item)))
        items.sort(key=lambda pair: pair[0])
        return dict(items)

    if isinstance(value, set | frozenset):
        raise TypeError(
            "Sets are rejected by canonical JSON: their iteration order is not "
            "part of the intended value. Convert to a sorted list explicitly."
        )

    if isinstance(value, Sequence):
        return [to_canonical_value(item) for item in value]

    raise TypeError(
        f"No canonical representation for {type(value).__name__}. Add an explicit "
        "rule rather than relying on repr()."
    )


def canonical_json(value: Any) -> str:
    """Serialise ``value`` to its canonical JSON text."""
    return json.dumps(
        to_canonical_value(value),
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    """Serialise ``value`` to canonical JSON encoded as UTF-8."""
    return canonical_json(value).encode("utf-8")


def canonical_hash(value: Any) -> str:
    """SHA-256 of the canonical JSON encoding of ``value``."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
