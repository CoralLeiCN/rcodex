from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from rcodex.config import RunConfig
from rcodex.json_values import MAX_JSON_NESTING
from rcodex.models import (
    CallMode,
    DelegateRequest,
    FinalPayload,
    IterationRecord,
    ReplExecutionRecord,
    RunUsage,
    ToolRequest,
)
from rcodex.tools import ToolError, ToolRegistry


def test_internal_repl_requests_remain_strict_and_typed() -> None:
    request = DelegateRequest.model_validate(
        {
            "kind": "delegate",
            "call_id": "child",
            "mode": "recursive",
            "task": "solve child",
            "model": None,
            "reasoning_effort": None,
        },
        strict=True,
    )

    assert request.mode == CallMode.recursive
    with pytest.raises(ValidationError):
        DelegateRequest.model_validate(
            {
                "kind": "delegate",
                "call_id": "child",
                "mode": "recursive",
                "task": "solve child",
                "unexpected": True,
            },
            strict=True,
        )


def test_final_payload_remains_strict() -> None:
    with pytest.raises(ValidationError):
        FinalPayload.model_validate(
            {"answer": "missing version", "evidence": [], "uncertainties": []},
            strict=True,
        )


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_tool_request_arguments_reject_non_finite_numbers(number: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        ToolRequest(
            kind="tool",
            call_id="invalid_number",
            name="lookup",
            arguments={"number": number},
        )


def test_tool_request_arguments_require_string_keys_and_bounded_nesting() -> None:
    with pytest.raises(ValidationError, match="keys must be strings"):
        ToolRequest(
            kind="tool",
            call_id="invalid_key",
            name="lookup",
            arguments={"nested": {1: "value"}},
        )

    nested: object = "leaf"
    for _ in range(MAX_JSON_NESTING):
        nested = [nested]
    with pytest.raises(ValidationError, match="nesting"):
        ToolRequest(
            kind="tool",
            call_id="too_deep",
            name="lookup",
            arguments={"nested": nested},
        )


def test_run_config_validates_recursive_limits_and_model_overrides() -> None:
    config = RunConfig(
        model="root",
        sub_model="child",
        allowed_models=("root", "child", "special"),
        max_depth=4,
        max_concurrency=1,
    )

    assert config.resolved_sub_model == "child"
    assert config.resolve_requested_model("special") == "special"
    assert config.limits().max_depth == 4

    with pytest.raises(ValueError):
        RunConfig(max_depth=-1)
    with pytest.raises(ValueError, match="allowed_models"):
        RunConfig(model="root").resolve_requested_model("other")
    with pytest.raises(ValueError):
        RunConfig(max_manifest_entries=1_000_000)
    with pytest.raises(ValueError):
        RunConfig(max_batch_size=4097)
    with pytest.raises(ValueError):
        RunConfig(max_query_context_bytes=16_777_217)
    with pytest.raises(ValueError):
        RunConfig(max_total_child_context_bytes=0)


def test_run_config_child_effort_falls_back_only_when_unset() -> None:
    assert RunConfig(reasoning_effort="medium").resolved_sub_reasoning_effort == "medium"
    assert (
        RunConfig(
            reasoning_effort="medium", sub_reasoning_effort="high"
        ).resolved_sub_reasoning_effort
        == "high"
    )
    with pytest.raises(ValueError, match="sub_reasoning_effort must not be blank"):
        RunConfig(reasoning_effort="medium", sub_reasoning_effort="")


def test_run_config_validates_openai_compatible_provider() -> None:
    config = RunConfig(
        model="Qwen/Qwen3-Coder",
        provider_base_url="http://127.0.0.1:8000/v1",
    )

    assert config.provider is not None
    assert config.provider.base_url == "http://127.0.0.1:8000/v1"

    with pytest.raises(ValueError, match="model is required"):
        RunConfig(provider_base_url="http://127.0.0.1:8000/v1")
    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
        RunConfig(model="local", provider_base_url="localhost:8000/v1")


def test_repl_iteration_round_trips_with_typed_final_payload() -> None:
    now = datetime.now(UTC)
    original = IterationRecord(
        node_id="node_000001",
        iteration=1,
        phase="finalization",
        started_at=now,
        completed_at=now,
        duration_ms=0,
        prompt_sha256="0" * 64,
        executions=[
            ReplExecutionRecord(
                code='submit_answer("done")',
                stdout="",
                stderr="",
                variable_types={},
                final_payload=FinalPayload(
                    schema_version="1.0",
                    answer="done",
                    evidence=[],
                    uncertainties=[],
                ),
                duration_ms=1,
            )
        ],
        usage=RunUsage(available=False),
    )

    restored = IterationRecord.model_validate_json(original.model_dump_json(), strict=True)

    assert type(restored.executions[0].final_payload) is FinalPayload
    assert restored.model_dump(mode="json") == original.model_dump(mode="json")


@pytest.mark.asyncio
async def test_tool_results_reject_non_string_json_object_keys() -> None:
    registry = ToolRegistry.from_custom_tools(
        {"mixed_keys": lambda _arguments: {1: "integer", "1": "string"}}
    )

    with pytest.raises(ToolError, match="keys must be strings"):
        await registry.invoke("mixed_keys", {}, timeout_seconds=1)


@pytest.mark.parametrize("value", [float("nan"), ("tuple",), {"nested": {1: "value"}}])
def test_custom_tool_constants_require_strict_json(value: object) -> None:
    with pytest.raises(ValueError, match="strict JSON value"):
        ToolRegistry.from_custom_tools({"invalid": value})


@pytest.mark.asyncio
async def test_custom_tool_constants_are_copied_on_registration_and_invoke() -> None:
    original = {"items": ["first"]}
    registry = ToolRegistry.from_custom_tools({"constant": original})
    original["items"].append("changed outside")

    first = await registry.invoke("constant", {}, timeout_seconds=1)
    assert first == {"items": ["first"]}
    assert isinstance(first, dict)
    first["items"].append("changed result")

    assert await registry.invoke("constant", {}, timeout_seconds=1) == {"items": ["first"]}


@pytest.mark.asyncio
async def test_tool_results_reject_non_finite_numbers_and_non_json_containers() -> None:
    nan_registry = ToolRegistry.from_custom_tools({"invalid": lambda _arguments: float("nan")})
    tuple_registry = ToolRegistry.from_custom_tools({"invalid": lambda _arguments: ("tuple",)})

    with pytest.raises(ToolError, match="finite"):
        await nan_registry.invoke("invalid", {}, timeout_seconds=1)
    with pytest.raises(ToolError, match="strict JSON types"):
        await tuple_registry.invoke("invalid", {}, timeout_seconds=1)
