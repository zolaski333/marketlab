"""The ``marketlab`` command line (§29).

One entry point, in the supported order
---------------------------------------
Every command here goes through :func:`marketlab.study.pipeline.open_study`,
which assembles the component graph once, and through
:class:`marketlab.experiments.driver.CycleDriver`, which runs a cycle's steps
in the one order the platform supports. A CLI that assembled its own graph
would be the fourth assembly in this repository, and the fourth is where two
of them start to differ in a way nobody notices.

Idempotent by default
---------------------
``run`` may be issued repeatedly on the same database. It resumes rather than
duplicating, because every layer beneath is idempotent on a derived
identifier. That is the property §30.6 asks for, and ``tests/cli/test_cli.py``
checks it by running a study twice and comparing the whole summary.

``--dry-run`` writes nothing at all
------------------------------------
Not "writes less": nothing. It parses and validates the configuration, reports
what would happen, and does not create the database, admit the universe, or
declare the run. Validating a configuration must not be the act that
pre-registers it.

Exit codes carry the outcome
----------------------------
See :class:`marketlab.cli.output.ExitCode`. A replay that finds a divergence
exits 4, not 0 with a warning in the text — this is a platform whose value is
that a failure cannot be mistaken for a success.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from marketlab.analysis.equivalence import DEFAULT_ALPHA, Rope
from marketlab.analysis.multiplicity import Correction
from marketlab.analysis.plan import AnalysisPlan
from marketlab.cli.output import Emitter, ExitCode
from marketlab.core.clock import SystemClock
from marketlab.core.failures import ConfigurationError, IntegrityError, MarketLabError
from marketlab.ingestion.synthetic import register_synthetic_calendars
from marketlab.instruments.calendars import CalendarRegistry
from marketlab.replay.verifier import ReplayConfig, ReplayVerifier
from marketlab.storage.blobs import BlobStore
from marketlab.storage.database import Database
from marketlab.storage.events import EventStore
from marketlab.study.config import StudyConfig, StudyRegistry
from marketlab.study.pipeline import Study, open_study

__all__ = ["app", "main"]

app = typer.Typer(
    name="marketlab",
    help=(
        "Virtual, reproducible market-agent research. No real money is ever "
        "moved and no investment advice is produced."
    ),
    no_args_is_help=True,
    add_completion=False,
)

_DB_OPTION = typer.Option("--db", help="Path to the study database.")
_JSON_OPTION = typer.Option("--json", help="Emit one canonical JSON object per record.")
_QUIET_OPTION = typer.Option("--quiet", help="Suppress human progress output.")


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _blob_root(db: Path) -> Path:
    """Blobs live beside the database they belong to."""
    return db.parent / f"{db.stem}-blobs"


def _load_config(path: Path) -> StudyConfig:
    """Read a YAML or JSON study configuration."""
    if not path.is_file():
        raise typer.BadParameter(f"No configuration file at {path}")
    text = path.read_text(encoding="utf-8")
    payload: Any = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise typer.BadParameter(
            f"{path} must contain a mapping of configuration keys, got {type(payload).__name__}."
        )
    return StudyConfig.from_payload(payload)


def _fail(emitter: Emitter, error: Exception, code: ExitCode) -> None:
    emitter.error(str(error), code=code)
    raise typer.Exit(int(code))


def _code_for(error: MarketLabError) -> ExitCode:
    """Map a platform failure to the code an operator would act on.

    A configuration mistake is fixed by editing a file; an integrity failure
    means the recorded data is not to be trusted until someone looks. Folding
    both into one code would make the difference invisible to a scheduler.
    """
    if isinstance(error, IntegrityError):
        return ExitCode.INTEGRITY
    if isinstance(error, ConfigurationError):
        return ExitCode.CONFIGURATION
    return ExitCode.FAILED


def _open(db: Path, run_id: str, emitter: Emitter) -> tuple[Database, Study]:
    """Reopen an existing study from its declared configuration."""
    if not db.exists():
        _fail(emitter, FileNotFoundError(f"No study database at {db}"), ExitCode.NO_DATA)
    database = Database(db)
    database.create_schema()
    session = database.session()
    blobs = BlobStore(_blob_root(db))
    clock = SystemClock()
    registry = StudyRegistry(session, clock, blobs)
    config = registry.load(run_id)
    if config is None:
        session.close()
        database.close()
        _fail(
            emitter,
            ValueError(f"No run declared with id {run_id!r} in {db}"),
            ExitCode.NO_DATA,
        )
        raise AssertionError  # pragma: no cover - _fail always raises
    return database, open_study(config, session=session, clock=clock, blobs=blobs, declare=False)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def run(
    config: Annotated[Path, typer.Option("--config", help="Study configuration (YAML or JSON).")],
    db: Annotated[Path, _DB_OPTION] = Path("data/study.db"),
    sessions: Annotated[
        int | None, typer.Option("--sessions", help="Stop after this many cycles.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and report; write nothing at all.")
    ] = False,
    as_json: Annotated[bool, _JSON_OPTION] = False,
    quiet: Annotated[bool, _QUIET_OPTION] = False,
) -> None:
    """Declare and run a study. Safe to repeat: it resumes, it does not redo."""
    emitter = Emitter(as_json=as_json, quiet=quiet)
    try:
        study_config = _load_config(config)
    except MarketLabError as error:
        _fail(emitter, error, ExitCode.CONFIGURATION)
        return

    if dry_run:
        _report_dry_run(emitter, study_config, db=db, sessions=sessions)
        return

    database = Database(db)
    database.create_schema()
    try:
        with database.session_scope() as session:
            blobs = BlobStore(_blob_root(db))
            study = open_study(study_config, session=session, clock=SystemClock(), blobs=blobs)
            planned = min(sessions, study_config.sessions) if sessions else study_config.sessions
            emitter.progress(
                f"running {study_config.run_id}: {len(study_config.arms)} arms x "
                f"{planned} of {study_config.sessions} sessions"
            )
            summary = study.run(sessions=sessions)
            emitter.table("run", summary.as_payload())
    except MarketLabError as error:
        _fail(emitter, error, _code_for(error))
    finally:
        database.close()


def _report_dry_run(
    emitter: Emitter, config: StudyConfig, *, db: Path, sessions: int | None
) -> None:
    """Say what would happen, and touch nothing.

    The declared configuration is only inspected if a database already exists;
    a dry run must not be the thing that creates one.
    """
    declared: str
    if db.exists():
        database = Database(db)
        try:
            with database.session_scope() as session:
                existing = StudyRegistry(session, SystemClock(), BlobStore(_blob_root(db))).load(
                    config.run_id
                )
            if existing is None:
                declared = "not declared"
            elif existing.fingerprint == config.fingerprint:
                declared = "already declared, identical"
            else:
                declared = "ALREADY DECLARED WITH A DIFFERENT CONFIGURATION"
        finally:
            database.close()
    else:
        declared = "no database yet"

    emitter.table(
        "dry-run",
        {
            "run_id": config.run_id,
            "world": config.world,
            "arms": ",".join(str(arm) for arm in config.arms),
            "repetitions": config.repetitions,
            "sessions": sessions if sessions is not None else config.sessions,
            "panel_horizons": ",".join(str(h) for h in config.panel_horizons),
            "fingerprint": config.fingerprint,
            "declared": declared,
            "writes": "none",
        },
    )


@app.command()
def resolve(
    run_id: Annotated[str, typer.Option("--run-id", help="Study to resolve.")],
    db: Annotated[Path, _DB_OPTION] = Path("data/study.db"),
    as_json: Annotated[bool, _JSON_OPTION] = False,
    quiet: Annotated[bool, _QUIET_OPTION] = False,
) -> None:
    """Resolve every forecast whose horizon has elapsed."""
    emitter = Emitter(as_json=as_json, quiet=quiet)
    database, study = _open(db, run_id, emitter)
    try:
        report = study.resolve()
        emitter.table(
            "resolve",
            {
                "run_id": run_id,
                "pending": len(report.pending),
                **{str(status): count for status, count in report.counts().items()},
            },
        )
        if not report.resolved and not report.pending:
            raise typer.Exit(int(ExitCode.NO_DATA))
    except MarketLabError as error:
        _fail(emitter, error, _code_for(error))
    finally:
        study.session.close()
        database.close()


@app.command()
def analyse(
    run_id: Annotated[str, typer.Option("--run-id", help="Study to analyse.")],
    rope_lower: Annotated[
        float,
        typer.Option(
            "--rope-lower",
            help="Lower bound of the pre-registered region of practical equivalence.",
        ),
    ],
    rope_upper: Annotated[float, typer.Option("--rope-upper", help="Upper bound of the ROPE.")],
    db: Annotated[Path, _DB_OPTION] = Path("data/study.db"),
    alpha: Annotated[float, typer.Option("--alpha", help="Per-test level.")] = DEFAULT_ALPHA,
    correction: Annotated[
        Correction, typer.Option("--correction", help="Multiplicity correction.")
    ] = Correction.HOLM,
    resamples: Annotated[int, typer.Option("--resamples", help="Bootstrap replicates.")] = 10_000,
    as_json: Annotated[bool, _JSON_OPTION] = False,
    quiet: Annotated[bool, _QUIET_OPTION] = False,
) -> None:
    """Run the pre-registered analysis.

    The ROPE has no default and is required, here as in the library: a region
    of practical equivalence chosen after seeing the data is not a
    pre-registration.
    """
    emitter = Emitter(as_json=as_json, quiet=quiet)
    database, study = _open(db, run_id, emitter)
    try:
        plan = AnalysisPlan(
            rope=Rope(rope_lower, rope_upper),
            horizons=study.config.panel_horizons,
            alpha=alpha,
            correction=correction,
            resamples=resamples,
            seed=f"{study.config.seed}|analysis",
        )
        report = study.analyse(plan)
        if not report.comparisons:
            emitter.table(
                "analyse",
                {"run_id": run_id, "comparisons": 0, "skipped": len(report.skipped)},
            )
            raise typer.Exit(int(ExitCode.NO_DATA))
        for comparison in report.comparisons:
            adjusted = report.adjusted_for(comparison.label)
            emitter.event(
                "comparison",
                label=comparison.label,
                question=comparison.contrast.question,
                estimate=comparison.equivalence.estimate,
                interval_low=comparison.equivalence.interval[0],
                interval_high=comparison.equivalence.interval[1],
                verdict=str(comparison.equivalence.verdict),
                p_two_sided=comparison.equivalence.p_two_sided,
                adjusted_p=adjusted.adjusted_p if adjusted else None,
                dates=comparison.dates,
                items=comparison.items,
                dropped=comparison.dropped,
            )
        for skipped in report.skipped:
            emitter.event("skipped", label=skipped.label, reason=skipped.reason)
        emitter.table(
            "analyse",
            {
                "run_id": run_id,
                "comparisons": len(report.comparisons),
                "skipped": len(report.skipped),
                "family_size": report.family_size,
                "correction": str(report.correction),
            },
        )
    except MarketLabError as error:
        _fail(emitter, error, _code_for(error))
    finally:
        study.session.close()
        database.close()


@app.command()
def replay(
    run_id: Annotated[str, typer.Option("--run-id", help="Study to replay.")],
    db: Annotated[Path, _DB_OPTION] = Path("data/study.db"),
    into: Annotated[
        Path | None, typer.Option("--into", help="Where to recompute. Defaults beside --db.")
    ] = None,
    as_json: Annotated[bool, _JSON_OPTION] = False,
    quiet: Annotated[bool, _QUIET_OPTION] = False,
) -> None:
    """Recompute a recorded run and compare it, artefact by artefact.

    Exits 4 on any divergence. A replay that found a difference and exited 0
    would be the defect this platform's whole audit history is about.
    """
    emitter = Emitter(as_json=as_json, quiet=quiet)
    database, study = _open(db, run_id, emitter)
    target_path = into if into is not None else db.parent / f"{db.stem}-replay.db"
    target = Database(target_path)
    target.create_schema()
    try:
        with target.session_scope() as replayed:
            calendars = CalendarRegistry()
            register_synthetic_calendars(calendars)
            report = ReplayVerifier(
                recorded=study.session,
                replayed=replayed,
                blobs=study.blobs,
                clock=study.clock,
                config=ReplayConfig(
                    run=study.config.run_config(),
                    calendars=calendars,
                    policy=study.config.execution_policy(),
                    base_currency=study.config.base_currency,
                ),
            ).verify()

        for divergence in report.divergences:
            emitter.event(
                "divergence",
                kind=divergence.kind,
                key=divergence.key,
                field=divergence.field_name,
                recorded=divergence.recorded,
                recomputed=divergence.recomputed,
            )
        emitter.table(
            "replay",
            {
                "run_id": run_id,
                "exact": report.is_exact,
                "compared": report.total_compared,
                "divergences": len(report.divergences),
                **{f"compared_{kind.lower()}": count for kind, count in report.compared.items()},
            },
        )
        if not report.is_exact:
            raise typer.Exit(int(ExitCode.INTEGRITY))
    except MarketLabError as error:
        _fail(emitter, error, _code_for(error))
    finally:
        study.session.close()
        database.close()
        target.close()


@app.command()
def verify(
    db: Annotated[Path, _DB_OPTION] = Path("data/study.db"),
    as_json: Annotated[bool, _JSON_OPTION] = False,
    quiet: Annotated[bool, _QUIET_OPTION] = False,
) -> None:
    """Re-derive every hash in the event chain."""
    emitter = Emitter(as_json=as_json, quiet=quiet)
    if not db.exists():
        _fail(emitter, FileNotFoundError(f"No study database at {db}"), ExitCode.NO_DATA)
    database = Database(db)
    database.create_schema()
    try:
        with database.session_scope() as session:
            checked = EventStore(session, SystemClock()).verify_chain()
        emitter.table("verify", {"database": str(db), "events_checked": checked})
        if checked == 0:
            raise typer.Exit(int(ExitCode.NO_DATA))
    except MarketLabError as error:
        _fail(emitter, error, _code_for(error))
    finally:
        database.close()


@app.command()
def status(
    run_id: Annotated[str, typer.Option("--run-id", help="Study to describe.")],
    db: Annotated[Path, _DB_OPTION] = Path("data/study.db"),
    as_json: Annotated[bool, _JSON_OPTION] = False,
    quiet: Annotated[bool, _QUIET_OPTION] = False,
) -> None:
    """Report what a study currently contains. Counted, never claimed."""
    emitter = Emitter(as_json=as_json, quiet=quiet)
    database, study = _open(db, run_id, emitter)
    try:
        summary = study.summary()
        emitter.table(
            "status",
            {
                **summary.as_payload(),
                "fingerprint": study.config.fingerprint,
                "planned_sessions": study.config.sessions,
                "arms": ",".join(str(arm) for arm in study.config.arms),
            },
        )
    finally:
        study.session.close()
        database.close()


def main() -> None:  # pragma: no cover - console-script shim
    app()
