from __future__ import annotations

from typing import Any

import pytest

from rcodex.models import CallMode, CallResult
from rcodex.repl import ReplSession


async def _queries(
    mode: CallMode, prompts: list[str], model: str | None, contexts: list[str | None]
) -> tuple[list[str], list[CallResult]]:
    del mode, model, contexts
    return [prompt.upper() for prompt in prompts], []


async def _tools(name: str, arguments: dict[str, Any]) -> tuple[Any, CallResult]:
    del name, arguments
    raise AssertionError("no tool call expected")


async def _context(arguments: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("no context call expected")


@pytest.mark.asyncio
async def test_repl_is_persistent_and_queries_resume_python_execution() -> None:
    session = await ReplSession.start(
        context_handler=_context,
        tool_names=[],
        max_output_bytes=4096,
        max_message_bytes=64 * 1024,
        max_query_context_bytes=8 * 1024**2,
        memory_bytes=512 * 1024**2,
        cpu_seconds=5,
        timeout_seconds=1,
    )
    try:
        first = await session.execute(
            "items = llm_query_batched(['one', 'two'])\nprint(items)",
            timeout_seconds=1,
            query_handler=_queries,
            tool_handler=_tools,
        )
        second = await session.execute(
            "items.append('THREE')\nsubmit_answer(' '.join(items))",
            timeout_seconds=1,
            query_handler=_queries,
            tool_handler=_tools,
        )
    finally:
        await session.close(1)

    assert first.stdout == "['ONE', 'TWO']\n"
    assert second.final_payload is not None
    assert second.final_payload["answer"] == "ONE TWO THREE"
    assert second.variable_types == {"items": "list"}


@pytest.mark.asyncio
async def test_repl_rejects_imports_and_file_access() -> None:
    session = await ReplSession.start(
        context_handler=_context,
        tool_names=[],
        max_output_bytes=4096,
        max_message_bytes=64 * 1024,
        max_query_context_bytes=8 * 1024**2,
        memory_bytes=512 * 1024**2,
        cpu_seconds=5,
        timeout_seconds=1,
    )
    try:
        imported = await session.execute(
            "import os",
            timeout_seconds=1,
            query_handler=_queries,
            tool_handler=_tools,
        )
        opened = await session.execute(
            "open('forbidden.txt', 'w')",
            timeout_seconds=1,
            query_handler=_queries,
            tool_handler=_tools,
        )
    finally:
        await session.close(1)

    assert "Import is unavailable" in imported.stderr
    assert "NameError" in opened.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression",
    [
        'rlm_query("task", context=123)',
        'rlm_query("task", context={"text": "value"})',
        'rlm_query_batched(["one", "two"], contexts=["one"])',
        'rlm_query_batched(["task"], contexts="text")',
        'rlm_query("task", context="é" * 5)',
        'rlm_query_batched(["one", "two"], contexts=["é" * 3, "ö" * 2])',
        'rlm_query("task", context=chr(0))',
        'rlm_query("task", context=chr(0xD800))',
    ],
)
async def test_bad_context_arguments_are_catchable_before_query_dispatch(expression: str) -> None:
    async def no_query(
        mode: CallMode, prompts: list[str], model: str | None, contexts: list[str | None]
    ) -> tuple[list[str], list[CallResult]]:
        raise AssertionError("invalid contexts must never be dispatched")

    session = await ReplSession.start(
        context_handler=_context,
        tool_names=[],
        max_output_bytes=4096,
        max_message_bytes=64 * 1024,
        max_query_context_bytes=8,
        memory_bytes=512 * 1024**2,
        cpu_seconds=5,
        timeout_seconds=1,
    )
    try:
        result = await session.execute(
            f"try:\n    {expression}\nexcept (TypeError, ValueError):\n    submit_answer('caught')",
            timeout_seconds=1,
            query_handler=no_query,
            tool_handler=_tools,
        )
    finally:
        await session.close(1)
    assert result.stderr == ""
    assert result.final_payload is not None and result.final_payload["answer"] == "caught"


@pytest.mark.asyncio
async def test_escaped_context_transport_exceeds_ordinary_message_limit() -> None:
    async def query(
        mode: CallMode, prompts: list[str], model: str | None, contexts: list[str | None]
    ) -> tuple[list[str], list[CallResult]]:
        assert mode == CallMode.recursive
        assert prompts == ["task"]
        assert contexts == ["\x01" * 100_000]
        return ["complete"], []

    session = await ReplSession.start(
        context_handler=_context,
        tool_names=[],
        max_output_bytes=4096,
        max_message_bytes=1024,
        max_query_context_bytes=100_000,
        memory_bytes=512 * 1024**2,
        cpu_seconds=5,
        timeout_seconds=1,
    )
    try:
        result = await session.execute(
            'submit_answer(rlm_query("task", context=chr(1) * 100000))',
            timeout_seconds=1,
            query_handler=query,
            tool_handler=_tools,
        )
    finally:
        await session.close(1)
    assert result.final_payload is not None and result.final_payload["answer"] == "complete"
    assert result.stdout == result.stderr == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contexts",
    [["é" * 5], ["\x00"], ["\ud800"], [123], [], "not a list"],
)
async def test_controller_revalidates_context_rpc(
    contexts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_query(
        mode: CallMode, prompts: list[str], model: str | None, contexts: list[str | None]
    ) -> tuple[list[str], list[CallResult]]:
        raise AssertionError("controller must reject the invalid RPC")

    session = await ReplSession.start(
        context_handler=_context,
        tool_names=[],
        max_output_bytes=4096,
        max_message_bytes=64 * 1024,
        max_query_context_bytes=8,
        memory_bytes=512 * 1024**2,
        cpu_seconds=5,
        timeout_seconds=1,
    )
    read = session._read

    async def corrupt_rpc() -> dict[str, Any]:
        message = await read()
        if message.get("type") == "query":
            message["contexts"] = contexts
        return message

    monkeypatch.setattr(session, "_read", corrupt_rpc)
    try:
        result = await session.execute(
            'submit_answer(rlm_query("task"))',
            timeout_seconds=1,
            query_handler=no_query,
            tool_handler=_tools,
        )
    finally:
        await session.close(1)
    assert result.final_payload is not None
    assert result.final_payload["answer"].startswith("Error: ValueError:")
