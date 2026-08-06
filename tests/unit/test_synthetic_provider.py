"""Tests for the deterministic synthetic market (§31 Phase 1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from marketlab.core.failures import ConfigurationError
from marketlab.core.instants import Instant, instant_from_datetime, to_datetime
from marketlab.ingestion.synthetic import SyntheticMarketDataProvider, generate_session_cutoffs
from marketlab.instruments.calendars import WeekdaySessionCalendar

ALPHA_ID = "id-alpha"
BETA_ID = "id-beta"
GAMMA_ID = "id-gamma"
DELTA_ID = "id-delta"


@pytest.fixture
def equity_calendar() -> WeekdaySessionCalendar:
    return WeekdaySessionCalendar(
        code="TEST_US_EQUITY",
        version="v1",
        iana_tz="America/New_York",
        session_open=time(9, 30),
        session_close=time(16, 0),
        holidays=frozenset({date(2026, 12, 25)}),
    )


def start_at() -> Instant:
    return instant_from_datetime(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))


def make_provider(
    equity_calendar: WeekdaySessionCalendar, num_sessions: int = 30
) -> SyntheticMarketDataProvider:
    return SyntheticMarketDataProvider(
        equity_calendar=equity_calendar,
        start_at=start_at(),
        num_sessions=num_sessions,
        alpha_id=ALPHA_ID,
        beta_id=BETA_ID,
        gamma_id=GAMMA_ID,
        delta_id=DELTA_ID,
    )


# -- session cutoffs ---------------------------------------------------------


def test_generate_session_cutoffs_produces_the_requested_count(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    cutoffs = generate_session_cutoffs(equity_calendar, start_at(), 10)
    assert len(cutoffs) == 10


def test_session_cutoffs_are_strictly_increasing(equity_calendar: WeekdaySessionCalendar) -> None:
    cutoffs = generate_session_cutoffs(equity_calendar, start_at(), 15)
    assert cutoffs == sorted(cutoffs)
    assert len(set(cutoffs)) == len(cutoffs)


def test_session_cutoffs_never_fall_on_a_weekend(equity_calendar: WeekdaySessionCalendar) -> None:
    cutoffs = generate_session_cutoffs(equity_calendar, start_at(), 30)
    for cutoff in cutoffs:
        assert to_datetime(cutoff).astimezone(UTC).weekday() < 5


def test_generate_session_cutoffs_rejects_a_non_positive_count(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    with pytest.raises(ConfigurationError, match="must be positive"):
        generate_session_cutoffs(equity_calendar, start_at(), 0)


# -- determinism --------------------------------------------------------


def test_identical_construction_yields_identical_price_bars(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    provider_a = make_provider(equity_calendar)
    provider_b = make_provider(equity_calendar)
    cutoff = provider_a.session_cutoffs()[9]
    assert provider_a.fetch_price_bars(cutoff) == provider_b.fetch_price_bars(cutoff)


def test_repeated_calls_are_identical(equity_calendar: WeekdaySessionCalendar) -> None:
    provider = make_provider(equity_calendar)
    cutoff = provider.session_cutoffs()[0]
    assert provider.fetch_price_bars(cutoff) == provider.fetch_price_bars(cutoff)
    assert provider.fetch_news(cutoff) == provider.fetch_news(cutoff)
    assert provider.fetch_fx_rates(cutoff) == provider.fetch_fx_rates(cutoff)


def test_an_as_of_outside_the_precomputed_cutoffs_is_refused(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    provider = make_provider(equity_calendar)
    with pytest.raises(ConfigurationError, match="not one of this synthetic world"):
        provider.fetch_price_bars(start_at())


# -- prices ---------------------------------------------------------------


def test_every_instrument_has_a_bar_with_a_sane_bid_ask_spread(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    provider = make_provider(equity_calendar)
    bars = provider.fetch_price_bars(provider.session_cutoffs()[0])
    ids = {bar.instrument_id for bar in bars}
    assert ids == {ALPHA_ID, BETA_ID, GAMMA_ID, DELTA_ID}
    for bar in bars:
        assert bar.bid < bar.close < bar.ask
        assert bar.volume > 0
        assert bar.first_seen_at == bar.as_of


def test_alpha_price_halves_from_the_split_session_onward(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    provider = make_provider(equity_calendar, num_sessions=25)
    cutoffs = provider.session_cutoffs()

    def alpha_close(session_idx: int) -> Decimal:
        bars = provider.fetch_price_bars(cutoffs[session_idx - 1])
        return next(b.close for b in bars if b.instrument_id == ALPHA_ID)

    before_split = alpha_close(17)
    at_split = alpha_close(18)

    # Doubling the post-split close should roughly recover the pre-split
    # trajectory (within one session's worth of drift/oscillation) — this is
    # what distinguishes an actually-applied split from the old defect where
    # raw prices were left unadjusted after a split event, corrupting every
    # forecast resolution that straddled it.
    assert at_split < before_split
    assert abs(at_split * Decimal("2") - before_split) < Decimal("10")


def test_fx_rate_is_not_constant_across_sessions(equity_calendar: WeekdaySessionCalendar) -> None:
    """The defect this fixes: a constant FX rate makes FX P&L attribution
    identically zero, silently hiding an entire class of accounting bug."""
    provider = make_provider(equity_calendar, num_sessions=20)
    rates = {provider.fetch_fx_rates(cutoff)[0].rate for cutoff in provider.session_cutoffs()}
    assert len(rates) > 1


# -- news fixtures ------------------------------------------------------


def test_session_five_has_no_news(equity_calendar: WeekdaySessionCalendar) -> None:
    provider = make_provider(equity_calendar)
    assert provider.fetch_news(provider.session_cutoffs()[4]) == ()


def test_session_six_carries_a_positive_alpha_story(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    provider = make_provider(equity_calendar)
    items = provider.fetch_news(provider.session_cutoffs()[5])
    assert len(items) == 1
    assert ALPHA_ID in items[0].instrument_ids
    assert "strength" in items[0].body.lower() or "growth" in items[0].title.lower()


def test_injection_session_carries_an_embedded_instruction_as_plain_text(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    """§11.2: the fixture exists to be tested by prompt-injection containment
    tests downstream — here we only verify the content is present and inert
    (a plain string field, not something that could execute)."""
    provider = make_provider(equity_calendar, num_sessions=22)
    items = provider.fetch_news(provider.session_cutoffs()[21])
    assert len(items) == 1
    assert isinstance(items[0].body, str)
    assert "SYSTEM OVERRIDE" in items[0].body


def test_late_data_session_has_an_item_first_seen_after_its_cutoff(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    """§6.2/§23.5: this is what a snapshot builder must filter out of the
    session it is nominally "about"."""
    provider = make_provider(equity_calendar)
    cutoff = provider.session_cutoffs()[11]
    items = provider.fetch_news(cutoff)
    assert len(items) == 2
    late_items = [item for item in items if item.first_seen_at > cutoff]
    assert len(late_items) == 1


# -- macro ------------------------------------------------------------------


def test_macro_indicator_is_revised_partway_through(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    provider = make_provider(equity_calendar, num_sessions=20)
    cutoffs = provider.session_cutoffs()

    before = provider.fetch_macro_records(cutoffs[12])[0]  # session 13
    after = provider.fetch_macro_records(cutoffs[13])[0]  # session 14 (revision session)

    assert before.revision == 1
    assert after.revision == 2
    assert before.value != after.value


# -- corporate actions -----------------------------------------------------


def test_dividend_is_declared_at_the_configured_session(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    provider = make_provider(equity_calendar, num_sessions=15)
    actions = provider.fetch_corporate_actions(provider.session_cutoffs()[9])  # session 10
    assert len(actions) == 1
    assert actions[0].action_type == "CASH_DIVIDEND"
    assert actions[0].instrument_id == ALPHA_ID


def test_no_corporate_actions_on_an_ordinary_session(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    provider = make_provider(equity_calendar, num_sessions=15)
    assert provider.fetch_corporate_actions(provider.session_cutoffs()[0]) == ()


def test_split_and_ticker_change_are_declared_at_their_sessions(
    equity_calendar: WeekdaySessionCalendar,
) -> None:
    provider = make_provider(equity_calendar, num_sessions=25)
    split_actions = provider.fetch_corporate_actions(provider.session_cutoffs()[17])  # session 18
    assert split_actions[0].action_type == "STOCK_SPLIT"

    ticker_actions = provider.fetch_corporate_actions(provider.session_cutoffs()[23])  # session 24
    assert ticker_actions[0].action_type == "TICKER_CHANGE"
    assert ticker_actions[0].details["new_ticker"] == "EQ_US_ALPHA_2"
