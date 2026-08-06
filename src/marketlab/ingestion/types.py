"""Raw provider records and provider interfaces (§8.1, §8.2).

Every field a raw record carries is a field the point-in-time machinery
depends on later: ``first_seen_at`` is what a
:class:`~marketlab.core.cutoff.Cutoff` actually filters on — §6.2 makes
availability a function of when the platform received the data, never of the
source's claimed publication time.

These types are provider-independent. Nothing downstream should import
:mod:`marketlab.ingestion.synthetic` except the code that wires up a study's
configuration; everything else — the ingestion pipeline, and eventually the
snapshot builder — depends on these dataclasses and protocols instead, so a
real provider adapter can be substituted in Phase 3 without touching a single
consumer (§8.1: "le cœur ne doit dépendre d'aucun fournisseur spécifique").

``FundamentalProvider`` from §8.1's list is deliberately absent: nothing in
Phase 1 consumes fundamentals data, and adding an unused protocol now would be
exactly the premature abstraction §34.3 warns against. Adding it later is a
pure addition, not a redesign, since every other provider here follows the
same one-method-per-protocol shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from marketlab.core.instants import Instant

__all__ = [
    "CorporateActionProvider",
    "FxProvider",
    "MacroProvider",
    "MarketDataProvider",
    "NewsProvider",
    "RawCorporateAction",
    "RawFxRate",
    "RawMacroRecord",
    "RawNewsItem",
    "RawPriceBar",
]


@dataclass(frozen=True, slots=True)
class RawPriceBar:
    """One instrument's quote for one session, exactly as the provider gave it."""

    instrument_id: str
    as_of: Instant
    bid: Decimal
    ask: Decimal
    close: Decimal
    volume: int
    first_seen_at: Instant


@dataclass(frozen=True, slots=True)
class RawNewsItem:
    """One news document — never summarised or altered before storage (§8.6)."""

    news_id: str
    instrument_ids: tuple[str, ...]
    title: str
    body: str
    source_published_at: Instant
    first_seen_at: Instant


@dataclass(frozen=True, slots=True)
class RawMacroRecord:
    """One macroeconomic indicator observation, possibly a later revision (§6.2)."""

    indicator_id: str
    value: Decimal
    revision: int
    source_published_at: Instant
    first_seen_at: Instant


@dataclass(frozen=True, slots=True)
class RawFxRate:
    """A currency-pair rate, quoted as units of the quote currency per unit of
    the base currency (``pair`` like ``"EUR_USD"`` means USD per EUR)."""

    pair: str
    rate: Decimal
    as_of: Instant
    first_seen_at: Instant


@dataclass(frozen=True, slots=True)
class RawCorporateAction:
    """One corporate action event (§17.5).

    ``details`` holds the action-specific fields — ``amount_per_share`` for a
    dividend, ``split_ratio`` for a split, ``new_ticker`` for a ticker change —
    rather than a wide row of mostly-null columns.
    """

    instrument_id: str
    action_type: str
    effective_at: Instant
    details: Mapping[str, Any]
    first_seen_at: Instant


@runtime_checkable
class MarketDataProvider(Protocol):
    def fetch_price_bars(self, as_of: Instant) -> Sequence[RawPriceBar]: ...


@runtime_checkable
class NewsProvider(Protocol):
    def fetch_news(self, as_of: Instant) -> Sequence[RawNewsItem]: ...


@runtime_checkable
class MacroProvider(Protocol):
    def fetch_macro_records(self, as_of: Instant) -> Sequence[RawMacroRecord]: ...


@runtime_checkable
class FxProvider(Protocol):
    def fetch_fx_rates(self, as_of: Instant) -> Sequence[RawFxRate]: ...


@runtime_checkable
class CorporateActionProvider(Protocol):
    def fetch_corporate_actions(self, as_of: Instant) -> Sequence[RawCorporateAction]: ...
