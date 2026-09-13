from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.agent.context import AgentContext
from harbor.trial.errors import AgentTimeoutError
from rcodex.codex_adapter.base import AdapterTimeoutError, DirectTurn
from rcodex.models import RunUsage, TokenUsageBreakdown

from benchmarks.harbor import agents
from benchmarks.harbor.agents import (
    MAX_OUTPUT_CHARS,
    HarborTerminal,
    RcodexAgent,
)
from benchmarks.harbor.baseline import CodexBaselineAgent


@pytest.fixture
def model_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RCODEX_MODEL", "local-model")
    monkeypatch.setenv("RCODEX_PROVIDER_BASE_URL", "http://localhost:30000/v1")


def environment_mock(**kwargs: Any) -> Any:
    return AsyncMock(spec=BaseEnvironment, **kwargs)


def model_adapter(steps: list[DirectTurn | BaseException]) -> AsyncMock:
    session = AsyncMock()
    session.thread_id = "thread-harbor-test"
    session.codex_runtime_version = "0.153.4-test"
    session.turn.side_effect = steps
    adapter = AsyncMock()
    adapter.start_root.return_value = session
    return adapter


@pytest.mark.asyncio
async def test_terminal_preserves_exit_and_full_logs(tmp_path: Path) -> None:
    environment = environment_mock()
    environment.exec = AsyncMock(
        return_value=ExecResult(stdout="x" * 20_000, stderr="failed", return_code=7)
    )
    log = tmp_path / "terminal.jsonl"
    terminal = HarborTerminal(environment, log)
    result = await terminal({"command": "a quoted ' command", "cwd": "/work", "timeout_sec": 8})

    environment.exec.assert_awaited_once_with(
        command="a quoted ' command", cwd="/work", timeout_sec=8
    )
    assert result["return_code"] == 7
    assert result["truncated"] is True
    assert len(result["stdout"]) == MAX_OUTPUT_CHARS
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(events[1]["stdout"]) == 20_000
    assert events[0]["event"] == "start"


@pytest.mark.asyncio
async def test_terminal_records_cancellation(tmp_path: Path) -> None:
    environment = environment_mock()
    environment.exec = AsyncMock(side_effect=asyncio.CancelledError)
    log = tmp_path / "terminal.jsonl"
    with pytest.raises(asyncio.CancelledError):
        await HarborTerminal(environment, log)({"command": "sleep 30"})
    assert json.loads(log.read_text().splitlines()[-1])["error"] == "CancelledError"


@pytest.mark.asyncio
async def test_recursive_run_routes_to_container_and_reports_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_env: None
) -> None:
    usage = TokenUsageBreakdown(
        input_tokens=100,
        cached_input_tokens=0,
        output_tokens=20,
        reasoning_output_tokens=0,
        total_tokens=120,
    )
    adapter = model_adapter(
        [
            DirectTurn(
                thread_id="thread-harbor-test",
                final_response='```repl\nprint(terminal(command="pwd"))\n```',
                usage=RunUsage(available=True, last=usage, total=usage),
                codex_runtime_version="0.153.4-test",
            ),
            DirectTurn(
                thread_id="thread-harbor-test",
                final_response='```repl\nsubmit_answer("Completed in container")\n```',
                usage=RunUsage(available=True, last=usage, total=usage),
                codex_runtime_version="0.153.4-test",
            ),
        ]
    )
    monkeypatch.setattr(agents, "SdkCodexAdapter", lambda **kwargs: adapter)
    environment = environment_mock()
    environment.exec = AsyncMock(return_value=ExecResult(stdout="/app", return_code=0))
    agent = RcodexAgent(logs_dir=tmp_path / "agent")
    context = AgentContext()

    await agent.run("Inspect the container", environment, context)

    environment.exec.assert_awaited_once_with(command="pwd", cwd=None, timeout_sec=30)
    assert context.n_input_tokens == 100
    assert context.n_output_tokens == 20
    assert context.cost_usd is None
    assert context.metadata is not None
    assert context.metadata["terminal_calls"] == 1
    assert context.metadata["rcodex_status"] == "succeeded"
    result_path = next((agent.logs_dir / "rcodex-state" / "runs").glob("*/result.json"))
    assert json.loads(result_path.read_text())["status"] == "succeeded"
    assert adapter.start_root.call_args.args[0].provider_base_url == "http://localhost:30000/v1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "harbor_error", "status"),
    [
        (RuntimeError("model failed"), NonZeroAgentExitCodeError, "failed"),
        (AdapterTimeoutError("model turn exceeded its deadline"), AgentTimeoutError, "timed_out"),
        (asyncio.CancelledError(), asyncio.CancelledError, "cancelled"),
    ],
)
async def test_unsuccessful_run_preserves_result_and_reports_harbor_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_env: None,
    error: BaseException,
    harbor_error: type[BaseException],
    status: str,
) -> None:
    adapter = model_adapter([error])
    monkeypatch.setattr(agents, "SdkCodexAdapter", lambda **kwargs: adapter)
    agent = RcodexAgent(logs_dir=tmp_path / "agent")
    context = AgentContext()
    with pytest.raises(harbor_error):
        await agent.run("Inspect the container", environment_mock(), context)
    assert context.metadata is not None
    assert context.metadata["rcodex_status"] == status
    assert context.metadata["terminal_calls"] == 0
    result_path = next((agent.logs_dir / "rcodex-state" / "runs").glob("*/result.json"))
    assert json.loads(result_path.read_text())["status"] == status
    assert (tmp_path / ".agent-codex-home").is_dir()
    assert not (tmp_path / "agent" / "codex-home").exists()


