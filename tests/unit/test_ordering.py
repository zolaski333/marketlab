"""Tests for arm execution ordering (§13.4, §30.3)."""

from __future__ import annotations

from collections import Counter

import pytest

from marketlab.core.failures import ConfigurationError
from marketlab.experiments.ordering import OrderPolicy, execution_order

UNITS = ("A", "B", "C", "D", "B_PRIME", "C_PRIME")
SEED = "test-seed"


def _order(policy: OrderPolicy, cycle_index: int, seed: str = SEED) -> tuple[str, ...]:
    return execution_order(UNITS, policy=policy, cycle_index=cycle_index, seed=seed)


@pytest.mark.parametrize("policy", list(OrderPolicy))
def test_every_policy_is_a_permutation_losing_nothing(policy: OrderPolicy) -> None:
    for cycle_index in range(20):
        assert sorted(_order(policy, cycle_index)) == sorted(UNITS)


@pytest.mark.parametrize("policy", list(OrderPolicy))
def test_every_policy_is_reproducible(policy: OrderPolicy) -> None:
    # §12.5: a replay must reconstruct the order the original run used.
    assert _order(policy, 7) == _order(policy, 7)


def test_latin_square_gives_every_unit_every_position_exactly_once() -> None:
    """Exact balance, not balance in expectation — the reason this is the
    default policy at the cycle counts a study of this kind reaches."""
    positions: dict[str, list[int]] = {unit: [] for unit in UNITS}
    for cycle_index in range(len(UNITS)):
        for position, unit in enumerate(_order(OrderPolicy.LATIN_SQUARE, cycle_index)):
            positions[unit].append(position)
    for unit, seen in positions.items():
        assert sorted(seen) == list(range(len(UNITS))), unit


def test_latin_square_changes_the_order_from_one_cycle_to_the_next() -> None:
    assert _order(OrderPolicy.LATIN_SQUARE, 0) != _order(OrderPolicy.LATIN_SQUARE, 1)


def test_latin_square_cycle_zero_is_the_declaration_order() -> None:
    assert _order(OrderPolicy.LATIN_SQUARE, 0) == UNITS


def test_randomized_depends_on_the_seed() -> None:
    assert _order(OrderPolicy.RANDOMIZED, 3, "seed-one") != _order(
        OrderPolicy.RANDOMIZED, 3, "seed-two"
    )


def test_randomized_depends_on_the_cycle_index() -> None:
    orders = {_order(OrderPolicy.RANDOMIZED, index) for index in range(10)}
    assert len(orders) > 1


def test_randomized_actually_shuffles_rather_than_returning_the_input() -> None:
    # A shuffle that silently no-ops would still pass the permutation test.
    orders = [_order(OrderPolicy.RANDOMIZED, index) for index in range(20)]
    assert any(order != UNITS for order in orders)


def test_randomized_is_reasonably_uniform_over_many_cycles() -> None:
    """Not a formal uniformity test — a smoke check that the rejection
    sampling is not tilting every draw towards one corner. Every unit should
    reach every position at least once across a few hundred cycles."""
    seen: Counter[tuple[str, int]] = Counter()
    for cycle_index in range(600):
        for position, unit in enumerate(_order(OrderPolicy.RANDOMIZED, cycle_index)):
            seen[(unit, position)] += 1
    for unit in UNITS:
        for position in range(len(UNITS)):
            assert seen[(unit, position)] > 0, (unit, position)


@pytest.mark.parametrize("policy", list(OrderPolicy))
def test_a_single_unit_or_none_is_returned_unchanged(policy: OrderPolicy) -> None:
    assert execution_order((), policy=policy, cycle_index=3, seed=SEED) == ()
    assert execution_order(("A",), policy=policy, cycle_index=3, seed=SEED) == ("A",)


@pytest.mark.parametrize("policy", list(OrderPolicy))
def test_a_negative_cycle_index_is_refused(policy: OrderPolicy) -> None:
    with pytest.raises(ConfigurationError):
        execution_order(UNITS, policy=policy, cycle_index=-1, seed=SEED)
