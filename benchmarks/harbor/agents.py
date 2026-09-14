"""Harbor external agent using rcodex's recursive runtime and container tools."""

from __future__ import annotations

import asyncio
import json
import os
import time
from functools import partial
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from harbor.agents.base import BaseAgent
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.trial.errors import AgentTimeoutError
from pydantic import BaseModel, ConfigDict, Field
from rcodex._version import __version__
from rcodex.codex_adapter import SdkCodexAdapter
from rcodex.codex_adapter.config import codex_runtime_path
from rcodex.config import RunConfig
from rcodex.models import RunResult, RunStatus, RunStrategy
from rcodex.runner import RecursiveRunner
from rcodex.storage import ensure_private_directory
from rcodex.tools import ToolSpec

MAX_OUTPUT_CHARS = 12_000


def _local_model_settings(model_name: str | None, provider_base_url: str | None) -> tuple[str, str]:
    load_dotenv(Path.cwd() / ".env", override=False)
    model_name = model_name or os.environ.get("RCODEX_MODEL")
    provider_base_url = provider_base_url or os.environ.get("RCODEX_PROVIDER_BASE_URL")
    if not model_name or not provider_base_url:
        raise ValueError("Harbor agents require a model and a Responses provider base URL")
    return model_name, provider_base_url


class TerminalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(min_length=1, max_length=32_768)
    cwd: str | None = None
    timeout_sec: int = Field(default=30, ge=1, le=120)


class HarborTerminal:
    """Execute only through Harbor; preserve full output in the trial logs."""

    def __init__(self, environment: BaseEnvironment, log_path: Path) -> None:
        self.environment = environment
        self.log_path = log_path
        self.calls = 0

    async def __call__(self, arguments: dict[str, Any]) -> dict[str, Any]:
        request = TerminalInput.model_validate(arguments)
        self.calls += 1
        call_id = self.calls
        started = time.monotonic()
        self._log({"event": "start", "call_id": call_id, **request.model_dump()})
        try:
            result = await self.environment.exec(
                command=request.command,
                cwd=request.cwd,
                timeout_sec=request.timeout_sec,
            )
        except (Exception, asyncio.CancelledError) as exc:
            self._log({"event": "error", "call_id": call_id, "error": type(exc).__name__})
            raise
        output = {
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "return_code": result.return_code,
        }
        self._log(
            {"event": "end", "call_id": call_id, "seconds": time.monotonic() - started, **output}
        )
        return {
            "stdout": (result.stdout or "")[:MAX_OUTPUT_CHARS],
            "stderr": (result.stderr or "")[:MAX_OUTPUT_CHARS],
            "return_code": result.return_code,
            "truncated": any(
                len(value or "") > MAX_OUTPUT_CHARS for value in (result.stdout, result.stderr)
            ),
        }

    def _log(self, event: dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


HARBOR_GUIDANCE = """Complete the task by acting in the Harbor container through terminal().
The local context contains only the task instruction; task files are inside the container.
The trusted terminal controller tool is authorized to read and write container files and run
programs. Local Codex tools cannot access the container. Use terminal for all task inspection
and execution. Its commands use fresh shells; use cwd or an explicit cd for each command.
Print the returned dictionary to inspect stdout, stderr, and return_code on the next turn.
For example, return a fenced repl block containing print(terminal(command="pwd && ls -la")).
Use shell commands or heredocs through terminal to create files. Do not import Python modules
in the REPL. Complete and check the requested work before calling submit_answer with a summary.
Only recursive nodes have terminal access; terminal leaves can reason about supplied text.
"""


class RcodexAgent(BaseAgent):
    """Load with Harbor's --agent benchmarks.harbor.agents:RcodexAgent option."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        provider_base_url: str | None = None,
        reasoning_effort: str = "low",
        max_depth: int = 1,
        max_iterations: int = 12,
        max_concurrency: int = 1,
        max_tokens: int = 200_000,
        run_timeout_seconds: float = 1800,
        **kwargs: Any,
    ) -> None:
        model_name, provider_base_url = _local_model_settings(model_name, provider_base_url)
        if max_depth < 1:
            raise ValueError("Harbor rcodex requires max_depth >= 1 for container tool access")
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self.run_config = RunConfig(
            model=model_name,
            provider_base_url=provider_base_url,
            reasoning_effort=reasoning_effort,
            max_depth=max_depth,
            max_iterations=max_iterations,
            max_concurrency=max_concurrency,
            max_tokens=max_tokens,
            max_errors=3,
            run_timeout_seconds=run_timeout_seconds,
            node_timeout_seconds=run_timeout_seconds,
            leaf_timeout_seconds=min(120, run_timeout_seconds),
            tool_timeout_seconds=125,
            custom_system_prompt=HARBOR_GUIDANCE,
        )

    @staticmethod
    def name() -> str:
        return "rcodex"

    def version(self) -> str:
        return __version__

    async def setup(self, environment: BaseEnvironment) -> None:
        codex_runtime_path()
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        context_root = self.logs_dir / "context"
        context_root.mkdir(parents=True, exist_ok=True)
        (context_root / "instruction.md").write_text(instruction, encoding="utf-8")
        terminal = HarborTerminal(environment, self.logs_dir / "terminal.jsonl")
        # Codex's caches are host-only; Harbor mirrors logs_dir into the container.
        codex_home = self.logs_dir.with_name(f".{self.logs_dir.name}-codex-home").resolve()
        ensure_private_directory(codex_home)
        runner = RecursiveRunner(
            adapter_factory=partial(SdkCodexAdapter, codex_home=codex_home),
            custom_tools={
                "terminal": ToolSpec(
                    name="terminal",
                    description=(
                        "Execute a shell command inside the Harbor task container. Authorized "
                        "to read/write files and run programs there. Returns stdout, stderr, "
                        "return_code, and truncated. Each call starts a fresh shell; cwd "
                        "defaults to the container working directory. Timeout: 1..120 seconds."
                    ),
                    handler=terminal.__call__,
                    input_model=TerminalInput,
                )
            },
        )
        result: RunResult | None = None
        try:
            result, _ = await runner.run(
                task=instruction,
                context=context_root,
                state_directory=self.logs_dir / "rcodex-state",
                config=self.run_config,
                strategy=RunStrategy.recursive,
            )
            if result.status != RunStatus.succeeded:
                message = result.error.message if result.error else result.status.value
                if result.status == RunStatus.timed_out:
                    raise AgentTimeoutError(f"rcodex run timed out: {message}")
                raise NonZeroAgentExitCodeError(f"rcodex run failed: {message}")
        finally:
            # On Harbor cancellation the runner persists a result before re-raising.
            if result is None:
                result_path = next(
                    (self.logs_dir / "rcodex-state" / "runs").glob("*/result.json"), None
                )
                if result_path is not None:
                    result = RunResult.model_validate_json(result_path.read_text(encoding="utf-8"))
            if result is not None:
                self._populate_context(context, result)
            context.metadata = {**(context.metadata or {}), "terminal_calls": terminal.calls}

    @staticmethod
    def _populate_context(context: AgentContext, result: RunResult) -> None:
        if result.usage.available and result.usage.total is not None:
            usage = result.usage.total
            context.n_input_tokens = usage.input_tokens
            context.n_cache_tokens = usage.cached_input_tokens
            context.n_output_tokens = usage.output_tokens
        context.metadata = {
            **(context.metadata or {}),
            "rcodex_status": result.status.value,
            "rcodex_duration_ms": result.duration_ms,
            "rcodex_run_id": result.run_id,
            "rcodex_nodes": len(result.nodes),
        }
