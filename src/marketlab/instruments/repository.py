"""The instrument reference repository (§7).

Two guarantees this module exists to make impossible to violate:

**Stable identity, independent of ticker (§7.1).** ``instrument_id`` is fixed
at admission and never changes. A ticker change, a status change, or any other
revision to an instrument's attributes creates a *new version* — the old
version is never edited, only superseded (§P4). This is why
``InstrumentVersionRow`` has no mutable ``effective_to`` column: storing one
would mean going back to update the previous version's row the moment a new
one is written, which is exactly the in-place mutation append-only storage
forbids. Instead, "current as of a cutoff" is computed by taking the latest
version whose ``effective_from`` does not exceed that cutoff — the version
that follows it, if any, defines where its validity ends without either row
ever being touched again.

**Strict resolution, no fuzzy matching (§7.2).** :meth:`InstrumentRepository.resolve`
is the only method that may back a trade order, and it matches an exact
``instrument_id`` or nothing. A ticker string, a company name, or a
near-miss — including a model-hallucinated symbol — resolves to ``None``. The
caller is responsible for recording that as an agent failure
(``AgentFailureKind.UNRESOLVED_INSTRUMENT``); this module has no fallback path
that could turn a hallucination into a real position.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.core.clock import Clock
from marketlab.core.cutoff import Cutoff
from marketlab.core.failures import ConfigurationError
from marketlab.core.ids import IdKind, derive_id
from marketlab.core.instants import Instant
from marketlab.core.money import quantum_for
from marketlab.instruments.types import (
    AssetClass,
    ExecutionModel,
    InstrumentStatus,
    InstrumentView,
    TradabilityStatus,
)
from marketlab.storage.base import Base, HashStr, InstantStr, ShortStr

__all__ = [
    "InstrumentRepository",
    "InstrumentRow",
    "InstrumentVersionRow",
    "compute_tradability",
]


class InstrumentRow(Base):
    """The stable identity of an instrument. Never versioned; see module docstring."""

    __tablename__ = "instruments"

    instrument_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    asset_class: Mapped[str] = mapped_column(ShortStr, nullable=False)
    admitted_at: Mapped[str] = mapped_column(InstantStr, nullable=False)


class InstrumentVersionRow(Base):
    """One versioned snapshot of an instrument's mutable attributes."""

    __tablename__ = "instrument_versions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "version_number", name="uq_instrument_versions_seq"),
    )

    version_id: Mapped[str] = mapped_column(HashStr, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ticker: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    name: Mapped[str] = mapped_column(ShortStr, nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    native_timezone: Mapped[str] = mapped_column(ShortStr, nullable=False)
    calendar_code: Mapped[str] = mapped_column(ShortStr, nullable=False)
    settlement_days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(ShortStr, nullable=False)
    execution_model: Mapped[str] = mapped_column(ShortStr, nullable=False)
    effective_from: Mapped[str] = mapped_column(InstantStr, nullable=False)
    supersedes_version_id: Mapped[str | None] = mapped_column(HashStr, nullable=True)
    created_at: Mapped[str] = mapped_column(InstantStr, nullable=False)


def _to_view(version: InstrumentVersionRow, asset_class: AssetClass) -> InstrumentView:
    return InstrumentView(
        instrument_id=version.instrument_id,
        asset_class=asset_class,
        version_number=version.version_number,
        ticker=version.ticker,
        name=version.name,
        quote_currency=version.quote_currency,
        native_timezone=version.native_timezone,
        calendar_code=version.calendar_code,
        settlement_days=version.settlement_days,
        status=InstrumentStatus(version.status),
        execution_model=ExecutionModel(version.execution_model),
        effective_from=Instant(version.effective_from),
    )


class InstrumentRepository:
    """Admits, versions, and resolves instruments."""

    __slots__ = ("_clock", "_session")

    def __init__(self, session: Session, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    # -- admission -----------------------------------------------------

    def admit(
        self,
        *,
        asset_class: AssetClass,
        ticker: str,
        name: str,
        quote_currency: str,
        native_timezone: str,
        calendar_code: str,
        settlement_days: int,
        execution_model: ExecutionModel,
        at: Instant,
    ) -> InstrumentView:
        """Admit a new instrument (§7.3).

        Idempotent: admitting the same ``(asset_class, ticker, quote_currency,
        at)`` twice returns the existing instrument instead of creating a
        second one, so replaying an interrupted ingestion is safe (§30.6).

        For Phase 1's fixed synthetic universe, the admission policy is
        encoded structurally rather than as a separate configurable rules
        engine: every field §7.3 requires — an unambiguous identifier, a
        valuable currency, a calendar, a settlement convention, an execution
        model — is mandatory here, so an instrument cannot be admitted without
        them. The full policy engine that re-evaluates an *open, changing*
        universe is deferred to when real providers make the universe actually
        open (see ``docs/ROADMAP.md``).

        Raises:
            ConfigurationError: if ``settlement_days`` is negative, ``ticker``
                or ``name`` is blank, ``quote_currency`` is not a registered
                currency (§16.4 — no honest valuation, no admission), or
                ``native_timezone`` is not a real IANA zone.
        """
        if settlement_days < 0:
            raise ConfigurationError(f"settlement_days cannot be negative: {settlement_days}")
        if not ticker.strip() or not name.strip():
            raise ConfigurationError("Instrument ticker and name must be non-empty")
        try:
            quantum_for(quote_currency)
        except ValueError as exc:
            raise ConfigurationError(
                f"Cannot admit an instrument quoted in unregistered currency "
                f"{quote_currency!r}: no honest valuation is possible (§16.4)."
            ) from exc
        try:
            ZoneInfo(native_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(f"Unknown IANA timezone: {native_timezone!r}") from exc

        instrument_id = derive_id(
            IdKind.INSTRUMENT,
            asset_class=str(asset_class),
            ticker=ticker,
            quote_currency=quote_currency,
            admitted_at=str(at),
        )

        existing = self._session.get(InstrumentRow, instrument_id)
        if existing is not None:
            view = self.resolve(instrument_id, Cutoff(as_of=at))
            if view is None:  # pragma: no cover - defensive, would indicate corruption
                raise ConfigurationError(
                    f"Instrument {instrument_id} exists but has no version as of "
                    f"its own admission time {at}; the store is corrupt."
                )
            return view

        self._session.add(
            InstrumentRow(
                instrument_id=instrument_id, asset_class=str(asset_class), admitted_at=str(at)
            )
        )
        version = self._write_version(
            instrument_id=instrument_id,
            version_number=1,
            ticker=ticker,
            name=name,
            quote_currency=quote_currency,
            native_timezone=native_timezone,
            calendar_code=calendar_code,
            settlement_days=settlement_days,
            status=InstrumentStatus.ACTIVE,
            execution_model=execution_model,
            effective_from=at,
            supersedes_version_id=None,
        )
        self._session.commit()
        return _to_view(version, asset_class)

    # -- resolution ------------------------------------------------------

    def resolve(self, instrument_id: str, cutoff: Cutoff) -> InstrumentView | None:
        """Resolve an instrument by its exact internal id, as of ``cutoff``.

        This is the only method that may back an order: it never fuzzy-matches
        a ticker, a name, or a near-miss string. A trade intent supplying
        anything other than a real ``instrument_id`` must get ``None`` back
        and be recorded as an agent failure — never silently coerced into a
        real instrument (§7.2).
        """
        instrument = self._session.get(InstrumentRow, instrument_id)
        if instrument is None:
            return None
        versions = self._versions_as_of(instrument_id, cutoff)
        if not versions:
            return None
        return _to_view(versions[-1], AssetClass(instrument.asset_class))

    def search(self, query: str, cutoff: Cutoff) -> list[InstrumentView]:
        """Fuzzy discovery by ticker/name substring, for the ``search_instruments``
        tool. Never used to resolve an order — see :meth:`resolve`.

        Point-in-time correct: an instrument admitted after ``cutoff`` is
        invisible, and a ticker change after ``cutoff`` is not revealed early.
        """
        needle = query.strip().lower()
        results: list[InstrumentView] = []
        for instrument in self._session.execute(select(InstrumentRow)).scalars().all():
            view = self.resolve(instrument.instrument_id, cutoff)
            if view is None:
                continue
            if needle in view.ticker.lower() or needle in view.name.lower():
                results.append(view)
        results.sort(key=lambda v: v.instrument_id)
        return results

    def _versions_as_of(self, instrument_id: str, cutoff: Cutoff) -> list[InstrumentVersionRow]:
        all_versions = (
            self._session.execute(
                select(InstrumentVersionRow)
                .where(InstrumentVersionRow.instrument_id == instrument_id)
                .order_by(InstrumentVersionRow.version_number.asc())
            )
            .scalars()
            .all()
        )
        return [v for v in all_versions if cutoff.allows(Instant(v.effective_from))]

    # -- versioning --------------------------------------------------------

    def record_ticker_change(
        self, instrument_id: str, new_ticker: str, at: Instant
    ) -> InstrumentView:
        """Record a ticker change as a new version (§7.1, a ``TICKER_CHANGE``
        corporate action).

        No-op if the current ticker already equals ``new_ticker`` — what makes
        replaying an interrupted corporate-actions cycle safe (§30.6) instead
        of piling up redundant versions.
        """
        current, instrument = self._current_version(instrument_id)
        if current.ticker == new_ticker:
            return _to_view(current, AssetClass(instrument.asset_class))

        new_version = self._write_version(
            instrument_id=instrument_id,
            version_number=current.version_number + 1,
            ticker=new_ticker,
            name=current.name,
            quote_currency=current.quote_currency,
            native_timezone=current.native_timezone,
            calendar_code=current.calendar_code,
            settlement_days=current.settlement_days,
            status=InstrumentStatus(current.status),
            execution_model=ExecutionModel(current.execution_model),
            effective_from=at,
            supersedes_version_id=current.version_id,
        )
        self._session.commit()
        return _to_view(new_version, AssetClass(instrument.asset_class))

    def record_status_change(
        self, instrument_id: str, new_status: InstrumentStatus, at: Instant
    ) -> InstrumentView:
        """Record a status change (suspension, delisting, ...) as a new version."""
        current, instrument = self._current_version(instrument_id)
        if InstrumentStatus(current.status) == new_status:
            return _to_view(current, AssetClass(instrument.asset_class))

        new_version = self._write_version(
            instrument_id=instrument_id,
            version_number=current.version_number + 1,
            ticker=current.ticker,
            name=current.name,
            quote_currency=current.quote_currency,
            native_timezone=current.native_timezone,
            calendar_code=current.calendar_code,
            settlement_days=current.settlement_days,
            status=new_status,
            execution_model=ExecutionModel(current.execution_model),
            effective_from=at,
            supersedes_version_id=current.version_id,
        )
        self._session.commit()
        return _to_view(new_version, AssetClass(instrument.asset_class))

    def _current_version(self, instrument_id: str) -> tuple[InstrumentVersionRow, InstrumentRow]:
        instrument = self._session.get(InstrumentRow, instrument_id)
        if instrument is None:
            raise ConfigurationError(f"Unknown instrument: {instrument_id}")
        latest = (
            self._session.execute(
                select(InstrumentVersionRow)
                .where(InstrumentVersionRow.instrument_id == instrument_id)
                .order_by(InstrumentVersionRow.version_number.desc())
                .limit(1)
            )
            .scalars()
            .one()
        )
        return latest, instrument

    def _write_version(
        self,
        *,
        instrument_id: str,
        version_number: int,
        ticker: str,
        name: str,
        quote_currency: str,
        native_timezone: str,
        calendar_code: str,
        settlement_days: int,
        status: InstrumentStatus,
        execution_model: ExecutionModel,
        effective_from: Instant,
        supersedes_version_id: str | None,
    ) -> InstrumentVersionRow:
        version_id = derive_id(
            IdKind.INSTRUMENT_VERSION, instrument_id=instrument_id, version_number=version_number
        )
        row = InstrumentVersionRow(
            version_id=version_id,
            instrument_id=instrument_id,
            version_number=version_number,
            ticker=ticker,
            name=name,
            quote_currency=quote_currency,
            native_timezone=native_timezone,
            calendar_code=calendar_code,
            settlement_days=settlement_days,
            status=str(status),
            execution_model=str(execution_model),
            effective_from=str(effective_from),
            supersedes_version_id=supersedes_version_id,
            created_at=str(self._clock.now_instant()),
        )
        self._session.add(row)
        return row


def compute_tradability(
    view: InstrumentView,
    *,
    has_tradable_quote: bool,
    data_is_stale: bool,
) -> TradabilityStatus:
    """Daily eligibility computation (§7.5).

    Pure and deterministic: the same inputs always produce the same status,
    and this function never touches the database — callers gather
    ``has_tradable_quote``/``data_is_stale`` from that day's snapshot.

    Precedence (first match wins): identity-level status (delisted, expired,
    suspended) always dominates data-quality issues, which in turn dominate
    the execution-model gap that distinguishes ``RESEARCH_ONLY`` from
    ``TRADABLE`` — a suspended instrument is not "stale", it is suspended.
    """
    if view.status == InstrumentStatus.DELISTED:
        return TradabilityStatus.DELISTED
    if view.status == InstrumentStatus.EXPIRED:
        return TradabilityStatus.EXPIRED
    if view.status == InstrumentStatus.SUSPENDED:
        return TradabilityStatus.SUSPENDED
    if data_is_stale:
        return TradabilityStatus.STALE_DATA
    if not has_tradable_quote:
        return TradabilityStatus.UNVALUABLE
    if view.execution_model == ExecutionModel.UNSUPPORTED:
        return TradabilityStatus.RESEARCH_ONLY
    return TradabilityStatus.TRADABLE
