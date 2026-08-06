"""Typed, budgeted agent tools over a frozen retrieval index (§10.3, §10.6).

Every tool call costs budget — hit or empty result, it still counts: comparing
arms fairly means comparing what they were *allowed* to spend, not just what
came back useful (see :mod:`marketlab.retrieval.budget`). Tools never raise on
a lookup miss (unknown instrument, no matching news) — they return ``None`` or
an empty tuple, exactly like
:meth:`marketlab.instruments.repository.InstrumentRepository.resolve`. A model
asking about something absent from the snapshot is an ordinary, expected
outcome, not a platform failure; only exhausting the budget raises.

This module, and everything it imports, must never import SQLAlchemy — see
``tests/security/test_decision_path_isolation.py``. A :class:`RetrievalIndex`
is already fully point-in-time filtered by the time it reaches here (built by
:mod:`marketlab.snapshots.builder`, which does hold a database session), so
there is no cutoff parameter to thread through these methods and no query
left to write that could forget it.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Final

from marketlab.instruments.types import InstrumentView
from marketlab.retrieval.budget import ToolBudget
from marketlab.retrieval.types import Evidence, EvidenceKind, RetrievalIndex

__all__ = ["DEFAULT_SEARCH_LIMIT", "RetrievalToolkit"]

DEFAULT_SEARCH_LIMIT: Final = 10


def _char_cost(payload: object) -> int:
    """A deterministic proxy for how much text a tool result hands the agent.

    Not token-accurate — that depends on the eventual model's tokenizer, which
    this layer must stay independent of (§12.1). Character count is a stable,
    provider-independent stand-in until a real cost model exists (task #4,
    still open — see ``docs/ROADMAP.md``).
    """
    if payload is None:
        return 0
    if isinstance(payload, Evidence | InstrumentView):
        return len(json.dumps(asdict(payload), default=str, sort_keys=True))
    if isinstance(payload, tuple):
        return sum(_char_cost(item) for item in payload)
    raise TypeError(f"_char_cost: unsupported payload type {type(payload).__name__}")


class RetrievalToolkit:
    """The typed tool surface bound to one frozen index and one decision's budget.

    One instance per decision: the budget is consumed across every call the
    model makes while producing a single bundle, then discarded. Every method
    here is a candidate for direct exposure as a model tool schema (task 8);
    none of them touch the database or the model provider.
    """

    __slots__ = ("_budget", "_index")

    def __init__(self, index: RetrievalIndex, budget: ToolBudget) -> None:
        self._index = index
        self._budget = budget

    @property
    def index(self) -> RetrievalIndex:
        return self._index

    @property
    def budget(self) -> ToolBudget:
        return self._budget

    def get_price_quote(self, instrument_id: str) -> Evidence | None:
        """Latest ``PRICE_BAR`` evidence for ``instrument_id`` as of the cutoff."""
        result = self._index.latest(EvidenceKind.PRICE_BAR, subject_id=instrument_id)
        self._budget.charge(chars=_char_cost(result))
        return result

    def search_news(
        self,
        query: str = "",
        *,
        instrument_id: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> tuple[Evidence, ...]:
        """News evidence, optionally narrowed to one instrument and/or a keyword."""
        matches = self._index.evidence_of_kind(EvidenceKind.NEWS_ITEM, subject_id=instrument_id)
        needle = query.strip().lower()
        if needle:
            matches = tuple(
                item
                for item in matches
                if needle in item.headline.lower()
                or any(needle in v.lower() for v in item.fields.values() if isinstance(v, str))
            )
        # Oldest-first is evidence_of_kind's natural order, but a growing
        # cumulative snapshot means truncating from the front would silently
        # drop the most recent, most relevant items first as the run gets
        # longer — the opposite of what a limited search should keep.
        result = tuple(matches)[-limit:] if limit > 0 else ()
        self._budget.charge(chars=_char_cost(result))
        return result

    def get_macro_indicator(self, indicator_id: str) -> Evidence | None:
        """Latest revision of ``indicator_id`` visible as of the cutoff (§6.2)."""
        result = self._index.latest(EvidenceKind.MACRO_RECORD, subject_id=indicator_id)
        self._budget.charge(chars=_char_cost(result))
        return result

    def get_fx_rate(self, pair: str) -> Evidence | None:
        """Latest rate for ``pair`` (e.g. ``"EUR_USD"``) visible as of the cutoff."""
        result = self._index.latest(EvidenceKind.FX_RATE, subject_id=pair)
        self._budget.charge(chars=_char_cost(result))
        return result

    def get_corporate_actions(self, instrument_id: str) -> tuple[Evidence, ...]:
        """Every corporate action on ``instrument_id`` visible as of the cutoff."""
        result = self._index.evidence_of_kind(
            EvidenceKind.CORPORATE_ACTION, subject_id=instrument_id
        )
        self._budget.charge(chars=_char_cost(result))
        return result

    def search_instruments(
        self, query: str, *, limit: int = DEFAULT_SEARCH_LIMIT
    ) -> tuple[InstrumentView, ...]:
        """Ticker/name discovery within the frozen universe.

        For orientation only — never for order resolution, which must go
        through the exact, non-fuzzy ``instrument_id`` lookup (§7.2).
        """
        result = self._index.search_instruments(query)[:limit]
        self._budget.charge(chars=_char_cost(result))
        return result