def test_harbor_requires_recursive_tool_access(tmp_path: Path, model_env: None) -> None:
    with pytest.raises(ValueError, match="max_depth >= 1"):
        RcodexAgent(logs_dir=tmp_path, max_depth=0)


def test_explicit_provider_overrides_dotenv(tmp_path: Path, model_env: None) -> None:
    agent = RcodexAgent(
        logs_dir=tmp_path, model_name="override", provider_base_url="http://localhost:8000/v1"
    )
    assert agent.run_config.model == "override"
    assert agent.run_config.provider_base_url == "http://localhost:8000/v1"


def test_baseline_uses_native_codex_with_local_provider_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_env: None
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-host-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unrelated.example/v1")
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", "/unrelated/auth.json")
    monkeypatch.setenv("CODEX_FORCE_AUTH_JSON", "true")
    monkeypatch.delenv("RCODEX_PROVIDER_API_KEY", raising=False)
    baseline = CodexBaselineAgent(logs_dir=tmp_path)
    recursive = RcodexAgent(logs_dir=tmp_path)

    assert baseline.model_name == recursive.run_config.model
    assert baseline.model_connection.configured_base_url == recursive.run_config.provider_base_url
    assert baseline.model_connection.api_key == ""
    assert baseline._resolve_auth_json_path() is None
    assert "CODEX_FORCE_AUTH_JSON" not in baseline.extra_env
    assert baseline.version() == "0.153.4"
    assert type(baseline).setup is Codex.setup
    assert baseline.render_instruction("Original task") == "Original task"
    config = baseline._build_effective_config()
    assert config["model_providers"]["local"] == {
        "name": "Local Responses provider",
        "base_url": "http://localhost:30000/v1",
        "wire_api": "responses",
        "requires_openai_auth": False,
    }
    assert "base_instructions" not in config
    assert "model_instructions_file" not in config


def test_baseline_passes_only_the_local_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_env: None
) -> None:
    monkeypatch.setenv("RCODEX_PROVIDER_API_KEY", "local-test-key")
    baseline = CodexBaselineAgent(
        logs_dir=tmp_path, model_name="override", provider_base_url="http://localhost:8000/v1"
    )
    assert baseline.model_name == "override"
    assert baseline.model_connection.configured_base_url == "http://localhost:8000/v1"
    assert baseline.model_connection.api_key == "local-test-key"
    config = baseline._build_effective_config()
    assert config["model_providers"]["local"]["env_key"] == "OPENAI_API_KEY"
    assert "local-test-key" not in json.dumps(config)


@pytest.mark.asyncio
async def test_baseline_delegates_native_run_and_restores_forwarding_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_env: None
) -> None:
    baseline = CodexBaselineAgent(logs_dir=tmp_path, provider_gateway_host="host.docker.internal")
    native_run = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(Codex, "run", native_run)
    environment = environment_mock()
    context = AgentContext()
    with pytest.raises(asyncio.CancelledError):
        await baseline.run("Original task", environment, context)
    native_run.assert_awaited_once_with("Original task", environment, context)
    assert baseline.model_connection.configured_base_url == "http://localhost:30000/v1"
    assert baseline._base_config["model_providers"]["local"]["base_url"] == (
        "http://localhost:30000/v1"
    )


@pytest.mark.asyncio
async def test_baseline_keeps_harbor_post_run_usage_hook_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_env: None
) -> None:
    baseline = CodexBaselineAgent(logs_dir=tmp_path)
    monkeypatch.setattr(Codex, "run", AsyncMock())
    context = AgentContext()
    await baseline.run("Original task", environment_mock(), context)
    assert context.is_empty()  # Harbor skips usage parsing if run() adds metadata.

    def populate_native(self: Codex, context: AgentContext) -> None:
        context.n_input_tokens = 123
        context.n_output_tokens = 45

    monkeypatch.setattr(Codex, "populate_context_post_run", populate_native)
    baseline.populate_context_post_run(context)
    assert context.n_input_tokens == 123
    assert context.n_output_tokens == 45
    assert context.metadata == {
        "provider_base_url": "http://localhost:30000/v1",
        "provider_gateway_host": None,
    }
