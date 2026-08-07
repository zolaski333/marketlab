"""Provider-independent language model interface (§12.1).

Nothing above this module may depend on a specific model provider's SDK or
wire format — that is what "the core must not depend on any specific
provider" means in practice here. A real Phase 3 adapter (OpenAI, Anthropic,
whatever) implements :class:`LanguageModel` directly against its own client;
every other package talks to this Protocol only.

No field anywhere in this module may carry an experimental condition id, arm
id, or repetition number — enforced structurally by
``tests/security/test_condition_isolation.py``, which inspects these
dataclasses' fields and :meth:`LanguageModel.generate`'s signature directly,
the same way ``test_decision_path_isolation.py`` inspects imports rather than
trusting a convention. A model that could see which arm it is running under
could branch on it, which is precisely the defect the prior implementation's
audit found ("mock LLM branching on condition_id" — see
``docs/ROADMAP.md``). A condition can only ever influence a decision through
the *content* it grants access to — see
``marketlab.agents.decision.ConditionContext``.

Tool calling is modelled as an explicit request/response loop rather than any
one provider's exact wire format: a turn either asks for more evidence
(``tool_calls``), commits to a decision (``decision``), or refuses
(``refused``). :mod:`marketlab.agents.decision` drives the loop generically,
so the same orchestration exercises a deterministic fake today and a real
adapter later without change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "LanguageModel",
    "ModelRequest",
    "ModelResponse",
    "RawDecision",
    "RawForecast",
    "RawTradeIntent",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolSchema",
    "TradeSide",
]


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """One tool's name and a human/model-readable description of its parameters.

    Deliberately not a JSON-schema object: Phase 1 has no real provider to
    negotiate a wire format with (§34.3 prefers a tested abstraction over an
    early fragile integration). A real adapter is free to translate this into
    whatever schema shape its provider's tool-calling API wants.
    """

    name: str
    description: str
    parameters: Mapping[str, str]
    """Parameter name -> a one-line description of what it means."""


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """One tool invocation the model is asking the agent to perform."""

    tool_name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """One tool invocation's outcome, fed back to the model on the next turn.

    ``result`` must already be plain, JSON-safe data (str / int / float /
    bool / None / dict / list) — the same constraint a real provider's wire
    format would impose. Converting a retrieval result into this shape is
    :mod:`marketlab.agents.decision`'s job, not this module's; this module
    stays free of any dependency on :mod:`marketlab.retrieval`.
    """

    tool_name: str
    arguments: Mapping[str, Any]
    result: Any


@dataclass(frozen=True, slots=True)
class RawForecast:
    """A probabilistic claim, as the model stated it — not yet validated
    against the snapshot (see ``marketlab.agents.decision.Forecast`` for the
    validated form)."""

    instrument_id: str
    horizon_sessions: int
    probability_up: float
    cited_evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RawTradeIntent:
    """A trade intent, as the model stated it — not yet validated.

    Carries no size or weight: sizing against real capital and risk limits is
    the execution engine's contract (task 10), not decided here.
    """

    instrument_id: str
    side: TradeSide
    rationale: str
    cited_evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RawDecision:
    """What the model claims, before any validation against the snapshot."""

    forecasts: tuple[RawForecast, ...]
    trade_intents: tuple[RawTradeIntent, ...]
    narrative: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One turn's worth of context handed to the model.

    ``injected_context`` is the *only* channel through which an experimental
    condition may differ from another — the actual memory/reflection/placebo
    text a condition grants, or ``None`` for a condition that grants none.
    There is deliberately no field here for which condition or arm produced
    it.
    """

    system_prompt: str
    injected_context: str | None
    tool_catalogue: tuple[ToolSchema, ...]
    tool_results: tuple[ToolCallResult, ...] = ()
    required_forecasts: tuple[tuple[str, int], ...] = ()
    """``(instrument_id, horizon_sessions)`` pairs the response must cover.

    Empty for a free decision; populated for an imposed panel (§15), where
    every condition is asked the *same* questions so their answers are
    comparable. Deliberately a plain pair rather than a
    :class:`~marketlab.forecasting.panel.PanelItem`: this module must stay
    free of any dependency on the packages above it, and a model needs the
    question, not the bookkeeping around it."""


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """One turn's response. At most one of ``tool_calls`` / ``decision`` is
    meaningful at a time; :mod:`marketlab.agents.decision` treats a response
    that sets both as malformed rather than guessing which one to honour."""

    tool_calls: tuple[ToolCallRequest, ...] = ()
    decision: RawDecision | None = None
    refused: bool = False
    refusal_reason: str = ""


@runtime_checkable
class LanguageModel(Protocol):
    """A provider-independent chat/tool-calling model."""

    @property
    def model_id(self) -> str: ...

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Produce the next turn's response.

        Raises:
            marketlab.core.failures.ModelProviderError: if the underlying
                provider is unreachable after the allowed retries (§23.4).
                Deliberately not caught by
                :class:`marketlab.agents.decision.DecisionAgent` — a missing
                provider means the whole decision is missing, which is a
                run-level concern for whatever calls it, not an agent-level
                observation to record and continue past.
        """
        ...
