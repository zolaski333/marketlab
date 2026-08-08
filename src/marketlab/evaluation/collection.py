"""Gathering everything a run forecast, so it can be resolved (§20.1).

Two elicitations produce probabilities, and they are not interchangeable:

``PANEL``
    The imposed questions. Every condition was asked the same ones at the same
    instant, so these — and only these — can be paired across arms.
``DECISION``
    Whatever the condition chose to forecast on its own. Resolved for
    completeness and for per-arm calibration, never paired: two arms that
    forecast different instruments produce numbers that are not comparable,
    and averaging them anyway would compare choices of subject rather than
    quality of judgement.

Both are read back from sealed bundles, never from anything held in memory
during the run. Resolution therefore works identically on a database produced
an hour ago and one produced by a replay, which is what makes §12.5's
comparison possible at all.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketlab.core.instants import Instant
from marketlab.evaluation.panels import PanelStore
from marketlab.evaluation.resolution import (
    ForecastSource,
    PendingForecast,
    forecast_id_for,
)
from marketlab.experiments.runner import DecisionBundleRow, outcome_from_payload
from marketlab.storage.blobs import BlobStore

__all__ = ["ForecastCollector"]


class ForecastCollector:
    """Reads one run's elicited probabilities back out of storage."""

    __slots__ = ("_blobs", "_panels", "_session")

    def __init__(self, session: Session, blobs: BlobStore, panels: PanelStore) -> None:
        self._session = session
        self._blobs = blobs
        self._panels = panels

    def collect(
        self,
        run_id: str,
        *,
        sources: Iterable[ForecastSource] = (ForecastSource.PANEL, ForecastSource.DECISION),
    ) -> tuple[PendingForecast, ...]:
        wanted = set(sources)
        forecasts: list[PendingForecast] = []
        if ForecastSource.PANEL in wanted:
            forecasts.extend(self._from_panels(run_id))
        if ForecastSource.DECISION in wanted:
            forecasts.extend(self._from_decisions(run_id))
        return tuple(forecasts)

    def _from_panels(self, run_id: str) -> list[PendingForecast]:
        collected: list[PendingForecast] = []
        for record in self._panels.for_run(run_id):
            for answer in record.outcome.answers:
                collected.append(
                    PendingForecast(
                        forecast_id=forecast_id_for(
                            ForecastSource.PANEL,
                            panel_bundle_id=record.panel_bundle_id,
                            item_id=answer.item_id,
                        ),
                        source=ForecastSource.PANEL,
                        source_bundle_id=record.panel_bundle_id,
                        arm_id=record.arm_id,
                        repetition=record.repetition,
                        instrument_id=answer.instrument_id,
                        horizon_sessions=answer.horizon_sessions,
                        probability_up=answer.probability_up,
                        anchor_at=record.as_of,
                    )
                )
        return collected

    def _from_decisions(self, run_id: str) -> list[PendingForecast]:
        rows = self._session.execute(
            select(DecisionBundleRow)
            .where(DecisionBundleRow.run_id == run_id)
            .order_by(
                DecisionBundleRow.as_of.asc(),
                DecisionBundleRow.arm_id.asc(),
                DecisionBundleRow.repetition.asc(),
            )
        ).scalars()

        collected: list[PendingForecast] = []
        for row in rows:
            outcome = outcome_from_payload(json.loads(self._blobs.get(row.payload_blob_hash)))
            for ordinal, forecast in enumerate(outcome.forecasts):
                # ``ordinal`` discriminates: a model may legitimately emit two
                # forecasts for the same instrument and horizon, and merging
                # them on a derived id would silently drop the second — the
                # exact under-counting derive_id's docstring warns about.
                collected.append(
                    PendingForecast(
                        forecast_id=forecast_id_for(
                            ForecastSource.DECISION,
                            decision_bundle_id=row.bundle_id,
                            ordinal=ordinal,
                        ),
                        source=ForecastSource.DECISION,
                        source_bundle_id=row.bundle_id,
                        arm_id=row.arm_id,
                        repetition=row.repetition,
                        instrument_id=forecast.instrument_id,
                        horizon_sessions=forecast.horizon_sessions,
                        probability_up=forecast.probability_up,
                        anchor_at=Instant(row.as_of),
                    )
                )
        return collected
