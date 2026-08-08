"""The quality gates are installed, configured and actually run (§30.10).

This is the file that answers the audit finding this whole project exists
because of. The previous implementation shipped a validation report claiming
its ``ruff`` and ``mypy`` gates passed. **Those gates were never installed.**
Nothing in the repository could have contradicted the claim, because nothing
checked it.

So the claim is checked here, three ways, all of them cheap to falsify:

* the tools are **declared** as dependencies, with versions pinned in the lock
  file;
* the tools are **importable and runnable in this very interpreter**, which is
  the only sense of "installed" that means anything;
* the CI workflow **invokes each of them**, so passing gates locally is not
  the same as passing them nowhere.

None of this proves the code is correct. It proves that a claim about the
gates can be contradicted by a machine, which is the property the original
report lacked.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The five commands docs/ROADMAP.md names as the gates.
GATE_COMMANDS = (
    "uv sync",
    "ruff check",
    "ruff format --check",
    "mypy src",
    "pytest",
)

REQUIRED_DEV_TOOLS = ("pytest", "hypothesis", "ruff", "mypy")


def _pyproject() -> dict[str, object]:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Declared
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", REQUIRED_DEV_TOOLS)
def test_every_gate_tool_is_a_declared_dependency(tool: str) -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    dev = project["optional-dependencies"]["dev"]  # type: ignore[index]
    assert any(str(entry).startswith(tool) for entry in dev), (
        f"{tool} is used as a quality gate but is not declared in the dev extra. "
        "A gate nobody installs is a gate nobody runs."
    )


def test_the_dependency_versions_are_locked() -> None:
    """``uv sync --frozen`` in CI is only meaningful against a lock file."""
    lock = _REPO_ROOT / "uv.lock"
    assert lock.is_file()
    for tool in REQUIRED_DEV_TOOLS:
        assert f'name = "{tool}"' in lock.read_text(encoding="utf-8")


def test_mypy_runs_in_strict_mode_on_the_scientific_core() -> None:
    """Strict is the setting that makes the type checker load-bearing. Without
    it, an untyped function body is silently unchecked."""
    config = _pyproject()["tool"]["mypy"]  # type: ignore[index]
    assert config["strict"] is True
    assert config["packages"] == ["marketlab"]


def test_pytest_refuses_unknown_markers_and_config() -> None:
    """``--strict-markers`` turns a typo in a marker into an error instead of a
    silently skipped selection."""
    options = _pyproject()["tool"]["pytest"]["ini_options"]  # type: ignore[index]
    assert "--strict-markers" in options["addopts"]
    assert "--strict-config" in options["addopts"]


def test_warnings_are_errors() -> None:
    """A deprecation warning nobody reads is how a platform ends up depending
    on behaviour that has already been removed elsewhere."""
    options = _pyproject()["tool"]["pytest"]["ini_options"]  # type: ignore[index]
    assert options["filterwarnings"] == ["error"]


# ---------------------------------------------------------------------------
# Installed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ("ruff", "mypy"))
def test_the_gate_tool_is_actually_executable_here(tool: str) -> None:
    """The check the original report could not have survived: run the thing.

    Not "is it in pyproject" — is it on this interpreter's path, right now,
    and does it answer.
    """
    executable = shutil.which(tool)
    assert executable is not None, (
        f"{tool} is declared but not installed in this environment. This is "
        "precisely the state the predecessor's validation report described as "
        "passing."
    )
    result = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert tool in result.stdout.lower()


def test_hypothesis_is_importable_so_the_property_suite_can_run() -> None:
    import hypothesis

    assert hypothesis.__version__


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def test_a_continuous_integration_workflow_exists() -> None:
    assert _WORKFLOW.is_file(), (
        "Gates that only run when someone remembers to run them are not gates."
    )


@pytest.mark.parametrize("command", GATE_COMMANDS)
def test_the_workflow_runs_every_gate(command: str) -> None:
    assert command in _workflow_text(), f"CI never runs `{command}`."


def test_the_workflow_is_valid_yaml_with_at_least_one_job() -> None:
    workflow = yaml.safe_load(_workflow_text())
    assert workflow["jobs"]
    for job in workflow["jobs"].values():
        assert job["steps"]


def test_the_workflow_runs_on_both_operating_systems() -> None:
    """Session boundaries are derived through zoneinfo, and Linux reads the
    system tz database while Windows relies on the tzdata package. A DST bug
    on one platform only is exactly the kind this study cannot afford."""
    workflow = yaml.safe_load(_workflow_text())
    systems = {
        system
        for job in workflow["jobs"].values()
        for system in job.get("strategy", {}).get("matrix", {}).get("os", [])
    }
    assert {"ubuntu-latest", "windows-latest"} <= systems


def test_the_workflow_exercises_the_command_line_end_to_end() -> None:
    """The CLI is a deliverable. A replay step that exits non-zero on any
    divergence makes CI a reproducibility check, not a smoke test."""
    text = _workflow_text()
    for command in ("marketlab run", "marketlab resolve", "marketlab replay"):
        assert command in text, f"CI never exercises `{command}`."


def test_the_roadmap_names_the_same_gates() -> None:
    """The single place completeness is claimed must not drift from the place
    it is checked."""
    roadmap = (_REPO_ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    for command in GATE_COMMANDS:
        assert command in roadmap, f"docs/ROADMAP.md no longer names `{command}`."
