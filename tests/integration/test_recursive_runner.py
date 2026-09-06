from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

from rcodex.api import AsyncRLM
from rcodex.codex_adapter import (
    AdapterCleanupError,
    AdapterRuntimeError,
    DirectTurn,
    DirectTurnRequest,
    RootSessionRequest,
    RootTurnRequest,
)
from rcodex.config import RunConfig
from rcodex.context import ManifestCancelledError
from rcodex.models import (
    BatchFailure,
    CallMode,
    CallStatus,
    ContextManifest,
    RunErrorCode,
    RunRequest,
    RunResult,
    RunStatus,
    RunStrategy,
    RunUsage,
    TokenUsageBreakdown,
)
from rcodex.prompts import prompt_sha256
from rcodex.runner import RecursiveRunner
from rcodex.storage import read_session, session_path


@dataclass(frozen=True, slots=True)
class _Step:
    response: str | None = None
    error: Exception | None = None
    delay: float = 0
    usage: RunUsage | None = None
    action: Callable[[], None] | None = None


class _FakeSession:
    def __init__(
        self,
        thread_id: str,
        steps: list[_Step],
        adapter: _FakeAdapter,
        close_error: Exception | None = None,
    ) -> None:
        self._thread_id = thread_id
        self._steps = deque(steps)
        self._adapter = adapter
        self._close_error = close_error
        self.turn_requests: list[RootTurnRequest] = []
        self.turn_started = asyncio.Event()
        self.compactions = 0
        self.closed = False

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def codex_runtime_version(self) -> str:
        return "0.153.4-test"

    async def turn(self, request: RootTurnRequest) -> DirectTurn:
        self.turn_requests.append(request)
        self.turn_started.set()
        if not self._steps:
            raise AssertionError(f"no scripted turn remains for {self.thread_id}")
        step = self._steps.popleft()
        if step.delay:
            await asyncio.sleep(step.delay)
        if step.error is not None:
            raise step.error
        if step.action is not None:
            step.action()
        assert step.response is not None
        turn = DirectTurn(
            thread_id=self.thread_id,
            final_response=step.response,
            usage=step.usage or RunUsage(available=False),
            codex_runtime_version=self.codex_runtime_version,
        )
        self._adapter.completed_turns.append(turn)
        return turn

    async def compact(self, timeout_seconds: float) -> None:
        del timeout_seconds
        self.compactions += 1

    async def close(self, timeout_seconds: float) -> None:
        del timeout_seconds
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _FakeAdapter:
    def __init__(
        self,
        *,
        root_scripts: list[list[_Step]] | None = None,
        leaf_steps: list[_Step] | None = None,
        root_close_errors: list[Exception | None] | None = None,
    ) -> None:
        self.root_scripts = deque(root_scripts or [])
        self.leaf_steps = deque(leaf_steps or [])
        self.root_close_errors = deque(root_close_errors or [])
        self.start_requests: list[RootSessionRequest] = []
        self.leaf_requests: list[DirectTurnRequest] = []
        self.sessions: list[_FakeSession] = []
        self.completed_turns: list[DirectTurn] = []

    async def start_root(self, request: RootSessionRequest) -> _FakeSession:
        self.start_requests.append(request)
        if not self.root_scripts:
            raise AssertionError("no scripted root session remains")
        thread_id = request.thread_id or f"thread-root-{len(self.sessions) + 1}"
        close_error = self.root_close_errors.popleft() if self.root_close_errors else None
        session = _FakeSession(
            thread_id,
            self.root_scripts.popleft(),
            self,
            close_error,
        )
        self.sessions.append(session)
        return session

    async def run_leaf(self, request: DirectTurnRequest) -> DirectTurn:
        self.leaf_requests.append(request)
        if not self.leaf_steps:
            raise AssertionError("no scripted leaf turn remains")
        step = self.leaf_steps.popleft()
        if step.delay:
            await asyncio.sleep(step.delay)
        if step.error is not None:
            raise step.error
        if step.action is not None:
            step.action()
        assert step.response is not None
        turn = DirectTurn(
            thread_id=f"thread-leaf-{len(self.leaf_requests)}",
            final_response=step.response,
            usage=step.usage or RunUsage(available=False),
            codex_runtime_version="0.153.4-test",
        )
        self.completed_turns.append(turn)
        return turn


class _RoutingDirectAdapter:
    async def start_root(self, request: RootSessionRequest) -> _FakeSession:
        del request
        raise AssertionError("direct batch must not open root sessions")

    async def run_leaf(self, request: DirectTurnRequest) -> DirectTurn:
        if '"bad"' in request.prompt:
            response = "not-json"
        elif '"slow"' in request.prompt:
            await asyncio.sleep(0.02)
            response = _final_payload("slow-answer")
        else:
            response = _final_payload("fast-answer")
        return DirectTurn(
            thread_id=f"thread-{request.node_id}",
            final_response=response,
            usage=RunUsage(available=False),
            codex_runtime_version="0.153.4-test",
        )


class _FailingFactory:
    def __call__(self) -> _RoutingDirectAdapter:
        raise AdapterRuntimeError("thread_start", "RuntimeError")


class _FinalRootFactory:
    def __init__(self) -> None:
        self.adapters: list[_FakeAdapter] = []

    def __call__(self) -> _FakeAdapter:
        adapter = _FakeAdapter(root_scripts=[[_Step(response=_final_repl("normalized"))]])
        self.adapters.append(adapter)
        return adapter


@pytest.fixture
def context_root(tmp_path: Path) -> Path:
    root = tmp_path / "context"
    root.mkdir()
    (root / "notes.txt").write_text("known context\n", encoding="utf-8")
    return root


def _config(**updates: Any) -> RunConfig:
    values: dict[str, Any] = {
        "run_timeout_seconds": 3.0,
        "node_timeout_seconds": 2.0,
        "leaf_timeout_seconds": 2.0,
        "cleanup_timeout_seconds": 0.1,
        "tool_timeout_seconds": 0.5,
        "max_iterations": 4,
        "max_depth": 3,
        "max_concurrency": 4,
    }
    values.update(updates)
    return RunConfig(**values)


def _final_payload(answer: str) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "answer": answer,
            "evidence": [],
            "uncertainties": [],
        }
    )


def _final_repl(answer: str) -> str:
    return f"```repl\nsubmit_answer({json.dumps(answer)})\n```"


