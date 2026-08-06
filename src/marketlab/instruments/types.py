"""Instrument value types (§7.1).

Kept separate from the ORM rows in :mod:`marketlab.instruments.repository` so
that the decision path (agents, retrieval, forecasting — see
:mod:`marketlab.core.cutoff`) can depend on these plain dataclasses without
importing SQLAlchemy at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from marketlab.core.instants import Instant

__all__ = [
    "AssetClass",
    "ExecutionModel",
    "InstrumentStatus",
    "InstrumentView",
    "TradabilityStatus",
]


class AssetClass(StrEnum):
    """Instrument category (§2.1).

    Fixed at admission and never changes across versions: a change of asset
    class describes a different instrument, not a new version of this one.
    """

    EQUITY = "EQUITY"
    ETF = "ETF"
    CRYPTO_SPOT = "CRYPTO_SPOT"
    FX_SPOT = "FX_SPOT"
    PRECIOUS_METAL_SPOT = "PRECIOUS_METAL_SPOT"


class InstrumentStatus(StrEnum):
    """Lifecycle status of the instrument as a tradable entity."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"
    EXPIRED = "EXPIRED"


class ExecutionModel(StrEnum):
    """Execution quality level available for this instrument (§16.5).

    ``UNSUPPORTED`` does not mean the instrument cannot be researched — it
    means no honest fill can be constructed for it, so orders are refused
    while forecasts remain possible (§7.5's ``RESEARCH_ONLY``).
    """

    LEVEL_A_REAL_QUOTES = "LEVEL_A_REAL_QUOTES"
    LEVEL_B_VALIDATED_MODEL = "LEVEL_B_VALIDATED_MODEL"
    UNSUPPORTED = "UNSUPPORTED"


class TradabilityStatus(StrEnum):
    """Daily-computed eligibility (§7.5).

    Distinct from :class:`InstrumentStatus`: this is recomputed every cycle
    from that day's conditions, never stored as part of the instrument's
    identity.
    """

    TRADABLE = "TRADABLE"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    SUSPENDED = "SUSPENDED"
    STALE_DATA = "STALE_DATA"
    UNVALUABLE = "UNVALUABLE"
    EXPIRED = "EXPIRED"
    DELISTED = "DELISTED"


@dataclass(frozen=True, slots=True)
class InstrumentView:
    """A read-only projection of one instrument as of a specific version.

    This, not the ORM row, is what the rest of the platform passes around —
    keeping SQLAlchemy types out of every package that is not ``storage`` or
    ``instruments`` itself.
    """

    instrument_id: str
    asset_class: AssetClass
    version_number: int
    ticker: str
    name: str
    quote_currency: str
    native_timezone: str
    calendar_code: str
    settlement_days: int
    status: InstrumentStatus
    execution_model: ExecutionModel
    effective_from: Instant
