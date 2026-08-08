"""The pre-registered configuration of one study, and its persistence (§29.2).

Why this exists
---------------
Until this module, every parameter that defines a study — which arms, how many
repetitions, what each is allowed to spend, how big a position is, how much
capital it starts with — lived in whatever code happened to launch the run.
Reconstructing what a historical run was configured to do meant reading that
code, if it still existed. ``docs/ROADMAP.md`` recorded that as a gap; this
closes it.

Declared once, and then unchangeable
------------------------------------
:meth:`StudyRegistry.declare` writes the configuration under its ``run_id`` the
first time and, on every later call, checks that the configuration *recomputes
to the same fingerprint*. Re-declaring a run with a different configuration
raises rather than overwriting — the same conflict-detection
:meth:`marketlab.snapshots.builder.SnapshotBuilder.build` applies to snapshots,
and for the same reason. A study whose parameters could be edited between
cycles is not pre-registered; it is a study that was tuned while its results
were visible.

Not everything is here
----------------------
Trading calendars are objects with behaviour (DST rules, holiday sets), not
values, so they are named by ``world`` and rebuilt by the world builder rather
than serialised. That is honest for Phase 1, whose universe is a fixed
synthetic script. A Phase 3 study over a real, changing universe would need a
persisted calendar registry of its own, and ``docs/ROADMAP.md`` says so.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Final

from sqlalchemy.orm import Mapped, Session, mapped_column

from marketlab.agents.decision import DEFAULT_MAX_MODEL_TURNS
from marketlab.core.canonical import canonical_bytes, canonical_hash
from marketlab.core.clock import Clock
from marketlab.core.failures import ConfigurationError, IntegrityError
from marketlab.core.instants import Instant, parse_instant
from marketlab.core.money import Money, decimal_from_str, decimal_to_str
from marketlab.execution.policy import ExecutionPolicy
from marketlab.experiments.arms import ARMS, DEFAULT_ARMS, ArmId
from marketlab.experiments.ordering import OrderPolicy
from marketlab.experiments.runner import RunConfig
from marketlab.forecasting.panel import DEFAULT_HORIZONS
from marketlab.memory.store import DEFAULT_RECALL_LIMIT
from marketlab.reflection.engine import DEFAULT_REFLECTION_INTERVAL
from marketlab.retrieval.budget import DEFAULT_MAX_EVIDENCE_CHARS, DEFAULT_MAX_TOOL_CALLS
from marketlab.storage.base import Base, HashStr, InstantStr, ShortStr
from marketlab.storage.blobs import BlobStore

__all__ = [
    "SYNTHETIC_WORLD",
    "RunRow",
    "StudyConfig",
    "StudyRegistry",
]

SYNTHETIC_WORLD: Final = "SYNTHETIC"
"""The only world Phase 1 knows how to build."""

_DEFAULT_CAPITAL: Final = (("USD", "1000000.00"), ("EUR", "500000.00"))


class RunRow(Base):
    """One declared study. Append-only, like every other scientific fact."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(ShortStr, primary_key=True)
    world: Mapped[str] = mapped_column(ShortStr, nullable=False)
    config_hash: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)
    config_blob_hash: Mapped[str] = mapped_column(HashStr, nullable=False)
    declared_at: Mapped[str] = mapped_column(InstantStr, nullable=False)