def _repl_continue(progress: str, calls: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    delegates = [call for call in calls if call["kind"] == "delegate"]
    if len(delegates) == len(calls) and len(delegates) > 1:
        mode = delegates[0]["mode"]
        assert all(call["mode"] == mode for call in delegates)
        function = "rlm_query_batched" if mode == "recursive" else "llm_query_batched"
        prompts = [call["task"] for call in delegates]
        lines.append(f"delegate_results = {function}({json.dumps(prompts)})")
    else:
        for index, call in enumerate(calls):
            if call["kind"] == "delegate":
                function = "rlm_query" if call["mode"] == "recursive" else "llm_query"
                lines.append(f"result_{index} = {function}({json.dumps(call['task'])})")
            else:
                arguments = ", ".join(
                    f"{name}={json.dumps(value)}" for name, value in call["arguments"].items()
                )
                lines.append(f"result_{index} = {call['name']}({arguments})")
    lines.append(f"print({json.dumps(progress)})")
    return "```repl\n" + "\n".join(lines) + "\n```"


def _delegate(call_id: str, task: str, mode: str = "leaf") -> dict[str, Any]:
    return {
        "kind": "delegate",
        "call_id": call_id,
        "mode": mode,
        "task": task,
        "model": None,
        "reasoning_effort": None,
    }


@pytest.mark.asyncio
async def test_manifest_scan_is_off_loop_and_receives_cancellation(
    context_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = Event()
    stopped = Event()

    def blocked_scan(
        context: Path,
        config: RunConfig,
        *,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> ContextManifest:
        del context, config, deadline
        assert cancel_event is not None
        started.set()
        if not cancel_event.wait(1.0):
            raise AssertionError("manifest scan blocked the event loop")
        stopped.set()
        raise ManifestCancelledError("context scan cancelled")

    monkeypatch.setattr("rcodex.runner.build_manifest", blocked_scan)
    active = asyncio.create_task(
        RecursiveRunner(lambda: _FakeAdapter()).run(
            task="cancel manifest",
            context=context_root,
            state_directory=tmp_path / "state",
            config=_config(),
        )
    )
    assert await asyncio.to_thread(started.wait, 1.0)
    active.cancel()

    with pytest.raises(asyncio.CancelledError):
        await active
    assert await asyncio.to_thread(stopped.wait, 1.0)


def _tool(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "kind": "tool",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _usage(last: int, total: int) -> RunUsage:
    return RunUsage(
        available=True,
        last=TokenUsageBreakdown(
            input_tokens=last,
            cached_input_tokens=0,
            cache_write_input_tokens=0,
            output_tokens=0,
            reasoning_output_tokens=0,
            total_tokens=last,
        ),
        total=TokenUsageBreakdown(
            input_tokens=total,
            cached_input_tokens=0,
            cache_write_input_tokens=0,
            output_tokens=0,
            reasoning_output_tokens=0,
            total_tokens=total,
        ),
        model_context_window=1000,
    )


def _iteration(result: RunResult, node_id: str, number: int) -> dict[str, Any]:
    directory = result.artifacts.iterations_directory
    assert directory is not None
    path = Path(directory) / node_id / f"{number:03d}.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _replace_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def _only_persisted_result(state_directory: Path) -> RunResult:
    result_paths = list((state_directory / "runs").glob("*/result.json"))
    assert len(result_paths) == 1
    return RunResult.model_validate_json(result_paths[0].read_bytes(), strict=True)


def _private_modes(root: Path) -> tuple[dict[str, int], dict[str, int]]:
    directories: dict[str, int] = {}
    files: dict[str, int] = {}
    for path in [root, *root.rglob("*")]:
        relative = path.relative_to(root).as_posix() or "."
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            directories[relative] = mode
        elif path.is_file():
            files[relative] = mode
    return directories, files


def _events(result: RunResult) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], json.loads(line))
        for line in Path(result.artifacts.events).read_text(encoding="utf-8").splitlines()
    ]


async def _wait_for_session(adapter: _FakeAdapter, index: int = 0) -> _FakeSession:
    for _ in range(100):
        if len(adapter.sessions) > index:
            session = adapter.sessions[index]
            await session.turn_started.wait()
            return session
        await asyncio.sleep(0.001)
    raise AssertionError("scripted root session did not start")


@pytest.mark.asyncio
async def test_direct_success_uses_one_terminal_leaf(context_root: Path, tmp_path: Path) -> None:
    adapter = _FakeAdapter(leaf_steps=[_Step(response=_final_payload("direct"))])
    result, output = await RecursiveRunner(lambda: adapter).run(
        task="answer directly",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
        strategy=RunStrategy.direct,
    )

    assert output is None
    assert result.status == RunStatus.succeeded
    assert result.payload is not None and result.payload.answer == "direct"
    assert len(result.nodes) == 1
    assert result.nodes[0].requested_mode == CallMode.leaf
    assert result.nodes[0].executed_mode == CallMode.leaf
    assert result.runtime.adapter_kind == "custom"
    assert result.runtime.requested_capabilities is None
    leaf_prompt = adapter.leaf_requests[0].prompt
    leaf_hash = prompt_sha256(leaf_prompt)
    assert result.nodes[0].initial_prompt_sha256 == leaf_hash
    assert result.runtime.prompt_sha256s["leaf"] == leaf_hash
    assert adapter.start_requests == []
    persisted = RunResult.model_validate_json(_read_bytes(result.artifacts.result), strict=True)
    assert persisted == result


@pytest.mark.asyncio
async def test_recursive_node_can_finish_without_delegation(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(root_scripts=[[_Step(response=_final_repl("root-final"))]])
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="solve",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.succeeded
    assert result.payload is not None and result.payload.answer == "root-final"
    assert result.nodes[0].iterations == 1
    assert result.nodes[0].thread_id == "thread-root-1"
    assert adapter.sessions[0].turn_requests[0].output_schema is None
    initial_prompt = adapter.sessions[0].turn_requests[0].prompt
    initial_hash = prompt_sha256(initial_prompt)
    assert result.nodes[0].initial_prompt_sha256 == initial_hash
    assert result.runtime.prompt_sha256s["node_initial"] == initial_hash
    assert _iteration(result, "node_000001", 0)["prompt_sha256"] == initial_hash
    assert adapter.sessions[0].closed


@pytest.mark.asyncio
async def test_root_python_can_transform_llm_query_result_before_submitting(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=(
                        "```repl\n"
                        "raw = llm_query('Return only the integer 41')\n"
                        "value = int(raw) + 1\n"
                        "submit_answer(str(value))\n"
                        "```"
                    )
                )
            ]
        ],
        leaf_steps=[_Step(response=_final_payload("41"))],
    )

    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="compute through a leaf",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.succeeded
    assert result.payload is not None and result.payload.answer == "42"
    iteration = _iteration(result, "node_000001", 0)
    assert iteration["executions"][0]["variable_types"] == {
        "raw": "str",
        "value": "int",
    }
    assert iteration["results"][0]["payload"]["answer"] == "41"
    assert len(adapter.sessions[0].turn_requests) == 1


