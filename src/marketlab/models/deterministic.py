"""A deterministic, condition-blind policy fake (§31 Phase 1).

Implements :class:`~marketlab.models.types.LanguageModel` without any real
model call: every decision is a closed-form function of the tool results it
has gathered, with no random component — the same discipline
:class:`~marketlab.ingestion.synthetic.SyntheticMarketDataProvider` already
established for market data. This is what lets the rest of the platform —
arms, execution, memory, reflection, statistics — be exercised end to end
offline, without paying for or depending on a real provider (§34.3).

Structurally condition-blind: this class never receives, and
:class:`~marketlab.models.types.ModelRequest` has no field for, a condition
id, arm id, or repetition number (see
``tests/security/test_condition_isolation.py``). Any difference between
conditions can only come from ``ModelRequest.injected_context`` — the actual
memory/reflection/placebo text a condition supplies, if any — never from
being told which condition it is.

Decision rule (deliberately simple and auditable, not a trading strategy):
for each instrument, fold the fractional cents of its latest close into
``[-1, 1)`` and squash that through a fixed logistic curve to get
``probability_up``. This is a closed-form function of a *number* the
snapshot always provides, never of *text* — the policy cites nearby news as
supporting evidence but never reads it for a directive, which is what keeps
it inert against an embedded instruction like the session-22 sentinel
fixture (see ``tests/security/test_prompt_injection_containment.py``): citing
a news item as considered evidence is fine, obeying a command written inside
one is not, and this rule has no code path that could do the latter.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from marketlab.models.types import (
    ModelRequest,
    ModelResponse,
    RawDecision,
    RawForecast,
    RawTradeIntent,
    ToolCallRequest,
    ToolCallResult,
    TradeSide,
)

__all__ = ["DeterministicPolicyModel"]

_HORIZON_SESSIONS: Final = 5
_LOGISTIC_SCALE: Final = 3.0
_BUY_THRESHOLD: Final = 0.55
_SELL_THRESHOLD: Final = 0.45


def _group_by_tool(results: Sequence[ToolCallResult]) -> dict[str, list[ToolCallResult]]:
    grouped: dict[str, list[ToolCallResult]] = {}
    for result in results:
        grouped.setdefault(result.tool_name, []).append(result)
    return grouped


def _signal_from_close(close: Decimal) -> float:
    """A deterministic, always-defined pseudo-signal in ``[-1, 1)``.

    Not a trading signal — a fixed, reproducible function of the one number
    every priced instrument always has, chosen so the policy never has to
    special-case a degenerate input. The synthetic world's own bid/ask
    construction keeps the close exactly at the bid-ask midpoint by design,
    which would make a spread-based signal identically zero for every
    instrument every session; this deliberately does not depend on the
    spread.
    """
    cents = int((close * 100) % 100)
    return (cents - 50) / 50


def _squash(signal: float) -> float:
    return 1.0 / (1.0 + math.exp(-_LOGISTIC_SCALE * signal))


def _side_for(probability_up: float) -> TradeSide:
    if probability_up > _BUY_THRESHOLD:
        return TradeSide.BUY
    if probability_up < _SELL_THRESHOLD:
        return TradeSide.SELL
    return TradeSide.HOLD


class DeterministicPolicyModel:
    """A closed-form, reproducible stand-in for a real language model."""

    __slots__ = ("_model_id",)

    def __init__(self, model_id: str = "deterministic-v1") -> None:
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    def generate(self, request: ModelRequest) -> ModelResponse:
        by_tool = _group_by_tool(request.tool_results)

        if "search_instruments" not in by_tool:
            return ModelResponse(tool_calls=(ToolCallRequest("search_instruments", {"query": ""}),))

        universe = by_tool["search_instruments"][0].result or []
        instrument_ids = [str(entry["instrument_id"]) for entry in universe]

        priced_ids = {
            str(result.arguments["instrument_id"])
            for result in request.tool_results
            if result.tool_name == "get_price_quote"
        }
        missing_prices = [iid for iid in instrument_ids if iid not in priced_ids]
        if missing_prices:
            return ModelResponse(
                tool_calls=tuple(
                    ToolCallRequest("get_price_quote", {"instrument_id": iid})
                    for iid in missing_prices
                )
            )

        if "search_news" not in by_tool:
            return ModelResponse(tool_calls=(ToolCallRequest("search_news", {"query": ""}),))

        price_by_instrument: dict[str, Mapping[str, Any]] = {
            str(result.arguments["instrument_id"]): result.result
            for result in request.tool_results
            if result.tool_name == "get_price_quote" and result.result is not None
        }
        news_items = by_tool["search_news"][0].result or []

        return ModelResponse(decision=self._decide(instrument_ids, price_by_instrument, news_items))

    def _decide(
        self,
        instrument_ids: Sequence[str],
        price_by_instrument: Mapping[str, Mapping[str, Any]],
        news_items: Sequence[Mapping[str, Any]],
    ) -> RawDecision:
        forecasts: list[RawForecast] = []
        trade_intents: list[RawTradeIntent] = []

        for instrument_id in instrument_ids:
            quote = price_by_instrument.get(instrument_id)
            if quote is None:
                continue

            close = Decimal(str(quote["fields"]["close"]))
            probability_up = _squash(_signal_from_close(close))

            cited = [str(quote["evidence_id"])]
            related_news = [
                item for item in news_items if instrument_id in item.get("subject_ids", ())
            ]
            cited.extend(str(item["evidence_id"]) for item in related_news[:1])
            cited_evidence_ids = tuple(cited)

            forecasts.append(
                RawForecast(
                    instrument_id=instrument_id,
                    horizon_sessions=_HORIZON_SESSIONS,
                    probability_up=probability_up,
                    cited_evidence_ids=cited_evidence_ids,
                )
            )
            side = _side_for(probability_up)
            trade_intents.append(
                RawTradeIntent(
                    instrument_id=instrument_id,
                    side=side,
                    rationale=(
                        f"closing-price signal {probability_up:.3f} "
                        f"(deterministic heuristic, not investment advice)"
                    ),
                    cited_evidence_ids=cited_evidence_ids,
                )
            )

        return RawDecision(
            forecasts=tuple(forecasts),
            trade_intents=tuple(trade_intents),
            narrative=(
                "Deterministic closing-price heuristic used for Phase 1 infrastructure "
                "testing; not a trading strategy and not investment advice."
            ),
        )
