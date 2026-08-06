"""Structural enforcement of INV-P5: no future data leakage.

§6.3 requires the scientific read path to be *structurally* incapable of
returning data dated after its cutoff — not incapable by convention, but by
construction. The mechanism is narrow: packages on the decision path
(``agents``, ``retrieval``, ``forecasting``) are forbidden from importing
SQLAlchemy directly. If they cannot hold a raw database session, they cannot
write a query that forgets to filter by ``Cutoff`` — every read has to go
through a repository (``instruments``, ``snapshots``, ...) whose methods
already bake the cutoff in (see ``marketlab.core.cutoff``).

This is an AST scan, not a hand-maintained rule: it runs on every test
invocation and fails the moment a future change adds a raw
``from sqlalchemy import ...`` to one of these packages, long before that
import could grow into an actual leak.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src" / "marketlab"

DECISION_PATH_PACKAGES = ("agents", "retrieval", "forecasting")


def _forbidden_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits.extend(
                alias.name
                for alias in node.names
                if alias.name == "sqlalchemy" or alias.name.startswith("sqlalchemy.")
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "sqlalchemy" or node.module.startswith("sqlalchemy."))
        ):
            hits.append(node.module)
    return hits


@pytest.mark.parametrize("package", DECISION_PATH_PACKAGES)
def test_decision_path_package_exists(package: str) -> None:
    # A protected package that does not exist gives the false impression of
    # protection — the same failure mode APPEND_ONLY_TABLES guards against
    # for tables (see marketlab.storage.database).
    assert (_SRC_ROOT / package).is_dir(), (
        f"Expected src/marketlab/{package}/ to exist. If it was renamed, update "
        "DECISION_PATH_PACKAGES in this test alongside it."
    )


@pytest.mark.parametrize("package", DECISION_PATH_PACKAGES)
def test_decision_path_package_never_imports_sqlalchemy_directly(package: str) -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted((_SRC_ROOT / package).rglob("*.py")):
        hits = _forbidden_imports(path)
        if hits:
            offenders[str(path.relative_to(_REPO_ROOT))] = hits

    assert not offenders, (
        f"{package}/ imports SQLAlchemy directly: {offenders}. Reads on the "
        "decision path must go through a Cutoff-bound repository instead, so "
        "that a future query cannot forget the point-in-time filter (INV-P5)."
    )