@pytest.mark.asyncio
async def test_recursive_node_python_namespace_persists_across_turns(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(response="```repl\nparts = ['persistent']\nprint(parts)\n```"),
                _Step(
                    response=(
                        "```repl\nparts.append('namespace')\nsubmit_answer(' '.join(parts))\n```"
                    )
                ),
            ]
        ]
    )

    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="use persistent state",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.succeeded
    assert result.payload is not None and result.payload.answer == "persistent namespace"
    assert _iteration(result, "node_000001", 1)["executions"][0]["variable_types"] == {
        "parts": "list"
    }


@pytest.mark.asyncio
async def test_dependent_delegation_runs_in_multiple_waves(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(response=_repl_continue("need first", [_delegate("first", "first task")])),
                _Step(response=_repl_continue("need second", [_delegate("second", "second task")])),
                _Step(response=_final_repl("synthesized")),
            ]
        ],
        leaf_steps=[
            _Step(response=_final_payload("first-result")),
            _Step(response=_final_payload("second-result")),
        ],
    )
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="solve in stages",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.succeeded
    assert result.payload is not None and result.payload.answer == "synthesized"
    assert len(result.nodes) == 3
    root_turns = adapter.sessions[0].turn_requests
    assert "first-result" in root_turns[1].prompt
    assert "second-result" not in root_turns[1].prompt
    assert "second-result" in root_turns[2].prompt
    assert _iteration(result, "node_000001", 0)["prompt_sha256"] == prompt_sha256(
        root_turns[0].prompt
    )
    assert _iteration(result, "node_000001", 1)["prompt_sha256"] == prompt_sha256(
        root_turns[1].prompt
    )
    assert _iteration(result, "node_000001", 2)["prompt_sha256"] == prompt_sha256(
        root_turns[2].prompt
    )
    assert result.runtime.prompt_sha256s["feedback"] == prompt_sha256(root_turns[2].prompt)
    assert [request.node_id for request in adapter.leaf_requests] == [
        "node_000002",
        "node_000003",
    ]


@pytest.mark.asyncio
async def test_nested_recursion_becomes_leaf_at_depth_boundary(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_repl_continue(
                        "recurse once", [_delegate("child", "child task", "recursive")]
                    )
                ),
                _Step(response=_final_repl("root answer")),
            ],
            [
                _Step(
                    response=_repl_continue(
                        "recurse at boundary",
                        [_delegate("grandchild", "grandchild task", "recursive")],
                    )
                ),
                _Step(response=_final_repl("child answer")),
            ],
        ],
        leaf_steps=[_Step(response=_final_payload("boundary leaf"))],
    )
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="nested",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_depth=2),
    )

    assert result.status == RunStatus.succeeded
    assert [(node.depth, node.requested_mode, node.executed_mode) for node in result.nodes] == [
        (0, CallMode.recursive, CallMode.recursive),
        (1, CallMode.recursive, CallMode.recursive),
        (2, CallMode.recursive, CallMode.leaf),
    ]
    assert len(adapter.sessions) == 2
    assert len(adapter.leaf_requests) == 1


@pytest.mark.asyncio
async def test_call_wave_preserves_order_when_one_leaf_fails(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_repl_continue(
                        "parallel",
                        [_delegate("first", "slow"), _delegate("second", "fails")],
                    )
                ),
                _Step(response=_final_repl("recovered")),
            ]
        ],
        leaf_steps=[
            _Step(response=_final_payload("slow-result"), delay=0.02),
            _Step(error=AdapterRuntimeError("turn_run", "RuntimeError")),
        ],
    )
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="partial wave",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.succeeded
    record = _iteration(result, "node_000001", 0)
    assert len(record["results"]) == 2
    assert all(item["call_id"].startswith("q_") for item in record["results"])
    assert [item["status"] for item in record["results"]] == [
        CallStatus.succeeded,
        CallStatus.failed,
    ]
    feedback = adapter.sessions[0].turn_requests[1].prompt
    assert feedback.index(record["results"][0]["call_id"]) < feedback.index(
        record["results"][1]["call_id"]
    )
    assert "slow-result" in feedback
    assert "Codex SDK failure during turn_run: RuntimeError" in feedback


@pytest.mark.asyncio
async def test_batched_runs_preserve_input_order_and_isolate_failure(
    context_root: Path, tmp_path: Path
) -> None:
    results = await RecursiveRunner(_RoutingDirectAdapter).run_batched(
        ["slow", "bad", "fast"],
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_concurrency=2),
        strategy=RunStrategy.direct,
    )

    assert all(isinstance(result, RunResult) for result in results)
    narrowed = [result for result in results if isinstance(result, RunResult)]
    assert [result.status for result in narrowed] == [
        RunStatus.succeeded,
        RunStatus.failed,
        RunStatus.succeeded,
    ]
    assert narrowed[0].payload is not None and narrowed[0].payload.answer == "slow-answer"
    assert narrowed[1].error is not None
    assert narrowed[1].error.code == RunErrorCode.invalid_model_output
    assert narrowed[2].payload is not None and narrowed[2].payload.answer == "fast-answer"


@pytest.mark.asyncio
async def test_batch_failure_is_narrowed_without_assuming_run_result(
    context_root: Path, tmp_path: Path
) -> None:
    results = await RecursiveRunner(_FailingFactory()).run_batched(
        ["startup failure"],
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_concurrency=1),
        strategy=RunStrategy.direct,
    )

    slot = results[0]
    assert isinstance(slot, BatchFailure)
    assert slot.task_preview == "startup failure"
    assert slot.task_sha256 == hashlib.sha256(b"startup failure").hexdigest()
    assert slot.error.code == RunErrorCode.sdk_startup


@pytest.mark.asyncio
async def test_async_api_projects_batch_failure_as_ordered_completion(
    context_root: Path, tmp_path: Path
) -> None:
    async with AsyncRLM(
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_concurrency=1),
        adapter_factory=_FailingFactory(),
    ) as client:
        completions = await client.completion_batched(["startup failure"])

    assert len(completions) == 1
    assert completions[0].response.startswith("Error: ")
    assert isinstance(completions[0].run, BatchFailure)
    assert completions[0].run.error.code == RunErrorCode.sdk_startup


