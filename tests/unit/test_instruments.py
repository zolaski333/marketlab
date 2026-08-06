"""Tests for the instrument reference repository (§7.1, §7.2, §7.3, §7.5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from marketlab.core.clock import FrozenClock
from marketlab.core.cutoff import Cutoff
from marketlab.core.failures import ConfigurationError
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.instruments.repository import InstrumentRepository, compute_tradability
from marketlab.instruments.types import (
    AssetClass,
    ExecutionModel,
    InstrumentStatus,
    InstrumentView,
    TradabilityStatus,
)


def at(year: int, month: int, day: int, hour: int = 16) -> Instant:
    return instant_from_datetime(datetime(year, month, day, hour, tzinfo=UTC))


DAY1 = at(2026, 8, 3)
DAY2 = at(2026, 8, 4)
DAY3 = at(2026, 8, 5)


@pytest.fixture
def repo(session: Session, clock: FrozenClock) -> InstrumentRepository:
    return InstrumentRepository(session, clock)


def admit_alpha(repo: InstrumentRepository, at_instant: Instant = DAY1) -> InstrumentView:
    return repo.admit(
        asset_class=AssetClass.EQUITY,
        ticker="ALPHA",
        name="Alpha Corp",
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="SYNTH_US_EQUITY",
        settlement_days=2,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        at=at_instant,
    )


# -- admission -----------------------------------------------------------


def test_admission_creates_version_one(repo: InstrumentRepository) -> None:
    view = admit_alpha(repo)
    assert view.version_number == 1
    assert view.ticker == "ALPHA"
    assert view.status == InstrumentStatus.ACTIVE
    assert view.asset_class == AssetClass.EQUITY


def test_admission_is_idempotent(repo: InstrumentRepository) -> None:
    """Replaying an interrupted ingestion must not create a second instrument
    or a second version (§30.6)."""
    first = admit_alpha(repo)
    second = admit_alpha(repo)
    assert first == second


def test_distinct_tickers_are_distinct_instruments(repo: InstrumentRepository) -> None:
    alpha = admit_alpha(repo)
    beta = repo.admit(
        asset_class=AssetClass.EQUITY,
        ticker="BETA",
        name="Beta Corp",
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="SYNTH_US_EQUITY",
        settlement_days=2,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        at=DAY1,
    )
    assert alpha.instrument_id != beta.instrument_id


@pytest.mark.parametrize(
    "overrides,error_match",
    [
        ({"settlement_days": -1}, "cannot be negative"),
        ({"ticker": "  "}, "non-empty"),
        ({"name": ""}, "non-empty"),
        ({"quote_currency": "ZZZ"}, "unregistered currency"),
        ({"native_timezone": "Not/AZone"}, "Unknown IANA timezone"),
    ],
)
def test_admission_rejects_invalid_input(
    repo: InstrumentRepository, overrides: dict[str, object], error_match: str
) -> None:
    kwargs: dict[str, object] = {
        "asset_class": AssetClass.EQUITY,
        "ticker": "X",
        "name": "X Corp",
        "quote_currency": "USD",
        "native_timezone": "America/New_York",
        "calendar_code": "SYNTH_US_EQUITY",
        "settlement_days": 2,
        "execution_model": ExecutionModel.LEVEL_A_REAL_QUOTES,
        "at": DAY1,
    }
    kwargs.update(overrides)
    with pytest.raises(ConfigurationError, match=error_match):
        repo.admit(**kwargs)  # type: ignore[arg-type]


# -- resolution (§7.2) ----------------------------------------------------


def test_resolve_is_exact_only_no_fuzzy_match(repo: InstrumentRepository) -> None:
    """§7.2: resolve() must never turn a near-miss string into a real
    instrument — this is what stands between a hallucinated ticker and a
    position actually being opened."""
    view = admit_alpha(repo)
    assert repo.resolve(view.instrument_id, Cutoff(as_of=DAY2)) is not None
    assert repo.resolve("ALPHA", Cutoff(as_of=DAY2)) is None  # a ticker, not the id
    assert repo.resolve("alpha corp", Cutoff(as_of=DAY2)) is None  # a name
    assert repo.resolve("EQ_HALLUCINATED_XYZ", Cutoff(as_of=DAY2)) is None


def test_resolve_respects_the_cutoff(repo: InstrumentRepository) -> None:
    view = admit_alpha(repo, at_instant=DAY2)
    assert repo.resolve(view.instrument_id, Cutoff(as_of=DAY1)) is None  # before admission
    assert repo.resolve(view.instrument_id, Cutoff(as_of=DAY2)) is not None  # at admission
    assert repo.resolve(view.instrument_id, Cutoff(as_of=DAY3)) is not None  # after


def test_resolving_an_unknown_id_returns_none_not_an_error(repo: InstrumentRepository) -> None:
    assert repo.resolve("a" * 64, Cutoff(as_of=DAY2)) is None


# -- search ----------------------------------------------------------------


def test_search_finds_by_ticker_or_name_substring(repo: InstrumentRepository) -> None:
    admit_alpha(repo)
    by_ticker = repo.search("alp", Cutoff(as_of=DAY2))
    assert len(by_ticker) == 1
    assert by_ticker[0].ticker == "ALPHA"

    assert repo.search("corp", Cutoff(as_of=DAY2))
    assert repo.search("nonexistent", Cutoff(as_of=DAY2)) == []


def test_search_does_not_reveal_a_ticker_change_before_it_happened(
    repo: InstrumentRepository,
) -> None:
    view = admit_alpha(repo, at_instant=DAY1)
    repo.record_ticker_change(view.instrument_id, "ALPHA2", DAY2)

    assert repo.search("ALPHA2", Cutoff(as_of=DAY1)) == []
    assert len(repo.search("ALPHA2", Cutoff(as_of=DAY2))) == 1


def test_search_does_not_reveal_an_instrument_admitted_after_the_cutoff(
    repo: InstrumentRepository,
) -> None:
    admit_alpha(repo, at_instant=DAY3)
    assert repo.search("alpha", Cutoff(as_of=DAY1)) == []


# -- versioning (§7.1, §P4) ------------------------------------------------


def test_ticker_change_creates_a_new_version_and_preserves_the_old_one(
    repo: InstrumentRepository,
) -> None:
    view = admit_alpha(repo, at_instant=DAY1)
    updated = repo.record_ticker_change(view.instrument_id, "ALPHA2", DAY2)

    assert updated.version_number == 2
    assert updated.ticker == "ALPHA2"
    assert updated.instrument_id == view.instrument_id

    old = repo.resolve(view.instrument_id, Cutoff(as_of=DAY1))
    assert old is not None
    assert old.ticker == "ALPHA"
    assert old.version_number == 1


def test_instrument_id_is_stable_across_ticker_changes(repo: InstrumentRepository) -> None:
    view = admit_alpha(repo, at_instant=DAY1)
    updated = repo.record_ticker_change(view.instrument_id, "ALPHA2", DAY2)
    assert updated.instrument_id == view.instrument_id
    assert updated.asset_class == view.asset_class == AssetClass.EQUITY


def test_redundant_ticker_change_is_a_no_op(repo: InstrumentRepository) -> None:
    """Replaying an interrupted corporate-actions cycle must not pile up
    versions (§30.6)."""
    view = admit_alpha(repo, at_instant=DAY1)
    once = repo.record_ticker_change(view.instrument_id, "ALPHA2", DAY2)
    twice = repo.record_ticker_change(view.instrument_id, "ALPHA2", DAY3)
    assert once.version_number == twice.version_number == 2


def test_status_change_creates_a_new_version(repo: InstrumentRepository) -> None:
    view = admit_alpha(repo, at_instant=DAY1)
    updated = repo.record_status_change(view.instrument_id, InstrumentStatus.SUSPENDED, DAY2)
    assert updated.status == InstrumentStatus.SUSPENDED
    assert updated.version_number == 2

    still_active_before = repo.resolve(view.instrument_id, Cutoff(as_of=DAY1))
    assert still_active_before is not None
    assert still_active_before.status == InstrumentStatus.ACTIVE


def test_redundant_status_change_is_a_no_op(repo: InstrumentRepository) -> None:
    view = admit_alpha(repo, at_instant=DAY1)
    once = repo.record_status_change(view.instrument_id, InstrumentStatus.SUSPENDED, DAY2)
    twice = repo.record_status_change(view.instrument_id, InstrumentStatus.SUSPENDED, DAY3)
    assert once.version_number == twice.version_number == 2


def test_versioning_an_unknown_instrument_raises(repo: InstrumentRepository) -> None:
    with pytest.raises(ConfigurationError, match="Unknown instrument"):
        repo.record_ticker_change("a" * 64, "X", DAY2)


# -- tradability (§7.5) -----------------------------------------------------


def _view(status: InstrumentStatus, execution_model: ExecutionModel) -> InstrumentView:
    return InstrumentView(
        instrument_id="x",
        asset_class=AssetClass.EQUITY,
        version_number=1,
        ticker="X",
        name="X",
        quote_currency="USD",
        native_timezone="UTC",
        calendar_code="C",
        settlement_days=2,
        status=status,
        execution_model=execution_model,
        effective_from=DAY1,
    )


def test_tradability_delisted_dominates_everything() -> None:
    view = _view(InstrumentStatus.DELISTED, ExecutionModel.LEVEL_A_REAL_QUOTES)
    result = compute_tradability(view, has_tradable_quote=True, data_is_stale=False)
    assert result == TradabilityStatus.DELISTED


def test_tradability_suspended_dominates_data_quality() -> None:
    view = _view(InstrumentStatus.SUSPENDED, ExecutionModel.LEVEL_A_REAL_QUOTES)
    result = compute_tradability(view, has_tradable_quote=False, data_is_stale=True)
    assert result == TradabilityStatus.SUSPENDED


def test_tradability_stale_data() -> None:
    view = _view(InstrumentStatus.ACTIVE, ExecutionModel.LEVEL_A_REAL_QUOTES)
    result = compute_tradability(view, has_tradable_quote=True, data_is_stale=True)
    assert result == TradabilityStatus.STALE_DATA


def test_tradability_unvaluable_when_no_tradable_quote() -> None:
    view = _view(InstrumentStatus.ACTIVE, ExecutionModel.LEVEL_A_REAL_QUOTES)
    result = compute_tradability(view, has_tradable_quote=False, data_is_stale=False)
    assert result == TradabilityStatus.UNVALUABLE


def test_tradability_research_only_when_execution_unsupported() -> None:
    """A quote exists (forecasting is meaningful) but no honest fill model
    does — the exact situation §7.5 reserves RESEARCH_ONLY for."""
    view = _view(InstrumentStatus.ACTIVE, ExecutionModel.UNSUPPORTED)
    result = compute_tradability(view, has_tradable_quote=True, data_is_stale=False)
    assert result == TradabilityStatus.RESEARCH_ONLY


def test_tradability_tradable_in_the_default_case() -> None:
    view = _view(InstrumentStatus.ACTIVE, ExecutionModel.LEVEL_A_REAL_QUOTES)
    result = compute_tradability(view, has_tradable_quote=True, data_is_stale=False)
    assert result == TradabilityStatus.TRADABLE
