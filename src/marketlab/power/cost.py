"""What a study costs to run, per arm (§29.5, task #4).

Why this is not a multiplication
--------------------------------
The obvious model — elicitations x tokens x price — is wrong here by a factor
of several, because :class:`~marketlab.agents.decision.DecisionAgent` **resends
every accumulated tool result on every turn**. A decision that takes five turns
does not send its evidence once; it sends the first tool result five times, the
second four times, and so on. Input cost therefore grows roughly with the
*square* of the turn count, and that term dominates everything else.

Stated as a formula, for one elicitation with ``T`` turns and evidence arriving
in equal parts across the ``T-1`` tool-calling turns:

    input ≈ T x (system + catalogue + granted material)
          + evidence x (T - 1) / 2

The second term is the one people forget. At the shipped defaults it is about
three quarters of the bill.

Two numbers, never mixed
------------------------
:class:`TokenProfile` can be **measured** from a real run's recorded usage, or
**assumed** from a written-down guess. A projection says which it was, and
:meth:`CostModel.project` refuses to silently average an unmeasured run into a
measured one. The deterministic fake reports no usage at all, so a projection
over a synthetic run is necessarily an assumption — and says so.

Caching
-------
The resent prefix is exactly what a provider-side prompt cache is for: the
system prompt, the tool catalogue, the granted material and every earlier tool
result are byte-identical from turn to turn. :attr:`Prices.cached_input` is
therefore not a rounding detail but the difference between a study costing
three hundred euros and one costing eighty.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from marketlab.core.failures import ConfigurationError
from marketlab.models.types import TokenUsage

__all__ = [
    "CostModel",
    "CostProjection",
    "Prices",
    "ProfileSource",
    "TokenProfile",
    "elicitation_input_tokens",
]

_MILLION: Final = Decimal(1_000_000)


class ProfileSource(StrEnum):
    """Where a token profile's numbers came from."""

    MEASURED = "MEASURED"
    """Read back from a run's recorded provider usage."""

    ASSUMED = "ASSUMED"
    """A written-down guess. Every projection built on one says so."""


@dataclass(frozen=True, slots=True)
class Prices:
    """Provider prices, per million tokens, in whatever currency the caller uses.

    No defaults. Prices change, differ by provider and by tier, and a library
    that shipped a number would be quoting a tariff it cannot know — the same
    reasoning that keeps a default out of
    :class:`~marketlab.analysis.equivalence.Rope`.
    """

    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None
    """``None`` means the provider has no cache discount, so cached input is
    billed at the full input rate."""

    currency: str = "USD"

    def __post_init__(self) -> None:
        for name, value in (
            ("input_per_million", self.input_per_million),
            ("output_per_million", self.output_per_million),
        ):
            if value < 0:
                raise ConfigurationError(f"{name} cannot be negative, got {value}")
        if self.cached_input_per_million is not None and self.cached_input_per_million < 0:
            raise ConfigurationError("cached_input_per_million cannot be negative")

    @property
    def effective_cached_rate(self) -> Decimal:
        if self.cached_input_per_million is None:
            return self.input_per_million
        return self.cached_input_per_million

    def charge(self, usage: TokenUsage) -> Decimal:
        """Exact cost of one usage record. Decimal, never float (§17.2)."""
        return (
            Decimal(usage.input_tokens) * self.input_per_million
            + Decimal(usage.cached_input_tokens) * self.effective_cached_rate
            + Decimal(usage.output_tokens) * self.output_per_million
        ) / _MILLION


@dataclass(frozen=True, slots=True)
class TokenProfile:
    """What one elicitation costs, in tokens, before any pricing.

    Expressed as the *components* of a prompt rather than a single total, so
    that changing the tool budget or the recall depth changes the projection
    the way it would change the bill.
    """

    turns: float
    """Mean model turns per elicitation, including the one that decides."""

    fixed_tokens: int
    """System prompt plus tool catalogue: resent verbatim every turn."""

    granted_tokens: int
    """Injected memory/reflection material. Zero for the control arm, which is
    why cost is projected per arm rather than per study."""

    evidence_tokens: int
    """Total tool output accumulated over the elicitation."""

    output_tokens: int
    """Total generated tokens across all turns."""

    source: ProfileSource = ProfileSource.ASSUMED

    def __post_init__(self) -> None:
        if self.turns < 1:
            raise ConfigurationError(f"turns must be >= 1, got {self.turns}")
        for name in ("fixed_tokens", "granted_tokens", "evidence_tokens", "output_tokens"):
            if getattr(self, name) < 0:
                raise ConfigurationError(f"{name} cannot be negative")


