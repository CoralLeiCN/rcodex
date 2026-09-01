"""Shared validation for values crossing strict JSON boundaries."""

from __future__ import annotations

import math
from typing import TypeAlias

MAX_JSON_NESTING = 64

JsonScalar: TypeAlias = bool | int | float | str | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class StrictJsonError(ValueError):
    """A value cannot be represented by the bounded strict-JSON contract."""


def _validate_text(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StrictJsonError("JSON strings must be valid UTF-8") from exc


def normalize_json_value(
    value: object,
    *,
    max_nesting: int = MAX_JSON_NESTING,
) -> JsonValue:
    """Validate and copy a finite, acyclic, bounded strict-JSON value."""

    if max_nesting < 1:
        raise ValueError("max_nesting must be positive")

    active_containers: set[int] = set()

    def visit(item: object, *, nesting: int) -> JsonValue:
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            _validate_text(item)
            return item
        if isinstance(item, int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise StrictJsonError("JSON numbers must be finite")
            return item
        if not isinstance(item, (list, dict)):
            raise StrictJsonError("value must contain only strict JSON types")
        if nesting >= max_nesting:
            raise StrictJsonError(f"JSON nesting must not exceed {max_nesting}")

        identity = id(item)
        if identity in active_containers:
            raise StrictJsonError("JSON values must not contain cycles")
        active_containers.add(identity)
        try:
            if isinstance(item, list):
                return [visit(child, nesting=nesting + 1) for child in item]

            normalized: dict[str, JsonValue] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise StrictJsonError("JSON object keys must be strings")
                _validate_text(key)
                normalized[key] = visit(child, nesting=nesting + 1)
            return normalized
        finally:
            active_containers.remove(identity)

    return visit(value, nesting=0)


def normalize_json_object(
    value: object,
    *,
    max_nesting: int = MAX_JSON_NESTING,
) -> dict[str, JsonValue]:
    """Validate and copy a strict JSON object."""

    normalized = normalize_json_value(value, max_nesting=max_nesting)
    if not isinstance(normalized, dict):
        raise StrictJsonError("value must be a JSON object")
    return normalized
