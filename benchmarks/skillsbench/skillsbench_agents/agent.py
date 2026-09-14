"""SkillsBench-owned ACP bridge to rcodex, executed inside a BenchFlow task container."""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import uuid
from pathlib import Path
from typing import Any, cast

from acp import PROTOCOL_VERSION, Agent, Client, RequestError, run_agent
from acp.helpers import start_tool_call, update_agent_message_text, update_tool_call
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
)
from pydantic import BaseModel, ConfigDict, Field

from rcodex.config import RunConfig
from rcodex.models import RunStatus
from rcodex.runner import RecursiveRunner
from rcodex.tools import ToolSpec

# BenchFlow owns the task deadline and sends ACP cancellation when it expires.
# rcodex requires finite deadlines; its maximum avoids an earlier adapter timeout.
CONTROLLER_TIMEOUT_CEILING_SECONDS = 86_400


class ShellInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=32_768)
    timeout_seconds: int = Field(default=60, ge=1, le=120)


async def run_shell(arguments: dict[str, Any], cwd: Path) -> dict[str, Any]:
    """Run as the unprivileged benchmark user; kill descendants on cancellation."""
    args = ShellInput.model_validate(arguments)
    # Never make the model provider credential available to task commands.
    env = {key: value for key, value in os.environ.items() if not key.endswith("API_KEY")}
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-c",
        args.command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    captured = bytearray()
    truncated = False

    async def drain() -> None:
        nonlocal truncated
        assert process.stdout is not None
        while chunk := await process.stdout.read(8192):
            remaining = max(0, 16_000 - len(captured))
            captured.extend(chunk[:remaining])
            truncated = truncated or len(chunk) > remaining

    reader = asyncio.create_task(drain())
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), args.timeout_seconds)
    except TimeoutError:
        timed_out = True
    finally:
        # Also reap commands that backgrounded children before exiting.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        await reader

    return {
        "return_code": process.returncode,
        "timed_out": timed_out,
        "output": captured.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }


class RcodexAgent:
    """Only the task-to-rcodex bridge; the ACP SDK owns the wire protocol."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.cwd = Path.cwd()
        self.active: asyncio.Task[Any] | None = None

    def on_connect(self, conn: Client) -> None:
        self.client = conn

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(),
            agent_info=Implementation(name="rcodex", version="0.0.1"),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        if (
            self.session_id is not None
            or kwargs.get("mcp_servers")
            or kwargs.get("additional_directories")
        ):
            raise RequestError.invalid_params(
                {"details": "Only one local task session is supported"}
            )
        self.cwd = await asyncio.to_thread(Path(cwd).resolve, strict=True)
        self.session_id = uuid.uuid4().hex
        return NewSessionResponse(session_id=self.session_id)

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        if session_id != self.session_id or self.active is not None:
            raise RequestError.invalid_params({"details": "Unknown or busy session"})
        if any(part.type != "text" for part in prompt):
            raise RequestError.invalid_params({"details": "Only text tasks are supported"})
        self.active = asyncio.current_task()
        text = "\n".join(part.text for part in prompt)

        async def shell(arguments: dict[str, Any]) -> dict[str, Any]:
            call_id = uuid.uuid4().hex
            await self.client.session_update(
                session_id,
                start_tool_call(
                    call_id,
                    "benchmark_shell",
                    kind="execute",
                    status="in_progress",
                    raw_input=arguments,
                ),
            )
            try:
                result = await run_shell(arguments, self.cwd)
            except BaseException:
                await self.client.session_update(
                    session_id, update_tool_call(call_id, status="failed")
                )
                raise
            await self.client.session_update(
                session_id,
                update_tool_call(
                    call_id,
                    status="completed",
                    raw_output=result,
                ),
            )
            return result

        try:
            # Persistent logs are already exported by BenchFlow, including on timeout.
            root = Path(tempfile.mkdtemp(prefix="run-", dir="/logs/agent"))
            context = root / "context"
            context.mkdir()
            (context / "instruction.txt").write_text(text, encoding="utf-8")
            config = RunConfig(
                model=os.environ["RCODEX_MODEL"],
                provider_base_url=os.environ["RCODEX_PROVIDER_BASE_URL"],
                reasoning_effort=os.environ.get("RCODEX_BENCH_REASONING_EFFORT", "low"),
                max_depth=int(os.environ.get("RCODEX_BENCH_MAX_DEPTH", "1")),
                max_iterations=int(os.environ.get("RCODEX_BENCH_MAX_ITERATIONS", "10")),
                max_concurrency=1,
                run_timeout_seconds=CONTROLLER_TIMEOUT_CEILING_SECONDS,
                node_timeout_seconds=CONTROLLER_TIMEOUT_CEILING_SECONDS,
                leaf_timeout_seconds=CONTROLLER_TIMEOUT_CEILING_SECONDS,
                tool_timeout_seconds=125,
                user_prologue=(
                    f"Task workspace: {self.cwd}. Use the benchmark_shell controller tool "
                    "to read task inputs and skills and write the requested deliverables. "
                    "Task skills are under /skills. Task writes through this tool are authorized."
                ),
            )
            tool = ToolSpec(
                "benchmark_shell", "Execute a command in the task container.", shell, ShellInput
            )
            result, _ = await RecursiveRunner(custom_tools={"benchmark_shell": tool}).run(
                task=text,
                context=context,
                state_directory=root / "state",
                config=config,
            )
            await self.client.session_update(
                session_id,
                update_agent_message_text(
                    result.model_dump_json(),
                ),
            )
            if result.status != RunStatus.succeeded:
                raise RequestError(-32603, f"rcodex ended with {result.status}: {result.error}")
            return PromptResponse(stop_reason="end_turn")
        except asyncio.CancelledError:
            return PromptResponse(stop_reason="cancelled")
        finally:
            self.active = None

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        if session_id == self.session_id and self.active is not None:
            self.active.cancel()


if __name__ == "__main__":
    if not Path("/.dockerenv").exists() or os.geteuid() == 0:
        raise RuntimeError("SkillsBench agent must run as a non-root user inside Docker")
    # Isolate user settings, retaining standard Codex instructions and tool configuration.
    os.environ["CODEX_HOME"] = tempfile.mkdtemp(prefix="rcodex-codex-home-")
    # Only the single-session capabilities advertised above are implemented.
    asyncio.run(run_agent(cast(Agent, RcodexAgent())))