@pytest.mark.asyncio
async def test_max_iterations_gets_one_finalization_turn(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(response=_repl_continue("still working", [_tool("lookup", "constant")])),
                _Step(response=_final_repl("forced-final")),
            ]
        ]
    )
    result, _ = await RecursiveRunner(lambda: adapter, custom_tools={"constant": 7}).run(
        task="must finalize",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_iterations=1),
    )

    assert result.status == RunStatus.succeeded
    assert result.payload is not None and result.payload.answer == "forced-final"
    assert result.nodes[0].iterations == 2
    assert adapter.sessions[0].turn_requests[-1].phase.endswith("_finalization")
    assert _iteration(result, "node_000001", 1)["phase"] == "finalization"


@pytest.mark.asyncio
async def test_custom_tool_result_is_returned_to_the_node(
    context_root: Path, tmp_path: Path
) -> None:
    seen: list[dict[str, Any]] = []

    def add(arguments: dict[str, Any]) -> int:
        seen.append(arguments)
        return int(arguments["left"]) + int(arguments["right"])

    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(response=_repl_continue("calculate", [_tool("sum", "add", left=2, right=3)])),
                _Step(response=_final_repl("five")),
            ]
        ]
    )
    result, _ = await RecursiveRunner(lambda: adapter, custom_tools={"add": add}).run(
        task="use tool",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.succeeded
    assert seen == [{"left": 2, "right": 3}]
    record = _iteration(result, "node_000001", 0)
    assert record["results"][0]["value"] == 5
    assert record["executions"][0]["variable_types"]["result_0"] == "int"
    assert len(result.nodes) == 1


@pytest.mark.asyncio
async def test_persistent_run_resumes_root_thread_and_versions_history(
    context_root: Path, tmp_path: Path
) -> None:
    state_directory = tmp_path / "state"
    first_task = "t" * 2100
    first_answer = "a" * 4100
    adapter = _FakeAdapter(
        root_scripts=[
            [_Step(response=_final_repl(first_answer))],
            [_Step(response=_final_repl("second"))],
        ]
    )
    runner = RecursiveRunner(lambda: adapter)
    first, _ = await runner.run(
        task=first_task,
        context=context_root,
        state_directory=state_directory,
        config=_config(persistent=True),
    )
    session_id = first.runtime.session_id
    assert session_id is not None

    second, _ = await runner.run(
        task="follow up",
        context=context_root,
        state_directory=state_directory,
        config=_config(persistent=True),
        session_id=session_id,
    )

    assert second.status == RunStatus.succeeded
    assert [request.thread_id for request in adapter.start_requests] == [
        None,
        "thread-root-1",
    ]
    assert "persistent context version 0" in adapter.sessions[0].turn_requests[0].prompt
    assert "persistent context version 1" in adapter.sessions[1].turn_requests[0].prompt
    persisted = read_session(session_path(state_directory.resolve(), session_id))
    assert persisted.root_thread_id == "thread-root-1"
    assert persisted.context_count == 2
    assert persisted.history[0].task_preview == first_task[:2000]
    assert (
        persisted.history[0].task_sha256 == hashlib.sha256(first_task.encode("utf-8")).hexdigest()
    )
    assert persisted.history[0].answer_preview == first_answer[:4000]
    assert (
        persisted.history[0].answer_sha256
        == hashlib.sha256(first_answer.encode("utf-8")).hexdigest()
    )
    assert persisted.history[1].task_preview == "follow up"
    assert persisted.history[1].answer_preview == "second"


@pytest.mark.asyncio
async def test_single_turn_semaphore_does_not_deadlock_delegation(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(response=_repl_continue("delegate", [_delegate("leaf", "child")])),
                _Step(response=_final_repl("done")),
            ]
        ],
        leaf_steps=[_Step(response=_final_payload("child result"))],
    )
    result, _ = await asyncio.wait_for(
        RecursiveRunner(lambda: adapter).run(
            task="semaphore",
            context=context_root,
            state_directory=tmp_path / "state",
            config=_config(max_concurrency=1),
        ),
        timeout=1,
    )

    assert result.status == RunStatus.succeeded


@pytest.mark.asyncio
async def test_call_limit_rejects_overflow_in_original_order(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_repl_continue(
                        "too many",
                        [_delegate("accepted", "one"), _delegate("overflow", "two")],
                    )
                ),
                _Step(response=_final_repl("bounded")),
            ]
        ],
        leaf_steps=[_Step(response=_final_payload("one result"))],
    )
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="bounded calls",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_calls_per_iteration=1, max_calls_per_node=1),
    )

    record = _iteration(result, "node_000001", 0)
    assert len(record["results"]) == 2
    assert all(item["call_id"].startswith("q_") for item in record["results"])
    assert record["results"][1]["status"] == CallStatus.rejected
    assert record["results"][1]["error"]["code"] == RunErrorCode.call_limit


@pytest.mark.asyncio
async def test_node_limit_rejects_delegation_without_starting_child(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(response=_repl_continue("no capacity", [_delegate("child", "blocked")])),
                _Step(response=_final_repl("bounded")),
            ]
        ]
    )
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="node budget",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_total_nodes=1),
    )

    assert result.status == RunStatus.succeeded
    assert len(result.nodes) == 1
    record = _iteration(result, "node_000001", 0)
    assert record["results"][0]["status"] == CallStatus.rejected
    assert record["results"][0]["error"]["code"] == RunErrorCode.node_limit


