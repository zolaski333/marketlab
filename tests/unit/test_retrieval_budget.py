"""Tests for the per-decision tool-call budget (§10.6)."""

from __future__ import annotations

import pytest

from marketlab.core.failures import BudgetError, ConfigurationError
from marketlab.retrieval.budget import ToolBudget


def test_charge_under_budget_increments_both_counters() -> None:
    budget = ToolBudget(max_calls=5, max_evidence_chars=100)
    budget.charge(chars=30)
    assert budget.calls_used == 1
    assert budget.evidence_chars_used == 30
    assert budget.calls_remaining == 4


def test_charge_exceeding_call_count_raises() -> None:
    budget = ToolBudget(max_calls=1, max_evidence_chars=1000)
    budget.charge(chars=10)
    with pytest.raises(BudgetError, match="Tool call budget exhausted"):
        budget.charge(chars=10)


def test_charge_exceeding_char_budget_raises() -> None:
    budget = ToolBudget(max_calls=10, max_evidence_chars=50)
    with pytest.raises(BudgetError, match="Evidence character budget exhausted"):
        budget.charge(chars=51)


def test_a_failed_charge_does_not_mutate_the_budget() -> None:
    budget = ToolBudget(max_calls=1, max_evidence_chars=1000)
    budget.charge(chars=10)
    with pytest.raises(BudgetError):
        budget.charge(chars=10)
    assert budget.calls_used == 1
    assert budget.evidence_chars_used == 10


def test_charge_exactly_at_the_char_limit_succeeds() -> None:
    budget = ToolBudget(max_calls=10, max_evidence_chars=50)
    budget.charge(chars=50)
    assert budget.evidence_chars_used == 50


def test_non_positive_max_calls_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="max_calls"):
        ToolBudget(max_calls=0)


def test_non_positive_max_evidence_chars_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="max_evidence_chars"):
        ToolBudget(max_evidence_chars=-1)
