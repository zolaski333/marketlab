"""The documents still describe this code (§5, task #5).

Documentation rots differently from code. Code that stops being true usually
stops working; a document that stops being true keeps rendering, keeps getting
linked, and keeps being believed. The predecessor's validation report is the
worst case of that failure: it described a repository that did not exist, and
nothing in that repository could contradict it.

So the load-bearing documents are checked the same way every other claim here
is — cheaply, by a machine, on every commit:

* the data dictionary is checked against ``Base.metadata``, table by table and
  column by column, in both directions;
* the failure policy is checked against every member of every taxonomy enum;
* the scientific protocol is checked against the declared arms;
* every architecture decision record carries its five sections and is indexed;
* every relative link in ``docs/`` resolves.

What this file deliberately does **not** check is whether any of it is *good*,
or true in the sense that matters. A data dictionary can name every column and
describe each one wrongly. That is what review is for. What a machine can do is
notice the drift, which is the failure that happens without anyone deciding to
let it.

Each group of checks is followed by a vacuity test: the check applied to
something that should not be found. Without those, a parser that silently
matched nothing would make every test in this file pass forever.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from marketlab.cli.output import ExitCode
from marketlab.core.failures import (
    AgentFailureKind,
    FailureScope,
    RequirementLevel,
    SnapshotStatus,
)
from marketlab.execution.types import RejectionReason
from marketlab.experiments.arms import ARMS, ArmId
from marketlab.storage import schema  # noqa: F401  (registers every table)
from marketlab.storage.base import Base
from marketlab.storage.database import APPEND_ONLY_TABLES

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _REPO_ROOT / "docs"
_ADR = _DOCS / "adr"

REQUIRED_DOCUMENTS = (
    "ARCHITECTURE.md",
    "SCIENTIFIC_PROTOCOL.md",
    "DATA_DICTIONARY.md",
    "THREAT_MODEL.md",
    "FAILURE_POLICY.md",
    "PROVIDER_POLICY.md",
    "REPRODUCIBILITY.md",
    "LIMITATIONS.md",
    "ROADMAP.md",
    "PRE_REGISTRATION.md",
    "POWER.md",
)

ADR_SECTIONS = (
    "## Context",
    "## Options considered",
    "## Decision",
    "## Consequences",
    "## What would make us revisit this",
)


def _read(relative: str) -> str:
    return (_DOCS / relative).read_text(encoding="utf-8")


def _adr_files() -> list[Path]:
    return sorted(path for path in _ADR.glob("*.md") if path.name != "README.md")


@pytest.mark.parametrize("name", REQUIRED_DOCUMENTS)
def test_the_document_exists(name: str) -> None:
    assert (_DOCS / name).is_file(), f"docs/{name} is missing."


# ---------------------------------------------------------------------------
# The data dictionary describes the schema that exists
# ---------------------------------------------------------------------------


def _documented_tables() -> dict[str, str]:
    """Table name -> the text of its section in the data dictionary."""
    text = _read("DATA_DICTIONARY.md")
    heading = re.compile(r"^### `([a-z_]+)`", re.MULTILINE)
    sections: dict[str, str] = {}
    matches = list(heading.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.start() : end]
    return sections


def test_the_data_dictionary_documents_every_table_and_invents_none() -> None:
    """Both directions matter. A missing table is an undocumented one; an extra
    table is a document describing a schema this code does not have, which is
    the more misleading of the two."""
    documented = set(_documented_tables())
    actual = set(Base.metadata.tables)
    assert documented == actual, (
        f"undocumented tables: {sorted(actual - documented)}; "
        f"documented but absent from the schema: {sorted(documented - actual)}"
    )


@pytest.mark.parametrize("table_name", sorted(Base.metadata.tables))
def test_every_column_is_named_in_its_own_section(table_name: str) -> None:
    """Scoped per table rather than searched across the whole file: `run_id`
    appears in eight sections, so a global search would let a genuinely missing
    column pass because some other table happens to have one by that name."""
    section = _documented_tables()[table_name]
    missing = [
        column.name
        for column in Base.metadata.tables[table_name].columns
        if f"`{column.name}`" not in section
    ]
    assert not missing, f"{table_name}: columns not described: {missing}"


def test_the_data_dictionary_states_that_every_table_is_append_only() -> None:
    """The claim is only worth making while it is true of all of them. If a
    mutable table is ever added, this fails and the sentence has to change."""
    assert set(APPEND_ONLY_TABLES) == set(Base.metadata.tables)
    assert f"All {len(APPEND_ONLY_TABLES)} tables are append-only" in _read("DATA_DICTIONARY.md")


def test_the_column_scan_would_notice_a_column_that_is_not_described() -> None:
    """Guards the parametrised test above. If the section parser ever returned
    empty text — a changed heading style, a renamed file — every column would
    be reported as missing and the suite would be loud. The opposite failure,
    a parser that matched everything, is what this catches."""
    section = _documented_tables()["events"]
    assert "`a_column_that_does_not_exist`" not in section
    assert "`previous_hash`" in section


# ---------------------------------------------------------------------------
# The failure policy describes the taxonomy that exists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "member",
    [
        *list(AgentFailureKind),
        *list(FailureScope),
        *list(RequirementLevel),
        *list(SnapshotStatus),
        *list(RejectionReason),
    ],
    ids=str,
)
def test_every_taxonomy_member_is_named_in_the_failure_policy(member: object) -> None:
    """A failure kind added to the code and not to the policy is a category
    with no stated consequence — which is how a taxonomy stops being a policy
    and becomes a list of words.

    Matched on a word boundary, not as a substring. A mutation run caught this:
    renaming the policy's ``NOTHING_TO_SELL`` row to ``NOTHING_TO_SELL_RENAMED``
    left the old name present as a prefix, and a substring check passed on a
    document that no longer described the member at all.
    """
    pattern = rf"\b{re.escape(str(member))}\b"
    assert re.search(pattern, _read("FAILURE_POLICY.md")), (
        f"{member} is declared in the code but the failure policy does not mention it."
    )


@pytest.mark.parametrize("code", list(ExitCode), ids=str)
def test_every_exit_code_is_documented_with_its_number(code: ExitCode) -> None:
    """Callers script against the number, not the name. Both must be right."""
    text = _read("FAILURE_POLICY.md")
    assert f"`{code.name}`" in text
    assert f"| {code.value} | `{code.name}` |" in text, (
        f"{code.name} is not in the exit code table with value {code.value}."
    )


def test_the_taxonomy_scan_would_notice_a_member_nobody_documented() -> None:
    sentinel = "AN_ENTIRELY" + "_INVENTED_SCOPE"
    assert sentinel not in _read("FAILURE_POLICY.md")


# ---------------------------------------------------------------------------
# The protocol describes the design that exists
# ---------------------------------------------------------------------------


def _ascii_primes(text: str) -> str:
    """Fold U+2032 onto the ASCII apostrophe.

    The documents use the specification's prime notation (``B'``, U+2032); the
    code spells the same labels with an ASCII apostrophe, because ruff flags
    U+2032 in source as an ambiguous character. Two typographies for one arm.
    Normalising here is what lets the comparison below be exact rather than
    approximate — the alternative is a test that matches ``B`` and would pass
    for a document that had dropped the placebos entirely.
    """
    return text.replace(chr(0x2032), "'")


@pytest.mark.parametrize("arm_id", list(ArmId), ids=str)
def test_every_arm_appears_in_the_scientific_protocol(arm_id: ArmId) -> None:
    """An arm that runs but is not described is an undeclared condition."""
    protocol = _ascii_primes(_read("SCIENTIFIC_PROTOCOL.md"))
    assert f"| {ARMS[arm_id].label} |" in protocol, (
        f"Arm {ARMS[arm_id].label} is not in the protocol's design table."
    )


def test_the_protocol_names_the_same_confirmatory_contrasts_as_the_pre_registration() -> None:
    """Two documents stating the same five contrasts, in different words, is
    two places for them to diverge. The pre-registration is the one that binds;
    this asserts the protocol has not drifted away from it."""
    prime = chr(0x2032)
    contrasts = ("B vs A", "D vs A", "C vs A", f"B vs B{prime}", f"C vs C{prime}")
    protocol = _read("SCIENTIFIC_PROTOCOL.md")
    for contrast in contrasts:
        assert contrast in protocol, f"{contrast} is missing from the scientific protocol."


def test_the_protocol_states_that_masking_is_partial() -> None:
    """§5.4 asks for partial masking. A document describing only the half that
    works is worse than none, because a reader assumes the other half works
    too."""
    text = _read("SCIENTIFIC_PROTOCOL.md")
    assert "Masking is partial" in text
    assert "Analysis side — absent" in text


def test_the_protocol_states_the_trajectory_confound_and_what_the_estimand_is() -> None:
    """The confound is structural and unavoidable. What is avoidable is
    leaving a reader to assume the estimand is a per-decision effect."""
    text = _read("SCIENTIFIC_PROTOCOL.md")
    assert "trajectory confound" in text.lower()
    assert "memory regime for the whole" in text


# ---------------------------------------------------------------------------
# The decision records are complete and indexed
# ---------------------------------------------------------------------------


def test_there_are_architecture_decision_records() -> None:
    assert _adr_files(), "docs/adr/ contains no records."


@pytest.mark.parametrize("path", _adr_files(), ids=lambda p: p.name)
def test_the_record_carries_every_required_section(path: Path) -> None:
    """The sections are the point. A record with a decision and no rejected
    options is a docstring with extra ceremony — it records what was chosen and
    not what it was chosen over, which is the thing a docstring already fails
    to carry."""
    text = path.read_text(encoding="utf-8")
    missing = [section for section in ADR_SECTIONS if section not in text]
    assert not missing, f"{path.name} is missing: {missing}"


@pytest.mark.parametrize("path", _adr_files(), ids=lambda p: p.name)
def test_the_record_names_what_implements_it(path: Path) -> None:
    """So a reader can check the record against the code rather than against
    the author's memory — which matters here because every record was written
    after the decision it describes."""
    text = path.read_text(encoding="utf-8")
    assert "**Implemented by:**" in text, f"{path.name} names no implementation."


@pytest.mark.parametrize("path", _adr_files(), ids=lambda p: p.name)
def test_the_record_is_listed_in_the_index(path: Path) -> None:
    """An unindexed record is one nobody will read."""
    assert f"]({path.name})" in (_ADR / "README.md").read_text(encoding="utf-8"), (
        f"{path.name} exists but the index does not link to it."
    )


def test_the_index_links_to_nothing_that_is_missing() -> None:
    index = (_ADR / "README.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\((\d{4}-[^)]+)\)", index):
        assert (_ADR / target).is_file(), f"The index links to missing {target}"


def test_the_records_are_numbered_uniquely_and_contiguously() -> None:
    """Gaps are ambiguous: a missing 0007 could be a superseded decision whose
    file was deleted, which is exactly what the index forbids."""
    numbers = sorted(int(path.name[:4]) for path in _adr_files())
    assert numbers == list(range(1, len(numbers) + 1)), f"Non-contiguous ADR numbers: {numbers}"


# ---------------------------------------------------------------------------
# Everything points somewhere
# ---------------------------------------------------------------------------


def _markdown_files() -> list[Path]:
    return sorted([*_DOCS.glob("*.md"), *_ADR.glob("*.md")])


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: str(p.relative_to(_DOCS)))
def test_every_relative_link_resolves(path: Path) -> None:
    """These documents cross-reference heavily and were written together, which
    is precisely when a renamed file breaks six links at once."""
    text = path.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        resolved = (path.parent / target.split("#", 1)[0]).resolve()
        assert resolved.exists(), f"{path.name} links to missing {target}"


def test_the_link_scan_reads_more_than_one_link() -> None:
    """A regex that matched nothing would make the test above pass for every
    document in the repository."""
    text = (_DOCS / "SCIENTIFIC_PROTOCOL.md").read_text(encoding="utf-8")
    relative = [
        target
        for target in re.findall(r"\]\(([^)]+)\)", text)
        if not target.startswith(("http://", "https://", "mailto:", "#"))
    ]
    assert len(relative) > 5, f"Only {len(relative)} relative links found; the scan is not working."


# ---------------------------------------------------------------------------
# The documents agree with each other about what is not done
# ---------------------------------------------------------------------------


def test_the_limitations_repeat_the_warning_that_no_result_is_about_memory_yet() -> None:
    """A reader who arrives at LIMITATIONS.md from a search result must not
    have to have read the README first."""
    text = _read("LIMITATIONS.md")
    assert "says anything about memory or reflection yet" in text
    assert "deterministic fake" in text


def test_the_roadmap_remains_the_only_document_claiming_completeness() -> None:
    """Eight new documents are eight new places for a completeness claim to
    appear. Each one defers instead, and this checks they still do."""
    for name in ("ARCHITECTURE.md", "SCIENTIFIC_PROTOCOL.md", "LIMITATIONS.md"):
        assert "ROADMAP.md" in _read(name), f"docs/{name} does not defer to the roadmap."


def test_the_provider_policy_states_the_hole_the_fingerprint_cannot_close() -> None:
    """A provider serving a different model under a stable identifier defeats
    the run fingerprint entirely, and no code here can detect it. If that
    sentence ever disappears, the pre-registration reads stronger than it is."""
    text = _read("PROVIDER_POLICY.md")
    assert "largest hole in the pre-registration mechanism" in text
