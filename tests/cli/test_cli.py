"""Tests for the ``marketlab`` command line (§29).

The properties checked here are the ones an operator depends on and cannot see
by reading the output: that ``--dry-run`` writes nothing at all, that a repeat
``run`` resumes rather than duplicating, that a run cannot be re-declared with
different parameters, and that every failure mode exits with a code a pipeline
can branch on rather than 0 with a warning in the text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text
from typer.testing import CliRunner, Result

from marketlab.cli.main import app
from marketlab.cli.output import ExitCode
from marketlab.storage.database import Database

RUN_ID = "CLI_TEST_RUN"
runner = CliRunner()

# Small on purpose: these tests are about the command surface, not about the
# science, and the pipeline itself is exercised end to end in tests/integration.
BASE_CONFIG: dict[str, object] = {
    "run_id": RUN_ID,
    "start_at": "2026-08-01T00:00:00.000000Z",
    "sessions": 5,
    "arms": ["A", "B"],
    "panel_horizons": [1, 2],
    "starting_capital": [["USD", "1000000.00"], ["EUR", "500000.00"]],
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


def _config(workspace: Path, **overrides: object) -> Path:
    payload = {**BASE_CONFIG, **overrides}
    path = workspace / "study.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _db(workspace: Path) -> Path:
    return workspace / "study.db"


def _run_study(workspace: Path, **overrides: object) -> Result:
    return runner.invoke(
        app,
        ["run", "--config", str(_config(workspace, **overrides)), "--db", str(_db(workspace))],
    )


def _json_records(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Help and usage
# ---------------------------------------------------------------------------


def test_the_top_level_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == ExitCode.OK
    for command in ("run", "resolve", "analyse", "replay", "verify", "status"):
        assert command in result.stdout


def test_the_help_states_that_no_real_money_is_involved() -> None:
    """The one claim about this platform that must never be ambiguous."""
    assert "No real money" in runner.invoke(app, ["--help"]).stdout


def test_every_command_has_its_own_help() -> None:
    for command in ("run", "resolve", "analyse", "replay", "verify", "status"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == ExitCode.OK, command
        assert "Usage:" in result.stdout, command


def test_an_unknown_command_is_a_usage_error() -> None:
    assert runner.invoke(app, ["frobnicate"]).exit_code == ExitCode.USAGE


def test_analysing_without_a_rope_is_a_usage_error() -> None:
    """The library refuses to default a ROPE; so does the command line."""
    result = runner.invoke(app, ["analyse", "--run-id", RUN_ID])
    assert result.exit_code == ExitCode.USAGE


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_a_dry_run_creates_no_database_at_all(workspace: Path) -> None:
    """Not 'writes less': nothing. Validating a configuration must not be the
    act that pre-registers it."""
    result = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(_config(workspace)),
            "--db",
            str(_db(workspace)),
            "--dry-run",
        ],
    )
    assert result.exit_code == ExitCode.OK
    assert not _db(workspace).exists()
    assert list(workspace.glob("*.db")) == []


def test_a_dry_run_reports_the_configuration_fingerprint(workspace: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "--config", str(_config(workspace)), "--db", str(_db(workspace)), "--dry-run"],
    )
    assert RUN_ID in result.stdout
    assert "writes" in result.stdout


def test_a_dry_run_warns_when_the_run_is_already_declared_differently(
    workspace: Path,
) -> None:
    assert _run_study(workspace).exit_code == ExitCode.OK
    result = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(_config(workspace, sessions=9)),
            "--db",
            str(_db(workspace)),
            "--dry-run",
        ],
    )
    assert result.exit_code == ExitCode.OK
    assert "DIFFERENT CONFIGURATION" in result.stdout


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_a_run_produces_decisions_panels_and_orders(workspace: Path) -> None:
    result = _run_study(workspace)
    assert result.exit_code == ExitCode.OK, result.stdout + result.stderr
    assert "decisions" in result.stdout


def test_running_twice_resumes_rather_than_duplicating(workspace: Path) -> None:
    """§30.6. Every layer beneath is idempotent on a derived identifier, so the
    whole study is — and the summary is identical to the row, not merely
    similar."""
    first = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(_config(workspace)),
            "--db",
            str(_db(workspace)),
            "--json",
        ],
    )
    second = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(_config(workspace)),
            "--db",
            str(_db(workspace)),
            "--json",
        ],
    )
    assert first.exit_code == second.exit_code == ExitCode.OK
    assert _json_records(first.stdout) == _json_records(second.stdout)


def test_stopping_early_and_continuing_reaches_the_same_place(workspace: Path) -> None:
    partial = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(_config(workspace)),
            "--db",
            str(_db(workspace)),
            "--sessions",
            "2",
            "--json",
        ],
    )
    assert _json_records(partial.stdout)[0]["cycles"] == 2

    complete = runner.invoke(
        app,
        ["run", "--config", str(_config(workspace)), "--db", str(_db(workspace)), "--json"],
    )
    assert _json_records(complete.stdout)[0]["cycles"] == 5


def test_re_declaring_a_run_with_different_parameters_is_refused(workspace: Path) -> None:
    """A study whose parameters could be edited between cycles is not
    pre-registered; it is a study tuned while its results were visible."""
    assert _run_study(workspace).exit_code == ExitCode.OK
    result = _run_study(workspace, target_weight="0.20")
    assert result.exit_code == ExitCode.INTEGRITY
    assert "cannot change its parameters" in result.stderr


def test_a_misspelled_configuration_key_is_refused(workspace: Path) -> None:
    """A typo that silently kept a default would be a study running under
    rules nobody chose."""
    result = _run_study(workspace, target_wieght="0.20")
    assert result.exit_code == ExitCode.CONFIGURATION
    assert "Unknown configuration keys" in result.stderr


def test_an_invalid_parameter_is_refused_before_anything_is_written(
    workspace: Path,
) -> None:
    result = _run_study(workspace, target_weight="5")
    assert result.exit_code == ExitCode.CONFIGURATION
    assert not _db(workspace).exists()


def test_a_missing_configuration_file_is_a_usage_error(workspace: Path) -> None:
    result = runner.invoke(
        app, ["run", "--config", str(workspace / "absent.yaml"), "--db", str(_db(workspace))]
    )
    assert result.exit_code == ExitCode.USAGE


# ---------------------------------------------------------------------------
# The rest of the pipeline
# ---------------------------------------------------------------------------


@pytest.fixture
def completed(workspace: Path) -> Path:
    assert _run_study(workspace).exit_code == ExitCode.OK
    return workspace


def test_status_counts_what_is_there(completed: Path) -> None:
    result = runner.invoke(
        app, ["status", "--run-id", RUN_ID, "--db", str(_db(completed)), "--json"]
    )
    assert result.exit_code == ExitCode.OK
    (record,) = _json_records(result.stdout)
    assert record["cycles"] == 5
    assert record["decisions"] == 10  # two arms
    assert record["panels"] == 10


def test_resolve_reports_each_status(completed: Path) -> None:
    result = runner.invoke(
        app, ["resolve", "--run-id", RUN_ID, "--db", str(_db(completed)), "--json"]
    )
    assert result.exit_code == ExitCode.OK
    (record,) = _json_records(result.stdout)
    assert record["RESOLVED"] > 0
    assert record["UNRESOLVABLE"] == 0


def test_analyse_runs_the_pre_registered_family(completed: Path) -> None:
    runner.invoke(app, ["resolve", "--run-id", RUN_ID, "--db", str(_db(completed))])
    result = runner.invoke(
        app,
        [
            "analyse",
            "--run-id",
            RUN_ID,
            "--db",
            str(_db(completed)),
            "--rope-lower",
            "-0.01",
            "--rope-upper",
            "0.01",
            "--resamples",
            "200",
            "--json",
        ],
    )
    assert result.exit_code == ExitCode.OK
    records = _json_records(result.stdout)
    comparisons = [r for r in records if r["event"] == "comparison"]
    assert comparisons
    assert all(r["verdict"] == "EQUIVALENT" for r in comparisons)


def test_analysing_before_resolving_says_there_is_no_data(completed: Path) -> None:
    """Distinct from success with zero comparisons: nothing was analysed
    because nothing has been resolved, and a scheduler must be able to tell."""
    result = runner.invoke(
        app,
        [
            "analyse",
            "--run-id",
            RUN_ID,
            "--db",
            str(_db(completed)),
            "--rope-lower",
            "-0.01",
            "--rope-upper",
            "0.01",
        ],
    )
    assert result.exit_code == ExitCode.NO_DATA


def test_verify_walks_the_whole_chain(completed: Path) -> None:
    result = runner.invoke(app, ["verify", "--db", str(_db(completed)), "--json"])
    assert result.exit_code == ExitCode.OK
    (record,) = _json_records(result.stdout)
    assert record["events_checked"] > 0


def test_replay_of_an_intact_run_is_exact(completed: Path) -> None:
    runner.invoke(app, ["resolve", "--run-id", RUN_ID, "--db", str(_db(completed))])
    result = runner.invoke(
        app, ["replay", "--run-id", RUN_ID, "--db", str(_db(completed)), "--json"]
    )
    assert result.exit_code == ExitCode.OK
    (record,) = _json_records(result.stdout)
    assert record["exact"] is True
    assert record["compared"] > 0


def test_replay_of_a_tampered_run_exits_on_the_integrity_code(completed: Path) -> None:
    """A replay that found a difference and exited 0 would be the defect this
    platform's whole audit history is about."""
    database = Database(_db(completed))
    with database.migration_mode(reason="test: corrupt a fill", author="test-suite") as engine:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE fills SET price = '1.00' WHERE fill_id = "
                    "(SELECT fill_id FROM fills ORDER BY fill_id LIMIT 1)"
                )
            )
    database.close()

    result = runner.invoke(
        app, ["replay", "--run-id", RUN_ID, "--db", str(_db(completed)), "--json"]
    )
    assert result.exit_code == ExitCode.INTEGRITY
    assert any(record["event"] == "divergence" for record in _json_records(result.stdout))


