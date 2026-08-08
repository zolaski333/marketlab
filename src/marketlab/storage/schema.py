"""Central registration of every persisted table.

SQLAlchemy's declarative base only knows about a table once the module defining
it has been imported. A table defined in a module nobody imported is absent from
``Base.metadata``, so ``create_all`` skips it and any protection keyed to its
name silently does nothing.

Importing every model module here — and importing *this* module before touching
the schema — makes that failure impossible: the metadata is complete by
construction, and :meth:`Database.create_schema` verifies that every table
listed in ``APPEND_ONLY_TABLES`` is actually present.

Tables are grouped by the pipeline stage that owns them, mirroring §27.
"""

from __future__ import annotations

from marketlab.accounting.ledger import LedgerEntryRow, LedgerTransactionRow
from marketlab.accounting.positions import PositionEventRow
from marketlab.audit.roots import DailyRootRow
from marketlab.evaluation.panels import PanelBundleRow
from marketlab.evaluation.resolution import ForecastResolutionRow
from marketlab.execution.corporate import CorporateActionApplicationRow
from marketlab.execution.engine import FillRow, OrderRejectionRow, OrderRow, SettlementRow
from marketlab.experiments.runner import DecisionBundleRow
from marketlab.instruments.repository import InstrumentRow, InstrumentVersionRow
from marketlab.memory.store import MemoryEpisodeRow
from marketlab.reflection.engine import ReflectionRow
from marketlab.snapshots.builder import SnapshotMemberRow, SnapshotRow
from marketlab.storage.base import Base
from marketlab.storage.blobs import BlobMetadataRow
from marketlab.storage.events import EventRow

__all__ = [
    "Base",
    "BlobMetadataRow",
    "CorporateActionApplicationRow",
    "DailyRootRow",
    "DecisionBundleRow",
    "EventRow",
    "FillRow",
    "ForecastResolutionRow",
    "InstrumentRow",
    "InstrumentVersionRow",
    "LedgerEntryRow",
    "LedgerTransactionRow",
    "MemoryEpisodeRow",
    "OrderRejectionRow",
    "OrderRow",
    "PanelBundleRow",
    "PositionEventRow",
    "ReflectionRow",
    "SettlementRow",
    "SnapshotMemberRow",
    "SnapshotRow",
    "table_names",
]


def table_names() -> list[str]:
    """Return every registered table name, sorted."""
    return sorted(Base.metadata.tables)