@pytest.mark.asyncio
async def test_callbacks_observe_work_and_callback_failures_are_isolated(
    context_root: Path, tmp_path: Path
) -> None:
    observed: list[tuple[Any, ...]] = []

    def subcall_start(*args: Any) -> None:
        observed.append(("subcall-start", *args))
        raise RuntimeError("callback failure")

    def subcall_complete(*args: Any) -> None:
        observed.append(("subcall-complete", *args))

    def iteration_start(*args: Any) -> None:
        observed.append(("iteration-start", *args))

    def iteration_complete(*args: Any) -> None:
        observed.append(("iteration-complete", *args))

    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(response=_repl_continue("delegate", [_delegate("leaf", "child")])),
                _Step(response=_final_repl("done")),
            ]
        ],
        leaf_steps=[_Step(response=_final_payload("child result"))],
    )
    result, _ = await RecursiveRunner(
        lambda: adapter,
        on_subcall_start=subcall_start,
        on_subcall_complete=subcall_complete,
        on_iteration_start=iteration_start,
        on_iteration_complete=iteration_complete,
    ).run(
        task="callbacks",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.succeeded
    assert [item[0] for item in observed].count("subcall-start") == 1
    assert [item[0] for item in observed].count("subcall-complete") == 1
    assert [item[0] for item in observed].count("iteration-start") == 2
    assert [item[0] for item in observed].count("iteration-complete") == 2


@pytest.mark.asyncio
async def test_delegate_completion_callback_reports_sanitized_unexpected_error(
    context_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_errors: list[str | None] = []

    def subcall_complete(
        _depth: int,
        _model: str,
        _elapsed_seconds: float,
        error: str | None,
    ) -> None:
        callback_errors.append(error)

    adapter = _FakeAdapter(
        root_scripts=[[_Step(response=_repl_continue("delegate", [_delegate("leaf", "child")]))]]
    )
    runner = RecursiveRunner(lambda: adapter, on_subcall_complete=subcall_complete)
    original_reserve = runner._reserve_node

    async def fail_child_reservation(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("parent") is not None:
            raise RuntimeError("unsafe delegate detail")
        return await original_reserve(*args, **kwargs)

    monkeypatch.setattr(runner, "_reserve_node", fail_child_reservation)

    result, _ = await runner.run(
        task="unexpected delegate failure",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.failed
    assert result.error is not None and result.error.code == RunErrorCode.unexpected
    assert len(callback_errors) == 1
    assert callback_errors[0] is not None
    assert "unsafe delegate detail" not in callback_errors[0]


@pytest.mark.asyncio
async def test_usage_aggregates_cumulative_root_and_fresh_leaf_threads(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_repl_continue("delegate", [_delegate("leaf", "child")]),
                    usage=_usage(5, 5),
                ),
                _Step(response=_final_repl("done"), usage=_usage(4, 9)),
            ]
        ],
        leaf_steps=[_Step(response=_final_payload("child"), usage=_usage(4, 4))],
    )
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="usage",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.usage.available
    assert result.usage.total is not None and result.usage.total.total_tokens == 13
    usage_by_node = {node.node_id: node.usage for node in result.nodes}
    assert usage_by_node["node_000001"].total is not None
    assert usage_by_node["node_000001"].total.total_tokens == 9
    assert usage_by_node["node_000002"].total is not None
    assert usage_by_node["node_000002"].total.total_tokens == 4


@pytest.mark.asyncio
async def test_any_missing_thread_usage_makes_run_aggregate_unavailable(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_repl_continue("delegate", [_delegate("leaf", "child")]),
                    usage=_usage(5, 5),
                ),
                _Step(response=_final_repl("done"), usage=_usage(4, 9)),
            ]
        ],
        leaf_steps=[_Step(response=_final_payload("child"))],
    )
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="missing usage",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.succeeded
    assert not result.usage.available
    root, child = result.nodes
    assert root.usage.available
    assert root.usage.total is not None and root.usage.total.total_tokens == 9
    assert not child.usage.available
    call = _iteration(result, "node_000001", 0)["results"][0]
    assert call["usage"]["available"] is False
    report = {item.name: item for item in result.runtime.limit_report}
    assert report["max_tokens"].observed is None


@pytest.mark.asyncio
async def test_tool_handler_exception_is_failed_call_and_parent_can_recover(
    context_root: Path, tmp_path: Path
) -> None:
    def explode(arguments: dict[str, Any]) -> None:
        del arguments
        raise ValueError("sensitive handler detail")

    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(response=_repl_continue("try tool", [_tool("attempt", "explode")])),
                _Step(response=_final_repl("recovered without tool")),
            ]
        ]
    )
    result, _ = await RecursiveRunner(lambda: adapter, custom_tools={"explode": explode}).run(
        task="recover",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.succeeded
    assert result.payload is not None and result.payload.answer == "recovered without tool"
    call = _iteration(result, "node_000001", 0)["results"][0]
    assert call["status"] == CallStatus.failed
    assert call["error"]["code"] == RunErrorCode.tool_runtime
    assert call["error"]["message"] == "tool handler failed: ValueError"
    assert "sensitive handler detail" not in adapter.sessions[0].turn_requests[1].prompt


@pytest.mark.asyncio
async def test_observed_token_limit_produces_token_limit_result(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        leaf_steps=[_Step(response=_final_payload("too costly"), usage=_usage(6, 6))]
    )
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="bounded tokens",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_tokens=5),
        strategy=RunStrategy.direct,
    )

    assert result.status == RunStatus.failed
    assert result.payload is None
    assert result.error is not None and result.error.code == RunErrorCode.token_limit
    assert result.error.details == {"observed": 6, "configured": 5}
    report = {item.name: item for item in result.runtime.limit_report}
    assert report["max_tokens"].observed == 6


@pytest.mark.asyncio
async def test_invalid_repl_response_reaches_consecutive_error_limit(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(root_scripts=[[_Step(response="not-json")]])
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="strict REPL response",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_errors=1),
    )

    assert result.status == RunStatus.failed
    assert result.error is not None and result.error.code == RunErrorCode.error_limit
    iteration = _iteration(result, "node_000001", 0)
    assert iteration["error"]["code"] == RunErrorCode.invalid_model_output
    assert result.nodes[0].iterations == 1


@pytest.mark.asyncio
async def test_compaction_threshold_requests_session_compaction_and_records_it(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_repl_continue("compact now", [_tool("read", "constant")]),
                    usage=_usage(900, 900),
                ),
                _Step(response=_final_repl("after compaction")),
            ]
        ]
    )
    result, _ = await RecursiveRunner(lambda: adapter, custom_tools={"constant": "value"}).run(
        task="compact",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(compaction=True, compaction_threshold=0.8),
    )

    assert result.status == RunStatus.succeeded
    assert adapter.sessions[0].compactions == 1
    assert _iteration(result, "node_000001", 0)["compaction_completed"] is True
    event_types = [event["type"] for event in _events(result)]
    assert "compaction.requested" in event_types
    assert "compaction.completed" in event_types


@pytest.mark.asyncio
async def test_invalid_session_id_is_rejected_before_session_path_access(
    context_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_path_access(*args: object) -> Path:
        del args
        raise AssertionError("session_path must not receive an invalid identifier")

    monkeypatch.setattr("rcodex.runner.session_path", forbidden_path_access)
    runner = RecursiveRunner(lambda: _FakeAdapter())

    with pytest.raises(ValueError, match="session_id"):
        await runner.run(
            task="resume",
            context=context_root,
            state_directory=tmp_path / "state",
            config=_config(persistent=True),
            session_id="../../outside",
        )


@pytest.mark.asyncio
async def test_verbose_trajectory_is_written_to_stderr_only(
    context_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _FakeAdapter(leaf_steps=[_Step(response=_final_payload("quiet stdout"))])
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="verbose",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(verbose=True),
        strategy=RunStrategy.direct,
    )

    captured = capsys.readouterr()
    assert result.status == RunStatus.succeeded
    assert captured.out == ""
    assert "[rcodex] run.started" in captured.err
    assert "[rcodex] run.completed" in captured.err


@pytest.mark.asyncio
async def test_custom_system_prompt_is_forwarded_as_developer_guidance_only(
    context_root: Path, tmp_path: Path
) -> None:
    guidance = "Use the private domain vocabulary exactly."
    adapter = _FakeAdapter(leaf_steps=[_Step(response=_final_payload("guided"))])
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="guidance",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(custom_system_prompt=guidance),
        strategy=RunStrategy.direct,
    )

    request = adapter.leaf_requests[0]
    assert request.custom_system_prompt == guidance
    assert guidance not in request.prompt
    persisted = RunRequest.model_validate_json(_read_bytes(result.artifacts.request), strict=True)
    assert (
        persisted.custom_system_prompt_sha256
        == hashlib.sha256(guidance.encode("utf-8")).hexdigest()
    )
    assert guidance not in _read_text(result.artifacts.request)


