"""Tests for the deterministic, condition-blind policy fake (§31 Phase 1)."""

from __future__ import annotations

from marketlab.models.deterministic import DeterministicPolicyModel
from marketlab.models.types import ModelRequest, ToolCallRequest, ToolCallResult

ALPHA_ID = "id-alpha"
BETA_ID = "id-beta"


def _request(tool_results: tuple[ToolCallResult, ...] = ()) -> ModelRequest:
    return ModelRequest(
        system_prompt="test", injected_context=None, tool_catalogue=(), tool_results=tool_results
    )


def test_first_turn_asks_to_discover_the_universe() -> None:
    response = DeterministicPolicyModel().generate(_request())
    assert response.decision is None
    assert response.tool_calls == (ToolCallRequest("search_instruments", {"query": ""}),)


def test_second_turn_asks_for_every_instrument_price() -> None:
    universe_result = ToolCallResult(
        "search_instruments",
        {"query": ""},
        [{"instrument_id": ALPHA_ID}, {"instrument_id": BETA_ID}],
    )
    response = DeterministicPolicyModel().generate(_request((universe_result,)))
    # ToolCallRequest.arguments is a dict, so its instances are unhashable;
    # compare as a sorted list instead of a set.
    calls = sorted(response.tool_calls, key=lambda c: c.arguments["instrument_id"])
    assert calls == [
        ToolCallRequest("get_price_quote", {"instrument_id": ALPHA_ID}),
        ToolCallRequest("get_price_quote", {"instrument_id": BETA_ID}),
    ]


def test_third_turn_asks_for_news_once_every_price_is_gathered() -> None:
    results = (
        ToolCallResult("search_instruments", {"query": ""}, [{"instrument_id": ALPHA_ID}]),
        ToolCallResult(
            "get_price_quote",
            {"instrument_id": ALPHA_ID},
            {"evidence_id": "ev-1", "fields": {"close": "150.00"}},
        ),
    )
    response = DeterministicPolicyModel().generate(_request(results))
    assert response.tool_calls == (ToolCallRequest("search_news", {"query": ""}),)


def test_final_turn_produces_a_decision_citing_the_gathered_evidence() -> None:
    results = (
        ToolCallResult("search_instruments", {"query": ""}, [{"instrument_id": ALPHA_ID}]),
        ToolCallResult(
            "get_price_quote",
            {"instrument_id": ALPHA_ID},
            {"evidence_id": "ev-price", "fields": {"close": "150.00"}},
        ),
        ToolCallResult("search_news", {"query": ""}, []),
    )
    response = DeterministicPolicyModel().generate(_request(results))
    assert response.decision is not None
    assert len(response.decision.forecasts) == 1

    forecast = response.decision.forecasts[0]
    assert forecast.instrument_id == ALPHA_ID
    assert 0.0 <= forecast.probability_up <= 1.0
    assert "ev-price" in forecast.cited_evidence_ids

    assert len(response.decision.trade_intents) == 1
    assert response.decision.trade_intents[0].instrument_id == ALPHA_ID


def test_related_news_is_cited_but_never_changes_the_probability() -> None:
    """The whole point of the closing-price signal: news content is citable
    context, never an input to the number."""
    base_results = (
        ToolCallResult("search_instruments", {"query": ""}, [{"instrument_id": ALPHA_ID}]),
        ToolCallResult(
            "get_price_quote",
            {"instrument_id": ALPHA_ID},
            {"evidence_id": "ev-price", "fields": {"close": "150.00"}},
        ),
    )
    model = DeterministicPolicyModel()
    without_news = model.generate(
        _request((*base_results, ToolCallResult("search_news", {"query": ""}, [])))
    )
    with_news = model.generate(
        _request(
            (
                *base_results,
                ToolCallResult(
                    "search_news",
                    {"query": ""},
                    [{"evidence_id": "ev-news", "subject_ids": [ALPHA_ID]}],
                ),
            )
        )
    )
    assert without_news.decision is not None
    assert with_news.decision is not None
    assert (
        without_news.decision.forecasts[0].probability_up
        == with_news.decision.forecasts[0].probability_up
    )
    assert without_news.decision.trade_intents[0].side == with_news.decision.trade_intents[0].side
    assert "ev-news" not in without_news.decision.forecasts[0].cited_evidence_ids
    assert "ev-news" in with_news.decision.forecasts[0].cited_evidence_ids


def test_identical_requests_produce_identical_responses() -> None:
    results = (
        ToolCallResult("search_instruments", {"query": ""}, [{"instrument_id": ALPHA_ID}]),
        ToolCallResult(
            "get_price_quote",
            {"instrument_id": ALPHA_ID},
            {"evidence_id": "ev-price", "fields": {"close": "150.00"}},
        ),
        ToolCallResult("search_news", {"query": ""}, []),
    )
    model = DeterministicPolicyModel()
    assert model.generate(_request(results)) == model.generate(_request(results))


def test_probability_varies_with_closing_price() -> None:
    model = DeterministicPolicyModel()

    def decide_for(close: str) -> float:
        results = (
            ToolCallResult("search_instruments", {"query": ""}, [{"instrument_id": ALPHA_ID}]),
            ToolCallResult(
                "get_price_quote",
                {"instrument_id": ALPHA_ID},
                {"evidence_id": "ev-price", "fields": {"close": close}},
            ),
            ToolCallResult("search_news", {"query": ""}, []),
        )
        response = model.generate(_request(results))
        assert response.decision is not None
        return response.decision.forecasts[0].probability_up

    assert decide_for("100.01") != decide_for("100.99")


def test_a_missing_price_result_is_skipped_rather_than_crashing() -> None:
    results = (
        ToolCallResult(
            "search_instruments",
            {"query": ""},
            [{"instrument_id": ALPHA_ID}, {"instrument_id": BETA_ID}],
        ),
        ToolCallResult(
            "get_price_quote",
            {"instrument_id": ALPHA_ID},
            {"evidence_id": "ev-1", "fields": {"close": "100.00"}},
        ),
        ToolCallResult("get_price_quote", {"instrument_id": BETA_ID}, None),
        ToolCallResult("search_news", {"query": ""}, []),
    )
    response = DeterministicPolicyModel().generate(_request(results))
    assert response.decision is not None
    assert [f.instrument_id for f in response.decision.forecasts] == [ALPHA_ID]
