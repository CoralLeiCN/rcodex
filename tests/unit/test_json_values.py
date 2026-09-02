from __future__ import annotations

import math

import pytest

from rcodex.context import canonical_json_bytes
from rcodex.json_values import StrictJsonError, normalize_json_value


def _nested_lists(count: int) -> object:
    value: object = "leaf"
    for _ in range(count):
        value = [value]
    return value


def test_normalize_json_value_copies_bounded_strict_json() -> None:
    original = {"items": [None, True, 7, 1.5, "text", {"nested": "value"}]}

    normalized = normalize_json_value(original, max_nesting=3)

    assert normalized == original
    assert normalized is not original
    assert isinstance(normalized, dict)
    assert normalized["items"] is not original["items"]


@pytest.mark.parametrize("number", [math.nan, math.inf, -math.inf])
def test_normalize_json_value_rejects_non_finite_numbers(number: float) -> None:
    with pytest.raises(StrictJsonError, match="finite"):
        normalize_json_value({"number": number})


def test_normalize_json_value_rejects_non_string_keys_and_non_json_containers() -> None:
    with pytest.raises(StrictJsonError, match="keys must be strings"):
        normalize_json_value({"nested": {1: "not a JSON key"}})
    with pytest.raises(StrictJsonError, match="strict JSON types"):
        normalize_json_value(("tuple",))


def test_normalize_json_value_rejects_excessive_nesting_and_cycles() -> None:
    assert normalize_json_value(_nested_lists(2), max_nesting=2) == [["leaf"]]
    with pytest.raises(StrictJsonError, match="nesting"):
        normalize_json_value(_nested_lists(3), max_nesting=2)

    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(StrictJsonError, match="cycles"):
        normalize_json_value(cyclic)


def test_canonical_json_bytes_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json_bytes({"number": math.nan})
