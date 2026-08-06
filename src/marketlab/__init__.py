"""MarketLab — prospective, reproducible study of decision-making LLM agents.

The platform observes how autonomous LLM agents behave in a *virtual*
multi-asset market, comparing arms that differ in what memory and strategic
reflection they carry into each decision.

No component of this system places a real order, connects to a real brokerage
account, or moves real money. All capital, positions and executions are virtual
(§2.2, §34.15).

Package layout follows the scientific pipeline:

``core``
    Determinism primitives: canonical instants, exact money, canonical JSON,
    injectable clocks, derived identifiers, failure taxonomy.
``storage`` / ``audit``
    Content-addressed blobs, the append-only event store, the hash chain.
``instruments`` / ``ingestion`` / ``transformations``
    The instrument reference data, provider adapters, and versioned derivations.
``snapshots`` / ``retrieval``
    The frozen exogenous snapshot and the frozen search index and tools.
``models`` / ``agents`` / ``experiments``
    The provider-independent model interface, the agent loop, and the arms.
``execution`` / ``accounting``
    Virtual execution and the double-entry ledger.
``memory`` / ``reflection`` / ``forecasting``
    Episodes and rules, scheduled reflection, the imposed forecast panel.
``evaluation`` / ``analysis``
    Resolution, metrics, and the pre-registered statistical plan.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