# ---------------------------------------------------------------------------
# Missing things
# ---------------------------------------------------------------------------


def test_acting_on_an_undeclared_run_says_there_is_no_data(completed: Path) -> None:
    for command in ("status", "resolve", "replay"):
        result = runner.invoke(
            app, [command, "--run-id", "NEVER_DECLARED", "--db", str(_db(completed))]
        )
        assert result.exit_code == ExitCode.NO_DATA, command


def test_acting_on_a_missing_database_says_there_is_no_data(workspace: Path) -> None:
    result = runner.invoke(app, ["verify", "--db", str(workspace / "absent.db")])
    assert result.exit_code == ExitCode.NO_DATA


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


def test_json_output_is_one_canonical_object_per_line(completed: Path) -> None:
    result = runner.invoke(
        app, ["status", "--run-id", RUN_ID, "--db", str(_db(completed)), "--json"]
    )
    for line in result.stdout.splitlines():
        assert line.strip()
        parsed = json.loads(line)
        assert isinstance(parsed, dict)
        assert "event" in parsed


def test_json_mode_keeps_progress_off_stdout(completed: Path) -> None:
    """So ``marketlab status --json | jq`` works. A tool that interleaved both
    would produce a log that is neither readable nor parseable."""
    result = runner.invoke(
        app,
        ["run", "--config", str(_config(completed)), "--db", str(_db(completed)), "--json"],
    )
    assert result.stdout.strip()
    for line in result.stdout.splitlines():
        json.loads(line)


def test_quiet_suppresses_human_output_but_not_the_exit_code(completed: Path) -> None:
    result = runner.invoke(
        app, ["status", "--run-id", RUN_ID, "--db", str(_db(completed)), "--quiet"]
    )
    assert result.exit_code == ExitCode.OK
    assert result.stdout.strip() == ""


def test_errors_go_to_stderr_in_json_mode(workspace: Path) -> None:
    """Results stay on stdout so a consumer's parse is never poisoned by a
    failure message."""
    result = runner.invoke(
        app, ["status", "--run-id", "NOPE", "--db", str(workspace / "absent.db"), "--json"]
    )
    assert result.stdout.strip() == ""
    assert json.loads(result.stderr.strip())["event"] == "error"
