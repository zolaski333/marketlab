"""Tests for the frozen retrieval index and its evidence types (§9.1, §10.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from marketlab.core.failures import SnapshotStatus
from marketlab.core.instants import Instant, instant_from_datetime
from marketlab.instruments.types import (
    AssetClass,
    ExecutionModel,
    InstrumentStatus,
    InstrumentView,
)
from marketlab.retrieval.types import (
    Evidence,
    EvidenceKind,
    RetrievalIndex,
    price_quote_from_evidence,
)

ALPHA_ID = "id-alpha"
BETA_ID = "id-beta"


def _instant(hour: int) -> Instant:
    return instant_from_datetime(datetime(2026, 8, 1, hour, 0, tzinfo=UTC))


def _evidence(
    kind: EvidenceKind,
    subject_ids: tuple[str, ...],
    *,
    hour: int = 10,
    headline: str = "headline",
    fields: dict[str, object] | None = None,
    suffix: str = "",
) -> Evidence:
    return Evidence(
        evidence_id=f"evid-{kind}-{'-'.join(subject_ids)}{suffix}",
        kind=kind,
        subject_ids=subject_ids,
        as_of=_instant(hour),
        first_seen_at=_instant(hour),
        blob_hash="a" * 64,
        headline=headline,
        fields=fields or {},
    )


def _alpha_view() -> InstrumentView:
    return InstrumentView(
        instrument_id=ALPHA_ID,
        asset_class=AssetClass.EQUITY,
        version_number=1,
        ticker="EQ_US_ALPHA",
        name="Alpha Corp",
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="SYNTH_US_EQUITY",
        settlement_days=2,
        status=InstrumentStatus.ACTIVE,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        effective_from=_instant(0),
    )


def _beta_view() -> InstrumentView:
    return InstrumentView(
        instrument_id=BETA_ID,
        asset_class=AssetClass.ETF,
        version_number=1,
        ticker="ETF_US_BETA",
        name="Beta Broad Market ETF",
        quote_currency="USD",
        native_timezone="America/New_York",
        calendar_code="SYNTH_US_EQUITY",
        settlement_days=2,
        status=InstrumentStatus.ACTIVE,
        execution_model=ExecutionModel.LEVEL_A_REAL_QUOTES,
        effective_from=_instant(0),
    )


def test_resolve_instrument_hit_and_miss() -> None:
    index = RetrievalIndex(
        "snap-1", _instant(12), SnapshotStatus.COMPLETE, (_alpha_view(), _beta_view()), ()
    )
    assert index.resolve_instrument(ALPHA_ID) is not None
    assert index.resolve_instrument("nonexistent") is None


def test_search_instruments_matches_ticker_and_name_case_insensitively() -> None:
    index = RetrievalIndex(
        "snap-1", _instant(12), SnapshotStatus.COMPLETE, (_alpha_view(), _beta_view()), ()
    )
    assert [v.instrument_id for v in index.search_instruments("alpha")] == [ALPHA_ID]
    assert [v.instrument_id for v in index.search_instruments("ETF")] == [BETA_ID]
    assert index.search_instruments("nope") == ()


def test_get_evidence_hit_and_miss() -> None:
    ev = _evidence(EvidenceKind.NEWS_ITEM, (ALPHA_ID,))
    index = RetrievalIndex("snap-1", _instant(12), SnapshotStatus.COMPLETE, (), (ev,))
    assert index.get_evidence(ev.evidence_id) is ev
    assert index.get_evidence("nonexistent") is None


def test_evidence_of_kind_filters_by_kind_and_subject_and_sorts_by_as_of() -> None:
    early = _evidence(EvidenceKind.NEWS_ITEM, (ALPHA_ID,), hour=9, suffix="-early")
    late = _evidence(EvidenceKind.NEWS_ITEM, (ALPHA_ID,), hour=11, suffix="-late")
    other_instrument = _evidence(EvidenceKind.NEWS_ITEM, (BETA_ID,), hour=10, suffix="-other")
    price = _evidence(EvidenceKind.PRICE_BAR, (ALPHA_ID,), hour=10, suffix="-price")
    index = RetrievalIndex(
        "snap-1", _instant(12), SnapshotStatus.COMPLETE, (), (late, early, other_instrument, price)
    )
    result = index.evidence_of_kind(EvidenceKind.NEWS_ITEM, subject_id=ALPHA_ID)
    assert result == (early, late)


def test_latest_picks_the_item_with_the_greatest_as_of_not_insertion_order() -> None:
    """The macro-revision scenario: revision 1 seen first, revision 2 dated later."""
    revision_1 = _evidence(
        EvidenceKind.MACRO_RECORD,
        ("SYNTH_US_INFLATION_RATE",),
        hour=9,
        suffix="-r1",
        fields={"revision": 1, "value": "2.4"},
    )
    revision_2 = _evidence(
        EvidenceKind.MACRO_RECORD,
        ("SYNTH_US_INFLATION_RATE",),
        hour=14,
        suffix="-r2",
        fields={"revision": 2, "value": "2.2"},
    )
    index = RetrievalIndex(
        "snap-1", _instant(20), SnapshotStatus.COMPLETE, (), (revision_2, revision_1)
    )
    latest = index.latest(EvidenceKind.MACRO_RECORD, "SYNTH_US_INFLATION_RATE")
    assert latest is revision_2


def test_latest_returns_none_when_nothing_matches() -> None:
    index = RetrievalIndex("snap-1", _instant(12), SnapshotStatus.COMPLETE, (), ())
    assert index.latest(EvidenceKind.FX_RATE, "EUR_USD") is None


def test_search_matches_headline_and_string_fields_case_insensitively() -> None:
    ev = _evidence(
        EvidenceKind.NEWS_ITEM,
        (ALPHA_ID,),
        headline="Routine update",
        fields={"body": "Alpha Corp reports strong quarterly growth."},
    )
    index = RetrievalIndex("snap-1", _instant(12), SnapshotStatus.COMPLETE, (), (ev,))
    assert index.search("growth") == (ev,)
    assert index.search("ROUTINE") == (ev,)
    assert index.search("nonexistent keyword") == ()


def test_search_ignores_non_string_fields() -> None:
    ev = _evidence(EvidenceKind.PRICE_BAR, (ALPHA_ID,), fields={"volume": 12345})
    index = RetrievalIndex("snap-1", _instant(12), SnapshotStatus.COMPLETE, (), (ev,))
    assert index.search("12345") == ()


def test_empty_search_query_returns_everything() -> None:
    ev = _evidence(EvidenceKind.PRICE_BAR, (ALPHA_ID,))
    index = RetrievalIndex("snap-1", _instant(12), SnapshotStatus.COMPLETE, (), (ev,))
    assert index.search("") == (ev,)


def test_price_quote_from_evidence_decodes_typed_numbers() -> None:
    ev = _evidence(
        EvidenceKind.PRICE_BAR,
        (ALPHA_ID,),
        fields={"bid": "149.90", "ask": "150.10", "close": "150.00", "volume": 1000},
    )
    quote = price_quote_from_evidence(ev)
    assert quote.bid == Decimal("149.90")
    assert quote.ask == Decimal("150.10")
    assert quote.close == Decimal("150.00")
    assert quote.volume == 1000


def test_price_quote_from_evidence_rejects_the_wrong_kind() -> None:
    ev = _evidence(EvidenceKind.NEWS_ITEM, (ALPHA_ID,))
    with pytest.raises(ValueError, match="Not a PRICE_BAR"):
        price_quote_from_evidence(ev)
