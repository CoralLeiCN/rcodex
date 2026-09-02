from __future__ import annotations

from typing import Any

import pytest

from rcodex.models import CallMode, CallResult
from rcodex.repl import ReplSession


async def _queries(
    mode: CallMode, prompts: list[str], model: str | None
) -> tuple[list[str], list[CallResult]]:
    del mode, model
    return [prompt.upper() for prompt in prompts], []


async def _tools(name: str, arguments: dict[str, Any]) -> tuple[Any, CallResult]:
    del name, arguments
    raise AssertionError("no tool call expected")


@pytest.mark.asyncio
async def test_repl_is_persistent_and_queries_resume_python_execution() -> None:
    session = await ReplSession.start(
        tool_names=[],
        max_output_bytes=4096,
        max_message_bytes=64 * 1024,
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
        tool_names=[],
        max_output_bytes=4096,
        max_message_bytes=64 * 1024,
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
