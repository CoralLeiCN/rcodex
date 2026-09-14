"""Offline contract checks for the opt-in container benchmark integration."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp import RequestError, text_block
from skillsbench_agents.agent import RcodexAgent, run_shell


async def test_shell_writes_deliverable_and_reports_exit(tmp_path: Path) -> None:
    result = await run_shell(
        {"command": "printf result > answer.txt; printf diagnostic; exit 7"}, tmp_path
    )
    assert (tmp_path / "answer.txt").read_text() == "result"
    assert result == {
        "return_code": 7,
        "output": "diagnostic",
        "timed_out": False,
        "truncated": False,
    }


async def test_shell_bounds_output_and_removes_provider_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RCODEX_PROVIDER_API_KEY", "private-test-value")
    result = await run_shell(
        {
            "command": 'printf "%s" "$RCODEX_PROVIDER_API_KEY"; '
            "head -c 17000 /dev/zero | tr '\\0' x"
        },
        tmp_path,
    )
    assert result["output"] == "x" * 16_000
    assert result["truncated"]


async def test_shell_cancellation_kills_descendants(tmp_path: Path) -> None:
    running = asyncio.create_task(
        run_shell({"command": "(sleep 0.5; touch leaked) & touch started; wait"}, tmp_path)
    )
    for _ in range(100):
        if (tmp_path / "started").exists():
            break
        await asyncio.sleep(0.01)
    assert (tmp_path / "started").exists()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await asyncio.sleep(0.6)
    assert not (tmp_path / "leaked").exists()


async def test_acp_rejects_unsupported_sessions(tmp_path: Path) -> None:
    agent = RcodexAgent()
    with pytest.raises(RequestError):
        await agent.new_session(str(tmp_path), mcp_servers=[{}])
    session = await agent.new_session(str(tmp_path))
    assert session.session_id == agent.session_id
    with pytest.raises(RequestError):
        await agent.prompt("unknown", [text_block("Task")])


async def test_acp_sdk_handshake() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import asyncio; from acp import run_agent; "
        "from skillsbench_agents.agent import RcodexAgent; asyncio.run(run_agent(RcodexAgent()))",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
    try:
        process.stdin.write((json.dumps(request) + "\n").encode())
        await process.stdin.drain()
        response = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=5))
        assert response["result"]["agentInfo"]["name"] == "rcodex"
        assert response["result"]["protocolVersion"] == 1
    finally:
        process.stdin.close()
        await asyncio.wait_for(process.wait(), timeout=5)
    assert process.returncode == 0


@pytest.mark.parametrize("cancelled", [False, True])
async def test_prompt_uses_normal_rcodex_config_and_honors_benchflow_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    from skillsbench_agents import agent as module

    from rcodex.models import RunStatus

    updates: list[Any] = []
    observed: dict[str, Any] = {}
    started = asyncio.Event()

    async def update(*args: Any) -> None:
        updates.append(args[1])

    async def run(**kwargs: Any) -> tuple[Any, None]:
        observed.update(kwargs)
        started.set()
        if cancelled:
            await asyncio.Future[None]()
        return SimpleNamespace(
            status=RunStatus.succeeded, model_dump_json=lambda: '{"status":"succeeded"}'
        ), None

    monkeypatch.setenv("RCODEX_MODEL", "local-model")
    monkeypatch.setenv("RCODEX_PROVIDER_BASE_URL", "http://localhost/v1")
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    monkeypatch.setattr(module, "RecursiveRunner", lambda **kwargs: SimpleNamespace(run=run))
    agent = RcodexAgent()
    agent.on_connect(SimpleNamespace(session_update=update))
    session = await agent.new_session(str(tmp_path))
    running = asyncio.create_task(
        agent.prompt(session.session_id, [text_block("Create the deliverables")])
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        if cancelled:
            await agent.cancel(session.session_id)
        response = await asyncio.wait_for(running, timeout=1)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert response.stop_reason == ("cancelled" if cancelled else "end_turn")
    assert observed["task"] == "Create the deliverables"
    assert observed["config"].custom_system_prompt is None
    assert observed["config"].model == "local-model"
    config = observed["config"]
    # Neither the standard 900s task nor a longer override should be preempted.
    assert (
        min(config.run_timeout_seconds, config.node_timeout_seconds, config.leaf_timeout_seconds)
        > 1200
    )
    assert agent.active is None
    assert len(updates) == (0 if cancelled else 1)