@pytest.mark.asyncio
async def test_openai_compatible_provider_is_forwarded_and_recorded_without_its_key(
    context_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RCODEX_PROVIDER_API_KEY", "secret-provider-key")
    adapter = _FakeAdapter(leaf_steps=[_Step(response=_final_payload("provider result"))])
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="provider",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(
            model="Qwen/Qwen3-Coder",
            provider_base_url="https://api.tokenfactory.nebius.com/v1",
        ),
        strategy=RunStrategy.direct,
    )

    request = adapter.leaf_requests[0]
    assert request.provider_base_url == "https://api.tokenfactory.nebius.com/v1"
    assert result.runtime.provider is not None
    persisted_text = _read_text(result.artifacts.request)
    persisted = RunRequest.model_validate_json(persisted_text, strict=True)
    assert persisted.provider == result.runtime.provider
    assert "secret-provider-key" not in persisted_text


@pytest.mark.asyncio
async def test_provider_without_api_key_uses_unauthenticated_endpoint(
    context_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RCODEX_PROVIDER_API_KEY", raising=False)
    adapter = _FakeAdapter(leaf_steps=[_Step(response=_final_payload("local result"))])

    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="provider",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(
            model="local",
            provider_base_url="http://127.0.0.1:8000/v1",
        ),
        strategy=RunStrategy.direct,
    )

    assert result.status == RunStatus.succeeded
    assert adapter.leaf_requests[0].provider_base_url == "http://127.0.0.1:8000/v1"


@pytest.mark.asyncio
async def test_local_leaf_capacity_timeout_is_a_call_result_parent_can_recover_from(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_repl_continue(
                        "local limit",
                        [_delegate("holder", "slow"), _delegate("waiting", "waits")],
                    )
                ),
                _Step(response=_final_repl("recovered")),
            ]
        ],
        leaf_steps=[
            _Step(response=_final_payload("holder done"), delay=0.05),
            _Step(response=_final_payload("should not start")),
        ],
    )
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="local deadline",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(
            max_concurrency=1,
            node_timeout_seconds=1,
            leaf_timeout_seconds=0.02,
        ),
    )

    assert result.status == RunStatus.succeeded
    record = _iteration(result, "node_000001", 0)
    assert [item["status"] for item in record["results"]] == [
        CallStatus.succeeded,
        CallStatus.timed_out,
    ]
    assert record["results"][1]["error"]["code"] == RunErrorCode.timeout
    assert len(adapter.leaf_requests) == 1


@pytest.mark.asyncio
async def test_parent_deadline_interrupts_wave_and_persists_ordered_trace(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_repl_continue(
                        "parent expires",
                        [_delegate("holder", "slow"), _delegate("waiting", "waits")],
                    )
                )
            ]
        ],
        leaf_steps=[
            _Step(response=_final_payload("too late"), delay=0.1),
            _Step(response=_final_payload("never starts")),
        ],
    )
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="parent deadline",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(
            max_concurrency=1,
            node_timeout_seconds=0.03,
            leaf_timeout_seconds=1,
        ),
    )

    assert result.status == RunStatus.timed_out
    assert result.error is not None and result.error.code == RunErrorCode.timeout
    assert result.best_partial_answer is None
    record = _iteration(result, "node_000001", 0)
    assert record["error"]["code"] == RunErrorCode.timeout
    assert record["executions"] == []


@pytest.mark.asyncio
async def test_failed_finalization_is_persisted_with_prompt_and_error(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(response=_repl_continue("best partial", [_tool("read", "constant")])),
                _Step(response="not-json"),
            ]
        ]
    )
    result, _ = await RecursiveRunner(lambda: adapter, custom_tools={"constant": "value"}).run(
        task="failed finalization",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_iterations=1),
    )

    assert result.status == RunStatus.partial
    assert result.best_partial_answer == "best partial"
    assert result.error is not None
    assert result.error.code == RunErrorCode.invalid_model_output
    record = _iteration(result, "node_000001", 1)
    final_prompt = adapter.sessions[0].turn_requests[1].prompt
    assert record["phase"] == "finalization"
    assert record["prompt_sha256"] == prompt_sha256(final_prompt)
    assert record["error"]["code"] == RunErrorCode.invalid_model_output
    assert result.runtime.prompt_sha256s["finalization"] == prompt_sha256(final_prompt)


