"""Deterministic identifier derivation.

Scientific identifiers are derived from content, never from a random source or
an autoincrement. Two properties follow, both required:

* **Idempotent resume (§16.7, §30.6).** Re-running an interrupted cycle
  recomputes the same identifiers, so a uniqueness constraint turns a duplicate
  write into a no-op instead of a second order.
* **Reproducible replay (§12.5).** A replay derives the same ids as the original
  run, so recomputed artefacts can be compared to recorded ones by key.

Two rules keep derivation safe:

1. **Namespacing.** The entity kind is part of the hashed input, so a decision
   bundle and a panel bundle built from identical parts cannot collide.
2. **Total discrimination.** Every field that distinguishes two entities must be
   in the parts. Omitting one silently merges distinct entities: a previous
   implementation derived ``bundle_id`` from ``(snapshot, condition)`` while
   omitting ``repetition``, which would have collapsed every repetition of an
   arm onto a single bundle the moment repetitions were enabled.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

from marketlab.core.canonical import canonical_bytes

__all__ = ["IdKind", "derive_id", "short_id"]


class IdKind(StrEnum):
    """Namespace for a derived identifier."""

    SNAPSHOT = "snapshot"
    SNAPSHOT_MEMBER = "snapshot_member"
    EVIDENCE = "evidence"
    CONDITION_CONTEXT = "condition_context"
    DECISION_BUNDLE = "decision_bundle"
    PANEL_BUNDLE = "panel_bundle"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    FORECAST = "forecast"
    FORECAST_RESOLUTION = "forecast_resolution"
    CLAIM = "claim"
    CONSIDERATION = "consideration"
    TRADE_INTENT = "trade_intent"
    ORDER = "order"
    FILL = "fill"
    LEDGER_TRANSACTION = "ledger_transaction"
    LEDGER_ENTRY = "ledger_entry"
    POSITION_EVENT = "position_event"
    CORPORATE_ACTION = "corporate_action"
    MEMORY_ITEM = "memory_item"
    EPISODE = "episode"
    STRATEGY_RULE = "strategy_rule"
    STRATEGY_RULE_VERSION = "strategy_rule_version"
    HYPOTHESIS = "hypothesis"
    REFLECTION = "reflection"
    EVENT = "event"
    FAILURE = "failure"
    AUDIT = "audit"
    SENTINEL_RESULT = "sentinel_result"
    INSTRUMENT = "instrument"
    INSTRUMENT_VERSION = "instrument_version"
    RETRIEVAL_INDEX = "retrieval_index"


def derive_id(kind: IdKind, **parts: Any) -> str:
    """Derive a stable hex identifier from ``kind`` and the given parts.

    Parts are canonicalised before hashing, so key order at the call site is
    irrelevant and nested structures are handled consistently.

    Raises:
        ValueError: if no parts are supplied. An identifier derived from the
            namespace alone would be the same for every instance of that kind.
    """
    if not parts:
        raise ValueError(
            f"derive_id({kind}) requires at least one discriminating part; "
            "a namespace-only id is identical for every entity of that kind."
        )
    payload = canonical_bytes({"kind": str(kind), "parts": parts})
    return hashlib.sha256(payload).hexdigest()


def short_id(identifier: str, length: int = 12) -> str:
    """Truncate an identifier for human-facing display only.

    Never use the result as a key: truncation reintroduces collision risk.
    """
    return identifier[:length]
