"""Property tests for canonical JSON (§9.4, §24.1).

Every scientific hash is taken over this encoding. If two structurally equal
values could ever encode differently, snapshot hashes would diverge between
arms that were served identical data, and the audit chain would report tampering
where none occurred.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from marketlab.core.canonical import canonical_hash, canonical_json
from marketlab.core.money import Money

# Arbitrary nested JSON-shaped values.
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**12), max_value=10**12)
    | st.text(max_size=40),
    lambda children: (
        st.lists(children, max_size=5)
        | st.dictionaries(st.text(min_size=1, max_size=12), children, max_size=5)
    ),
    max_leaves=25,
)


@given(json_values)
def test_encoding_is_stable_across_calls(value: object) -> None:
    assert canonical_json(value) == canonical_json(value)


@given(st.dictionaries(st.text(min_size=1, max_size=10), st.integers(), max_size=8))
def test_key_insertion_order_does_not_change_the_encoding(data: dict[str, int]) -> None:
    """Dict ordering is an implementation detail, not part of the value."""
    reversed_insertion = dict(reversed(list(data.items())))
    assert canonical_json(data) == canonical_json(reversed_insertion)
    assert canonical_hash(data) == canonical_hash(reversed_insertion)


def test_nested_keys_are_sorted_at_every_depth() -> None:
    encoded = canonical_json({"b": {"z": 1, "a": 2}, "a": [{"y": 1, "x": 2}]})
    assert encoded == '{"a":[{"x":2,"y":1}],"b":{"a":2,"z":1}}'


def test_encoding_has_no_insignificant_whitespace() -> None:
    assert canonical_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


@given(
    st.decimals(
        min_value=Decimal("-1e6"),
        max_value=Decimal("1e6"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    )
)
def test_decimals_encode_as_plain_strings(value: Decimal) -> None:
    """A Decimal routed through binary float is no longer the same number."""
    encoded = canonical_json({"amount": value})
    assert encoded.startswith('{"amount":"')
    assert "E" not in encoded.upper().replace("AMOUNT", "")


def test_money_encodes_with_its_currency() -> None:
    encoded = canonical_json(Money(Decimal("1234.5"), "USD"))
    assert encoded == '{"amount":"1234.50","currency":"USD"}'


def test_datetimes_encode_as_canonical_instants() -> None:
    encoded = canonical_json(datetime(2026, 8, 1, 16, 0, tzinfo=UTC))
    assert encoded == '"2026-08-01T16:00:00.000000Z"'


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="Naive datetime rejected"):
        canonical_json(datetime(2026, 8, 1, 16, 0))


def test_non_ascii_is_preserved_not_escaped() -> None:
    assert canonical_json({"t": "réunion"}) == '{"t":"réunion"}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_refused(value: float) -> None:
    """Standard json emits NaN/Infinity, which are not valid JSON."""
    with pytest.raises(ValueError, match="Non-finite float"):
        canonical_json({"x": value})


def test_sets_are_refused_rather_than_silently_ordered() -> None:
    """Set iteration order is not part of the value the caller intended."""
    with pytest.raises(TypeError, match="Sets are rejected"):
        canonical_json({"x": {1, 2, 3}})


def test_non_string_keys_are_refused() -> None:
    with pytest.raises(TypeError, match="requires string keys"):
        canonical_json({1: "a"})


def test_unknown_types_are_refused_rather_than_repr_ed() -> None:
    class Opaque:
        pass

    with pytest.raises(TypeError, match="No canonical representation"):
        canonical_json(Opaque())


def test_booleans_are_not_conflated_with_integers() -> None:
    """bool subclasses int; encoding True as 1 would collide distinct values."""
    assert canonical_json(True) == "true"
    assert canonical_json(1) == "1"
    assert canonical_hash(True) != canonical_hash(1)