@pytest.mark.asyncio
async def test_async_rlm_rejects_overlapping_persistent_completions(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(root_scripts=[[_Step(response=_final_repl("first"), delay=0.08)]])
    async with AsyncRLM(
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(persistent=True),
        adapter_factory=lambda: adapter,
    ) as client:
        active = asyncio.create_task(client.completion("first"))
        await _wait_for_session(adapter)
        with pytest.raises(RuntimeError, match="already active"):
            await client.completion("overlap")
        completion = await active

    assert completion.response == "first"


@pytest.mark.asyncio
async def test_persistent_session_lease_rejects_other_runner(
    context_root: Path, tmp_path: Path
) -> None:
    state_directory = tmp_path / "state"
    config = _config(persistent=True)
    initial_adapter = _FakeAdapter(root_scripts=[[_Step(response=_final_repl("created"))]])
    initial, _ = await RecursiveRunner(lambda: initial_adapter).run(
        task="create session",
        context=context_root,
        state_directory=state_directory,
        config=config,
    )
    session_id = initial.runtime.session_id
    assert session_id is not None

    active_adapter = _FakeAdapter(
        root_scripts=[[_Step(response=_final_repl("active"), delay=0.08)]]
    )
    active = asyncio.create_task(
        RecursiveRunner(lambda: active_adapter).run(
            task="hold lease",
            context=context_root,
            state_directory=state_directory,
            config=config,
            session_id=session_id,
        )
    )
    await _wait_for_session(active_adapter)

    with pytest.raises(ValueError, match="already active"):
        await RecursiveRunner(lambda: _FakeAdapter()).run(
            task="competing run",
            context=context_root,
            state_directory=state_directory,
            config=config,
            session_id=session_id,
        )
    completed, _ = await active
    assert completed.status == RunStatus.succeeded


@pytest.mark.asyncio
async def test_auto_created_persistent_session_is_leased_during_first_run(
    context_root: Path, tmp_path: Path
) -> None:
    state_directory = tmp_path / "state"
    config = _config(persistent=True)
    active_adapter = _FakeAdapter(
        root_scripts=[[_Step(response=_final_repl("created"), delay=0.2)]]
    )
    active = asyncio.create_task(
        RecursiveRunner(lambda: active_adapter).run(
            task="create and hold generated session",
            context=context_root,
            state_directory=state_directory,
            config=config,
        )
    )
    await _wait_for_session(active_adapter)
    records = list((state_directory / "sessions").glob("session_*.json"))
    assert len(records) == 1
    session_id = read_session(records[0]).session_id

    try:
        with pytest.raises(ValueError, match="already active"):
            await RecursiveRunner(lambda: _FakeAdapter()).run(
                task="competing generated session owner",
                context=context_root,
                state_directory=state_directory,
                config=config,
                session_id=session_id,
            )
    finally:
        completed, _ = await active

    assert completed.status == RunStatus.succeeded
    assert completed.runtime.session_id == session_id


@pytest.mark.asyncio
async def test_batch_validation_failures_remain_ordered_siblings(
    context_root: Path, tmp_path: Path
) -> None:
    oversized = "x" * 16_385
    results = await RecursiveRunner(_RoutingDirectAdapter).run_batched(
        ["first", "   ", oversized, "last"],
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_concurrency=2),
        strategy=RunStrategy.direct,
    )

    assert len(results) == 4
    assert isinstance(results[0], RunResult)
    assert isinstance(results[1], BatchFailure)
    assert isinstance(results[2], BatchFailure)
    assert isinstance(results[3], RunResult)
    assert results[1].task_sha256 == hashlib.sha256(b"   ").hexdigest()
    assert results[2].task_preview == oversized[:2000]
    assert results[2].task_sha256 == hashlib.sha256(oversized.encode()).hexdigest()


@pytest.mark.asyncio
async def test_api_normalization_failure_remains_an_ordered_batch_slot(
    context_root: Path, tmp_path: Path
) -> None:
    prompts: list[Any] = ["first", {"invalid": object()}, "last"]
    factory = _FinalRootFactory()
    async with AsyncRLM(
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_concurrency=2),
        adapter_factory=factory,
    ) as client:
        completions = await client.completion_batched(prompts)

    assert len(completions) == 3
    assert isinstance(completions[0].run, RunResult)
    assert completions[0].response == "normalized"
    assert isinstance(completions[1].run, BatchFailure)
    assert completions[1].response.startswith("Error: ")
    assert isinstance(completions[2].run, RunResult)
    assert completions[2].response == "normalized"
    assert len(factory.adapters) == 2


@pytest.mark.asyncio
async def test_repl_final_payload_obeys_final_result_limit(
    context_root: Path, tmp_path: Path
) -> None:
    raw = _final_repl("a" * 1500)
    assert len(raw.encode()) > 1024
    assert len(raw.encode()) < 4096
    adapter = _FakeAdapter(root_scripts=[[_Step(response=raw)]])
    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="raw final limit",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(
            max_repl_code_bytes=4096,
            max_final_result_bytes=1024,
            max_errors=1,
        ),
    )

    assert result.status == RunStatus.failed
    assert result.error is not None and result.error.code == RunErrorCode.error_limit
    iteration = _iteration(result, "node_000001", 0)
    assert iteration["error"]["code"] == RunErrorCode.oversized_model_output
    assert iteration["error"]["details"]["max_bytes"] == 1024
    assert iteration["error"]["details"]["observed_bytes"] > 1024


@pytest.mark.asyncio
async def test_repl_adapter_error_writes_terminal_iteration_and_balances_callback(
    context_root: Path, tmp_path: Path
) -> None:
    starts: list[tuple[int, int]] = []
    completes: list[tuple[int, int]] = []
    adapter = _FakeAdapter(
        root_scripts=[[_Step(error=AdapterRuntimeError("turn_run", "RuntimeError"))]]
    )
    result, _ = await RecursiveRunner(
        lambda: adapter,
        on_iteration_start=lambda depth, iteration: starts.append((depth, iteration)),
        on_iteration_complete=lambda depth, iteration, _duration: completes.append(
            (depth, iteration)
        ),
    ).run(
        task="adapter failure",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.failed
    assert result.error is not None and result.error.code == RunErrorCode.sdk_runtime
    iteration = _iteration(result, "node_000001", 0)
    assert iteration["error"]["code"] == RunErrorCode.sdk_runtime
    assert starts == [(0, 0)]
    assert completes == [(0, 0)]


@pytest.mark.asyncio
async def test_repl_cancellation_writes_terminal_iteration_and_balances_callback(
    context_root: Path, tmp_path: Path
) -> None:
    state_directory = tmp_path / "state"
    starts: list[tuple[int, int]] = []
    completes: list[tuple[int, int]] = []
    adapter = _FakeAdapter(root_scripts=[[_Step(response=_final_repl("never"), delay=10)]])
    active = asyncio.create_task(
        RecursiveRunner(
            lambda: adapter,
            on_iteration_start=lambda depth, iteration: starts.append((depth, iteration)),
            on_iteration_complete=lambda depth, iteration, _duration: completes.append(
                (depth, iteration)
            ),
        ).run(
            task="cancel REPL turn",
            context=context_root,
            state_directory=state_directory,
            config=_config(),
        )
    )
    await _wait_for_session(adapter)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active

    persisted = _only_persisted_result(state_directory)
    assert persisted.status == RunStatus.cancelled
    iteration = _iteration(persisted, "node_000001", 0)
    assert iteration["error"]["code"] == RunErrorCode.cancelled
    assert starts == [(0, 0)]
    assert completes == [(0, 0)]


@pytest.mark.asyncio
async def test_repl_token_limit_writes_terminal_iteration_and_balances_callback(
    context_root: Path, tmp_path: Path
) -> None:
    starts: list[tuple[int, int]] = []
    completes: list[tuple[int, int]] = []
    adapter = _FakeAdapter(
        root_scripts=[[_Step(response=_final_repl("over budget"), usage=_usage(6, 6))]]
    )
    result, _ = await RecursiveRunner(
        lambda: adapter,
        on_iteration_start=lambda depth, iteration: starts.append((depth, iteration)),
        on_iteration_complete=lambda depth, iteration, _duration: completes.append(
            (depth, iteration)
        ),
    ).run(
        task="token REPL turn",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_tokens=5),
    )

    assert result.status == RunStatus.failed
    assert result.error is not None and result.error.code == RunErrorCode.token_limit
    iteration = _iteration(result, "node_000001", 0)
    assert iteration["error"]["code"] == RunErrorCode.token_limit
    assert starts == [(0, 0)]
    assert completes == [(0, 0)]