@dataclass(frozen=True, slots=True)
class StudyConfig:
    """Every parameter that must be fixed before a study begins.

    Monetary amounts are held as exact strings rather than floats, for the
    reason §17.2 gives: a binary float cannot represent 0.01, and a
    configuration is the last place a rounding error should be introduced.
    """

    run_id: str
    start_at: Instant
    sessions: int

    world: str = SYNTHETIC_WORLD
    arms: tuple[ArmId, ...] = DEFAULT_ARMS
    repetitions: int = 1
    seed: str = "marketlab"
    order_policy: OrderPolicy = OrderPolicy.LATIN_SQUARE

    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_evidence_chars: int = DEFAULT_MAX_EVIDENCE_CHARS
    max_model_turns: int = DEFAULT_MAX_MODEL_TURNS

    panel: bool = True
    panel_horizons: tuple[int, ...] = DEFAULT_HORIZONS

    recall_limit: int = DEFAULT_RECALL_LIMIT
    reflection_interval: int = DEFAULT_REFLECTION_INTERVAL

    target_weight: str = "0.05"
    max_participation: str = "0.05"
    minimum_notional: str = "100"
    base_currency: str = "USD"
    starting_capital: tuple[tuple[str, str], ...] = field(default=_DEFAULT_CAPITAL)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ConfigurationError("run_id must not be empty.")
        if self.world != SYNTHETIC_WORLD:
            raise ConfigurationError(
                f"Unknown world {self.world!r}. Phase 1 builds {SYNTHETIC_WORLD!r} only; "
                "a real-data world is Phase 3.",
                world=self.world,
            )
        if self.sessions < 1:
            raise ConfigurationError(f"sessions must be >= 1, got {self.sessions}")
        if not self.panel_horizons:
            raise ConfigurationError(
                "A study with no panel horizons produces nothing the analysis can "
                "pair on. Set panel=false if that is deliberate."
            )
        if any(horizon < 1 for horizon in self.panel_horizons):
            raise ConfigurationError(
                f"Every panel horizon must be >= 1 session, got {list(self.panel_horizons)}."
            )
        if not self.starting_capital:
            raise ConfigurationError("A study needs starting capital in at least one currency.")
        if self.base_currency not in {currency for currency, _ in self.starting_capital}:
            raise ConfigurationError(
                f"The base currency {self.base_currency} is not among the funded currencies "
                f"{[c for c, _ in self.starting_capital]}: equity would be reported in a "
                "currency the study never holds."
            )
        # Constructing the derived objects here means an invalid weight or an
        # unknown arm is rejected at declaration, not several cycles in.
        self.run_config()
        self.execution_policy()
        for _, amount in self.starting_capital:
            decimal_from_str(amount)

    # -- derived objects -----------------------------------------------------

    def run_config(self) -> RunConfig:
        return RunConfig(
            run_id=self.run_id,
            arms=self.arms,
            repetitions=self.repetitions,
            seed=self.seed,
            order_policy=self.order_policy,
            max_tool_calls=self.max_tool_calls,
            max_evidence_chars=self.max_evidence_chars,
            max_model_turns=self.max_model_turns,
            panel_horizons=self.panel_horizons,
        )

    def execution_policy(self) -> ExecutionPolicy:
        return ExecutionPolicy(
            target_weight=Decimal(self.target_weight),
            max_participation=Decimal(self.max_participation),
            minimum_notional=Decimal(self.minimum_notional),
        )

    def capital(self) -> tuple[Money, ...]:
        return tuple(
            Money(decimal_from_str(amount), currency) for currency, amount in self.starting_capital
        )

    def with_run_id(self, run_id: str) -> StudyConfig:
        return replace(self, run_id=run_id)

    # -- serialisation -------------------------------------------------------

    def to_payload(self) -> dict[str, Any]:
        """Canonical, round-trippable form. The thing that gets hashed."""
        return {
            "run_id": self.run_id,
            "world": self.world,
            "start_at": str(self.start_at),
            "sessions": self.sessions,
            "arms": [str(arm) for arm in self.arms],
            "repetitions": self.repetitions,
            "seed": self.seed,
            "order_policy": str(self.order_policy),
            "max_tool_calls": self.max_tool_calls,
            "max_evidence_chars": self.max_evidence_chars,
            "max_model_turns": self.max_model_turns,
            "panel": self.panel,
            "panel_horizons": list(self.panel_horizons),
            "recall_limit": self.recall_limit,
            "reflection_interval": self.reflection_interval,
            "target_weight": decimal_to_str(Decimal(self.target_weight)),
            "max_participation": decimal_to_str(Decimal(self.max_participation)),
            "minimum_notional": decimal_to_str(Decimal(self.minimum_notional)),
            "base_currency": self.base_currency,
            "starting_capital": [
                [currency, decimal_to_str(decimal_from_str(amount))]
                for currency, amount in self.starting_capital
            ],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> StudyConfig:
        """Rebuild a configuration from its canonical form or a config file.

        Unknown keys are rejected rather than ignored: a typo in a
        pre-registered parameter name that silently left the default in place
        would be a study running under different rules than its author
        believed.
        """
        known = {f for f in _FIELD_NAMES}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ConfigurationError(
                f"Unknown configuration keys: {unknown}. A misspelled parameter that "
                "silently kept its default would be a study running under rules "
                "nobody chose.",
                unknown=unknown,
            )
        missing = sorted({"run_id", "start_at", "sessions"} - set(payload))
        if missing:
            raise ConfigurationError(f"Missing required configuration keys: {missing}")

        defaults = cls(
            run_id=str(payload["run_id"]),
            start_at=parse_instant(str(payload["start_at"])),
            sessions=int(payload["sessions"]),
        )
        return replace(
            defaults,
            world=str(payload.get("world", defaults.world)),
            arms=_parse_arms(payload.get("arms"), defaults.arms),
            repetitions=int(payload.get("repetitions", defaults.repetitions)),
            seed=str(payload.get("seed", defaults.seed)),
            order_policy=OrderPolicy(payload.get("order_policy", defaults.order_policy)),
            max_tool_calls=int(payload.get("max_tool_calls", defaults.max_tool_calls)),
            max_evidence_chars=int(payload.get("max_evidence_chars", defaults.max_evidence_chars)),
            max_model_turns=int(payload.get("max_model_turns", defaults.max_model_turns)),
            panel=bool(payload.get("panel", defaults.panel)),
            panel_horizons=tuple(
                int(h) for h in payload.get("panel_horizons", defaults.panel_horizons)
            ),
            recall_limit=int(payload.get("recall_limit", defaults.recall_limit)),
            reflection_interval=int(
                payload.get("reflection_interval", defaults.reflection_interval)
            ),
            target_weight=str(payload.get("target_weight", defaults.target_weight)),
            max_participation=str(payload.get("max_participation", defaults.max_participation)),
            minimum_notional=str(payload.get("minimum_notional", defaults.minimum_notional)),
            base_currency=str(payload.get("base_currency", defaults.base_currency)),
            starting_capital=_parse_capital(
                payload.get("starting_capital"), defaults.starting_capital
            ),
        )

    @property
    def fingerprint(self) -> str:
        """Content hash of the whole configuration."""
        return canonical_hash(self.to_payload())


_FIELD_NAMES: Final = frozenset(StudyConfig.__dataclass_fields__)


def _parse_arms(value: Any, default: tuple[ArmId, ...]) -> tuple[ArmId, ...]:
    if value is None:
        return default
    names = [str(entry) for entry in value]
    unknown = [name for name in names if name not in {str(arm) for arm in ARMS}]
    if unknown:
        raise ConfigurationError(f"Unknown arms in configuration: {unknown}", unknown=unknown)
    return tuple(ArmId(name) for name in names)


def _parse_capital(value: Any, default: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    if value is None:
        return default
    if isinstance(value, Mapping):
        entries: Sequence[tuple[Any, Any]] = tuple(value.items())
    else:
        entries = tuple((pair[0], pair[1]) for pair in value)
    return tuple((str(currency), str(amount)) for currency, amount in entries)


class StudyRegistry:
    """Declares and reloads study configurations."""

    __slots__ = ("_blobs", "_clock", "_session")

    def __init__(self, session: Session, clock: Clock, blobs: BlobStore) -> None:
        self._session = session
        self._clock = clock
        self._blobs = blobs

    def declare(self, config: StudyConfig) -> StudyConfig:
        """Record a configuration, or verify it against the recorded one.

        Raises:
            IntegrityError: if this ``run_id`` was already declared with a
                different configuration. Continuing would mean the run's later
                cycles ran under rules its earlier cycles did not, with nothing
                in the record to say so.
        """
        fingerprint = config.fingerprint
        existing = self._session.get(RunRow, config.run_id)
        if existing is not None:
            if existing.config_hash != fingerprint:
                raise IntegrityError(
                    f"Run {config.run_id} was declared with configuration "
                    f"{existing.config_hash} and is now being run with {fingerprint}. "
                    "A pre-registered study cannot change its parameters mid-run; use "
                    "a new run_id.",
                    run_id=config.run_id,
                    declared=existing.config_hash,
                    supplied=fingerprint,
                )
            return config

        blob = self._blobs.put(canonical_bytes(config.to_payload()))
        self._session.add(
            RunRow(
                run_id=config.run_id,
                world=config.world,
                config_hash=fingerprint,
                config_blob_hash=blob.digest,
                declared_at=str(self._clock.now_instant()),
            )
        )
        self._session.flush()
        return config

    def load(self, run_id: str) -> StudyConfig | None:
        """The configuration a run was declared with, or ``None``."""
        row = self._session.get(RunRow, run_id)
        if row is None:
            return None
        return StudyConfig.from_payload(json.loads(self._blobs.get(row.config_blob_hash)))

    def require(self, run_id: str) -> StudyConfig:
        config = self.load(run_id)
        if config is None:
            raise ConfigurationError(
                f"No run declared with id {run_id!r} in this database. Runs must be "
                "declared before they can be resolved, analysed or replayed.",
                run_id=run_id,
            )
        return config