def elicitation_input_tokens(profile: TokenProfile) -> tuple[int, int]:
    """Input tokens for one elicitation, split ``(fresh, cacheable)``.

    The stable prefix — system prompt, catalogue, granted material, and every
    tool result already seen — is what a provider cache can serve. Only the
    first occurrence of each is genuinely new.

    Returns:
        ``(fresh, cacheable)``. Their sum is the total input the provider
        sees; how it is billed depends on whether the caller's :class:`Prices`
        has a cache rate.
    """
    turns = profile.turns
    prefix = profile.fixed_tokens + profile.granted_tokens

    # Sent once, then resent on every later turn.
    fresh_prefix = prefix
    cacheable_prefix = int(prefix * (turns - 1))

    # Evidence arrives across the tool-calling turns and is resent thereafter.
    # With it arriving in equal parts, the total sent is evidence x (T-1)/2,
    # of which one full copy is new.
    fresh_evidence = profile.evidence_tokens
    resent_evidence = max(0.0, profile.evidence_tokens * (turns - 2) / 2)

    return fresh_prefix + fresh_evidence, cacheable_prefix + int(resent_evidence)


@dataclass(frozen=True, slots=True)
class CostProjection:
    """What one configuration is expected to cost."""

    label: str
    elicitations: int
    usage: TokenUsage
    cost: Decimal
    currency: str
    source: ProfileSource

    @property
    def is_measured(self) -> bool:
        return self.source is ProfileSource.MEASURED

    def as_payload(self) -> dict[str, str | int]:
        return {
            "label": self.label,
            "elicitations": self.elicitations,
            "input_tokens": self.usage.input_tokens,
            "cached_input_tokens": self.usage.cached_input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cost": f"{self.cost:.2f}",
            "currency": self.currency,
            "basis": str(self.source),
        }


@dataclass(frozen=True, slots=True)
class CostModel:
    """Projects a study's API cost from a token profile and a price list."""

    profile: TokenProfile
    prices: Prices

    def project(self, *, label: str, elicitations: int) -> CostProjection:
        """Cost of ``elicitations`` calls sharing this profile.

        Raises:
            ConfigurationError: if ``elicitations`` is negative. Zero is
                allowed and means zero — an arm that is never asked anything.
        """
        if elicitations < 0:
            raise ConfigurationError(f"elicitations cannot be negative, got {elicitations}")

        fresh, cacheable = elicitation_input_tokens(self.profile)
        usage = TokenUsage(
            input_tokens=fresh * elicitations,
            cached_input_tokens=cacheable * elicitations,
            output_tokens=self.profile.output_tokens * elicitations,
        )
        return CostProjection(
            label=label,
            elicitations=elicitations,
            usage=usage,
            cost=self.prices.charge(usage),
            currency=self.prices.currency,
            source=self.profile.source,
        )


def measure_profile(
    usages: Sequence[TokenUsage], *, turns: Sequence[int], evidence_share: float = 0.75
) -> TokenProfile:
    """Build a profile from a run's *recorded* usage.

    ``evidence_share`` splits measured input between the evidence term and the
    fixed prefix, because a provider reports one input number and not its
    composition. It is the one assumption inside a measured profile, and it is
    an argument rather than a constant so that a caller who knows better can
    say so.

    Raises:
        ConfigurationError: if nothing was measured. A run under the
            deterministic fake reports no usage at all, and averaging over it
            would produce a profile claiming a real model costs nothing.
    """
    measured = [usage for usage in usages if usage.is_measured]
    if not measured:
        raise ConfigurationError(
            "No recorded token usage: every call reported zero. This is what a run "
            "against the deterministic fake looks like, and a profile built from it "
            "would say a real model is free. Measure a pilot against a real provider, "
            "or state an assumed profile explicitly.",
            calls=len(usages),
        )
    if not turns:
        raise ConfigurationError("Cannot measure a profile without turn counts.")

    mean_turns = sum(turns) / len(turns)
    total_input = sum(usage.input_tokens + usage.cached_input_tokens for usage in measured)
    mean_input = total_input / len(measured)
    mean_output = sum(usage.output_tokens for usage in measured) / len(measured)

    # Invert the resend arithmetic to recover per-elicitation components.
    prefix_weight = mean_turns
    evidence_weight = max(1.0, (mean_turns - 1) / 2)
    evidence_total = mean_input * evidence_share
    prefix_total = mean_input - evidence_total

    return TokenProfile(
        turns=mean_turns,
        fixed_tokens=int(prefix_total / prefix_weight),
        granted_tokens=0,
        evidence_tokens=int(evidence_total / evidence_weight),
        output_tokens=int(mean_output),
        source=ProfileSource.MEASURED,
    )
