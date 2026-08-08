"""The repository is fit to be published (§34).

Going public makes two new kinds of claim: a legal one (a licence others may
rely on) and a scientific one (a pre-registration whose credibility rests
entirely on it predating the data). Both are the sort of claim that is made
once, in a file nobody reads again, and is wrong for years without anyone
noticing.

So they are checked the same way every other claim in this repository is:
cheaply, by a machine, on every commit.

This file deliberately does **not** check that the documentation is good. It
checks that specific, load-bearing statements are present, consistent with each
other, and consistent with the code — the failures that would embarrass a
public repository rather than merely improve it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = (
    "LICENSE",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "CITATION.cff",
    ".gitattributes",
    ".gitignore",
    "docs/ROADMAP.md",
    "docs/PRE_REGISTRATION.md",
    "docs/adr/README.md",
)


def _read(relative: str) -> str:
    return (_REPO_ROOT / relative).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", REQUIRED_FILES)
def test_the_file_exists(relative: str) -> None:
    assert (_REPO_ROOT / relative).is_file(), f"{relative} is missing."


# ---------------------------------------------------------------------------
# Legally coherent
# ---------------------------------------------------------------------------


def test_the_licence_file_matches_the_licence_declared_in_metadata() -> None:
    """A package that advertises MIT and ships something else is a licensing
    problem for everyone downstream, and nobody reads both files at once."""
    declared = tomllib.loads(_read("pyproject.toml"))["project"]["license"]
    assert declared == {"text": "MIT"}
    licence = _read("LICENSE")
    assert licence.startswith("MIT License")
    assert "Copyright (c)" in licence
    assert "WITHOUT WARRANTY OF ANY KIND" in licence


def test_the_citation_metadata_is_valid_yaml_and_names_the_licence() -> None:
    citation = yaml.safe_load(_read("CITATION.cff"))
    assert citation["cff-version"].startswith("1.")
    assert citation["license"] == "MIT"
    assert citation["authors"]
    assert citation["title"].strip()


def test_the_same_person_is_named_as_author_in_all_three_places() -> None:
    """The copyright line, the package metadata and the citation file each
    state who this belongs to, in three different formats, and nobody reads
    them together. They drift, and the drift is invisible until someone tries
    to cite the work or reuse it.
    """
    citation = yaml.safe_load(_read("CITATION.cff"))
    author = citation["authors"][0]
    full_name = f"{author['given-names']} {author['family-names']}"

    assert full_name in _read("LICENSE"), "The LICENSE copyright names someone else."

    declared = tomllib.loads(_read("pyproject.toml"))["project"]["authors"]
    assert [entry["name"] for entry in declared] == [full_name]


def test_the_project_urls_point_at_the_repository_and_its_two_key_documents() -> None:
    """A public package whose metadata leads nowhere. The roadmap and the
    pre-registration are the two documents a reader needs to interpret any
    claim made here, so they are addressable rather than merely present."""
    urls = tomllib.loads(_read("pyproject.toml"))["project"]["urls"]
    assert urls["Repository"].startswith("https://github.com/")
    for key in ("Roadmap", "Pre-registration"):
        assert urls[key].startswith(urls["Repository"]), key


# ---------------------------------------------------------------------------
# Scientifically honest
# ---------------------------------------------------------------------------


def test_the_readme_states_that_no_real_money_is_involved() -> None:
    """The one claim about this platform that must never be ambiguous, and the
    first thing a stranger arriving from a search result will look for."""
    readme = _read("README.md")
    assert "No real money" in readme
    assert "no investment advice" in readme.lower()


def test_the_readme_warns_that_no_result_here_is_about_memory_yet() -> None:
    """The shipped model ignores the granted material by design. A reader who
    missed that could take an arm comparison from this repository at face
    value, and it would mean nothing."""
    readme = _read("README.md").lower()
    assert "deterministic fake" in readme
    assert "ignores the material" in readme


def test_the_readme_points_at_the_roadmap_as_the_only_place_completeness_is_claimed() -> None:
    assert "only place completeness is claimed" in _read("README.md")


def test_the_pre_registration_is_marked_not_yet_binding_while_decisions_are_open() -> None:
    """The document must not read as a finished pre-registration while the
    primary metric and the ROPE are still undecided. If someone fills those in
    and forgets this banner, that is a claim about the study's ordering that
    would be false."""
    text = _read("docs/PRE_REGISTRATION.md")
    open_items = text.count("**OPEN")
    banner = "not yet binding" in text.lower()
    assert banner == (open_items > 0), (
        "The pre-registration's status banner disagrees with its own contents: "
        f"{open_items} item(s) marked OPEN, banner present: {banner}."
    )


def test_the_pre_registration_names_the_confirmatory_contrasts() -> None:
    # The prime is built from its code point rather than written literally:
    # ruff flags U+2032 as an ambiguous character in source, while the
    # documents legitimately use the specification's own B-prime / C-prime
    # notation.
    prime = chr(0x2032)
    contrasts = ("B vs A", "D vs A", "C vs A", f"B vs B{prime}", f"C vs C{prime}")
    text = _read("docs/PRE_REGISTRATION.md")
    for contrast in contrasts:
        assert contrast in text, f"{contrast} is not named in the pre-registration."


def test_the_pre_registration_explains_how_it_binds_to_a_run() -> None:
    """A pre-registration nothing enforces is a document. The fingerprint
    mechanism is what makes it a constraint."""
    text = _read("docs/PRE_REGISTRATION.md").lower()
    assert "fingerprint" in text
    assert "run_id" in text


# ---------------------------------------------------------------------------
# Internally consistent
# ---------------------------------------------------------------------------


def test_every_document_the_readme_links_to_exists() -> None:
    """A broken link in the first file a stranger opens."""
    readme = _read("README.md")
    for target in re.findall(r"\]\(([^)#][^)]*)\)", readme):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        assert (_REPO_ROOT / target).exists(), f"README links to missing {target}"


def test_the_security_policy_covers_the_three_structural_defences() -> None:
    text = _read("SECURITY.md")
    assert "test_injection_routing.py" in text
    assert "APPEND_ONLY_TABLES" in text
    assert "test_decision_path_isolation.py" in text


def test_the_contributing_guide_states_the_gates_and_the_roadmap_rule() -> None:
    text = _read("CONTRIBUTING.md")
    for command in ("ruff check", "ruff format --check", "mypy src", "pytest"):
        assert command in text
    assert "docs/ROADMAP.md" in text


def test_the_adr_directory_admits_it_is_empty_rather_than_pretending() -> None:
    """An empty directory git would not even publish. The README in it is what
    makes the gap visible to someone who clones."""
    adr = _REPO_ROOT / "docs" / "adr"
    records = [path for path in adr.glob("*.md") if path.name != "README.md"]
    text = _read("docs/adr/README.md")
    assert (len(records) == 0) == ("empty on purpose" in text), (
        "docs/adr/README.md claims the directory is empty but it is not, or vice versa."
    )


# ---------------------------------------------------------------------------
# Reproducible from a clean checkout
# ---------------------------------------------------------------------------


def test_line_endings_are_normalised_for_the_linux_leg_of_ci() -> None:
    """`ruff format --check` compares bytes, and the matrix runs on two
    operating systems. A file stored with CRLF passes the formatting gate on
    one and fails on the other, for reasons unrelated to the code."""
    assert "* text=auto eol=lf" in _read(".gitattributes")


def test_the_python_version_is_pinned_for_uv() -> None:
    assert _read(".python-version").strip() == "3.12"
    requires = tomllib.loads(_read("pyproject.toml"))["project"]["requires-python"]
    assert requires == ">=3.12"


def test_the_lock_file_is_committed_so_frozen_installs_mean_something() -> None:
    assert (_REPO_ROOT / "uv.lock").is_file()
    assert "--frozen" in _read(".github/workflows/ci.yml")


def test_the_gitignore_makes_no_claim_about_files_that_do_not_exist() -> None:
    """It used to promise a committed fixture directory and an export command,
    and neither existed. A comment that describes a repository other than this
    one is how a reader's model of the project goes wrong."""
    ignored = _read(".gitignore")
    assert "data/fixtures" not in ignored
    assert "marketlab export release" not in ignored


def test_the_example_configuration_actually_loads() -> None:
    """It is the first command in the README. A stranger's very first
    invocation must not fail on a typo in a file nobody re-reads."""
    from marketlab.study.config import StudyConfig

    for path in sorted((_REPO_ROOT / "configs").glob("*.yaml")):
        config = StudyConfig.from_payload(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert config.fingerprint
        assert config.run_config()
        assert config.execution_policy()
