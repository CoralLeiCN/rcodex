"""Controller-mediated custom tools exposed as recursive-REPL callables."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from rcodex.json_values import StrictJsonError, normalize_json_value

RESERVED_TOOL_NAMES = frozenset(
    {
        "answer",
        "context",
        "history",
        "llm_query",
        "llm_query_batched",
        "rlm_query",
        "rlm_query_batched",
        "SHOW_VARS",
        "submit_answer",
        "delegate",
        "delegate_batch",
    }
)
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
ToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class ToolError(RuntimeError):
    """A safe custom-tool failure."""


class ToolInputError(ToolError):
    """The model-authored tool arguments did not validate."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    input_model: type[BaseModel] | None = None

    def definition(self) -> dict[str, Any]:
        schema: dict[str, Any] = (
            self.input_model.model_json_schema()
            if self.input_model is not None
            else {"type": "object", "additionalProperties": True}
        )
        return {"name": self.name, "description": self.description, "input_schema": schema}


class ToolRegistry:
    """Validated trusted host tools invoked through the REPL controller boundary."""

    def __init__(self, tools: Mapping[str, ToolSpec] | None = None) -> None:
        self._tools = dict(tools or {})
        for name, spec in self._tools.items():
            self._validate_name(name)
            if name != spec.name:
                raise ValueError("tool mapping key must equal ToolSpec.name")

    @classmethod
    def from_custom_tools(cls, values: Mapping[str, Any] | None) -> ToolRegistry:
        registry = cls()
        for name, raw in (values or {}).items():
            if isinstance(raw, ToolSpec):
                if raw.name != name:
                    raise ValueError("tool mapping key must equal ToolSpec.name")
                registry.register(
                    name,
                    raw.handler,
                    description=raw.description,
                    input_model=raw.input_model,
                )
                continue
            description: str | None = None
            value = raw
            if isinstance(raw, Mapping) and set(raw).issubset({"tool", "description"}):
                if "tool" not in raw:
                    raise ValueError(f"custom tool {name!r} is missing 'tool'")
                value = raw["tool"]
                raw_description = raw.get("description")
                if raw_description is not None and not isinstance(raw_description, str):
                    raise ValueError(f"custom tool {name!r} description must be a string")
                description = raw_description
            if callable(value):
                handler = value
                inferred = inspect.getdoc(value) or f"Call the trusted host tool {name}."
            else:
                try:
                    immutable = normalize_json_value(value)
                except StrictJsonError as exc:
                    raise ValueError(f"custom value {name!r} must be a strict JSON value") from exc

                def constant(_arguments: dict[str, Any], *, item: Any = immutable) -> Any:
                    return normalize_json_value(item)

                handler = constant
                inferred = f"Read the immutable custom value {name}."
            registry.register(name, handler, description=description or inferred)
        return registry

    @staticmethod
    def _validate_name(name: str) -> None:
        if TOOL_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"invalid custom tool name: {name!r}")
        if name in RESERVED_TOOL_NAMES:
            raise ValueError(f"custom tool name is reserved: {name}")

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        description: str,
        input_model: type[BaseModel] | None = None,
    ) -> None:
        self._validate_name(name)
        if name in self._tools:
            raise ValueError(f"custom tool is already registered: {name}")
        if len(self._tools) >= 64:
            raise ValueError("at most 64 custom tools may be registered")
        normalized = description.strip()
        if not normalized or len(normalized) > 2000:
            raise ValueError("tool description must contain 1..2000 characters")
        try:
            normalized.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("tool description must be valid UTF-8") from exc
        spec = ToolSpec(name, normalized, handler, input_model)
        encoded = json.dumps(
            spec.definition(),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > 32 * 1024:
            raise ValueError("tool definition exceeds 32768 bytes")
        self._tools[name] = spec

    def definitions(self) -> list[dict[str, Any]]:
        return [self._tools[name].definition() for name in sorted(self._tools)]

    def definitions_sha256(self) -> str:
        encoded = json.dumps(
            self.definitions(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def __bool__(self) -> bool:
        return bool(self._tools)

    async def invoke(self, name: str, arguments: dict[str, Any], *, timeout_seconds: float) -> Any:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(f"unknown controller tool: {name}")
        normalized = arguments
        if spec.input_model is not None:
            try:
                validated = spec.input_model.model_validate(arguments, strict=True)
            except ValidationError as exc:
                raise ToolInputError("tool arguments failed validation") from exc
            normalized = validated.model_dump(mode="python")

        async def call() -> Any:
            result: Any
            if inspect.iscoroutinefunction(spec.handler):
                result = spec.handler(normalized)
            else:

                def invoke_sync() -> Any | Awaitable[Any]:
                    return spec.handler(normalized)

                result = await asyncio.to_thread(invoke_sync)
            if inspect.isawaitable(result):
                result = await result
            try:
                return normalize_json_value(result)
            except StrictJsonError as exc:
                raise ToolError(f"tool result is not strict JSON: {exc}") from exc

        try:
            return await asyncio.wait_for(call(), timeout=timeout_seconds)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ToolError("tool call exceeded its deadline") from exc
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"tool handler failed: {type(exc).__name__}") from exc