@pytest.mark.asyncio
async def test_recursive_cleanup_failure_surfaces_and_invalidates_resume(
    context_root: Path, tmp_path: Path
) -> None:
    state_directory = tmp_path / "state"
    adapter = _FakeAdapter(
        root_scripts=[
            [_Step(response=_final_repl("usable answer"))],
            [_Step(response=_final_repl("fresh thread"))],
        ],
        root_close_errors=[AdapterCleanupError("cleanup failed"), None],
    )
    runner = RecursiveRunner(lambda: adapter)
    first, _ = await runner.run(
        task="cleanup",
        context=context_root,
        state_directory=state_directory,
        config=_config(persistent=True),
    )

    assert first.status == RunStatus.failed
    assert first.payload is None
    assert first.error is not None and first.error.code == RunErrorCode.cleanup
    session_id = first.runtime.session_id
    assert session_id is not None
    persisted = read_session(session_path(state_directory.resolve(), session_id))
    assert persisted.root_thread_id is None

    second, _ = await runner.run(
        task="after cleanup",
        context=context_root,
        state_directory=state_directory,
        config=_config(persistent=True),
        session_id=session_id,
    )
    assert second.status == RunStatus.succeeded
    assert [request.thread_id for request in adapter.start_requests] == [None, None]


@pytest.mark.asyncio
async def test_child_leaf_cleanup_failure_aborts_the_run(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(response=_repl_continue("delegating leaf", [_delegate("leaf", "child")])),
                _Step(response=_final_repl("must not run")),
            ]
        ],
        leaf_steps=[_Step(error=AdapterCleanupError("leaf cleanup failed"))],
    )

    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="leaf cleanup propagation",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(),
    )

    assert result.status == RunStatus.failed
    assert result.error is not None and result.error.code == RunErrorCode.cleanup
    assert len(adapter.sessions[0].turn_requests) == 1


@pytest.mark.asyncio
async def test_child_recursive_cleanup_failure_aborts_the_run(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_repl_continue(
                        "delegating recursive child",
                        [_delegate("child", "recursive child", "recursive")],
                    )
                ),
                _Step(response=_final_repl("must not run")),
            ],
            [_Step(response=_final_repl("child answer"))],
        ],
        root_close_errors=[None, AdapterCleanupError("child cleanup failed")],
    )

    result, _ = await RecursiveRunner(lambda: adapter).run(
        task="recursive cleanup propagation",
        context=context_root,
        state_directory=tmp_path / "state",
        config=_config(max_depth=2),
    )

    assert result.status == RunStatus.failed
    assert result.error is not None and result.error.code == RunErrorCode.cleanup
    assert len(adapter.sessions) == 2
    assert len(adapter.sessions[0].turn_requests) == 1
    assert adapter.sessions[1].closed


@pytest.mark.asyncio
async def test_context_integrity_failure_clears_persistent_resume_thread(
    context_root: Path, tmp_path: Path
) -> None:
    state_directory = tmp_path / "state"
    notes = context_root / "notes.txt"
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_final_repl("stale"),
                    action=lambda: _replace_text(notes, "changed context\n"),
                )
            ],
            [_Step(response=_final_repl("fresh"))],
        ]
    )
    runner = RecursiveRunner(lambda: adapter)
    first, _ = await runner.run(
        task="integrity",
        context=context_root,
        state_directory=state_directory,
        config=_config(persistent=True),
    )

    assert first.status == RunStatus.failed
    assert first.error is not None and first.error.code == RunErrorCode.context_integrity
    session_id = first.runtime.session_id
    assert session_id is not None
    persisted = read_session(session_path(state_directory.resolve(), session_id))
    assert persisted.root_thread_id is None

    second, _ = await runner.run(
        task="new context version",
        context=context_root,
        state_directory=state_directory,
        config=_config(persistent=True),
        session_id=session_id,
    )
    assert second.status == RunStatus.succeeded
    assert [request.thread_id for request in adapter.start_requests] == [None, None]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
async def test_controller_state_artifacts_and_lease_are_private(
    context_root: Path, tmp_path: Path
) -> None:
    state_directory = tmp_path / "state"
    adapter = _FakeAdapter(
        root_scripts=[
            [_Step(response=_final_repl("private"))],
            [_Step(response=_final_repl("leased"))],
        ]
    )
    runner = RecursiveRunner(lambda: adapter)
    result, _ = await runner.run(
        task="private state",
        context=context_root,
        state_directory=state_directory,
        config=_config(persistent=True),
    )

    assert result.status == RunStatus.succeeded
    session_id = result.runtime.session_id
    assert session_id is not None
    resumed, _ = await runner.run(
        task="create private lease",
        context=context_root,
        state_directory=state_directory,
        config=_config(persistent=True),
        session_id=session_id,
    )
    assert resumed.status == RunStatus.succeeded
    directories, files = _private_modes(state_directory)
    assert directories
    assert files
    assert all(mode == 0o700 for mode in directories.values()), directories
    assert all(mode == 0o600 for mode in files.values()), files
    assert any(name.endswith(".lock") for name in files)


@pytest.mark.asyncio
async def test_single_concurrency_allows_one_recursive_session_per_depth(
    context_root: Path, tmp_path: Path
) -> None:
    adapter = _FakeAdapter(
        root_scripts=[
            [
                _Step(
                    response=_repl_continue(
                        "recursive child",
                        [_delegate("child", "solve child", "recursive")],
                    )
                ),
                _Step(response=_final_repl("root")),
            ],
            [_Step(response=_final_repl("child"))],
        ]
    )
    result, _ = await asyncio.wait_for(
        RecursiveRunner(lambda: adapter).run(
            task="depth quotas",
            context=context_root,
            state_directory=tmp_path / "state",
            config=_config(max_concurrency=1, max_depth=2),
        ),
        timeout=1,
    )

    assert result.status == RunStatus.succeeded
    assert len(adapter.sessions) == 2
    assert [(node.depth, node.executed_mode) for node in result.nodes] == [
        (0, CallMode.recursive),
        (1, CallMode.recursive),
    ]
