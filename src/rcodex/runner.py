"""Codex-native direct and recursive inference engine."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib
import json
import os
import platform
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, Literal, cast

from openai_codex import __version__ as OPENAI_CODEX_VERSION
from pydantic import ValidationError

from rcodex._version import __version__ as RCODEX_VERSION
from rcodex.codex_adapter import (
    AdapterAuthenticationError,
    AdapterCleanupError,
    AdapterError,
    AdapterRuntimeError,
    AdapterTimeoutError,
    DirectTurn,
    DirectTurnRequest,
    RecursiveCodexAdapter,
    RootSession,
    RootSessionRequest,
    RootTurnRequest,
    SdkCodexAdapter,
    adapter_error_code,
)
from rcodex.config import RunConfig
from rcodex.context import (
    EvidenceValidationError,
    ManifestDeadlineError,
    ManifestError,
    build_manifest,
    canonical_json_bytes,
    integrity_changes,
    manifest_sha256,
    validate_evidence,
)
from rcodex.models import (
    BatchFailure,
    CallMode,
    CallResult,
    CallStatus,
    ContextManifest,
    DelegateRequest,
    FinalPayload,
    IterationRecord,
    NodeRecord,
    NodeStatus,
    ReplExecutionRecord,
    RequestedCapabilities,
    RetryClassification,
    RunError,
    RunErrorCode,
    RunRequest,
    RunResult,
    RunStatus,
    RunStrategy,
    RuntimeMetadata,
    RunUsage,
    SessionHistoryEntry,
    SessionRecord,
    TokenUsageBreakdown,
    ToolRequest,
)
from rcodex.prompts import (
    PROMPT_VERSION,
    build_direct_prompt,
    build_feedback_prompt,
    build_finalize_prompt,
    build_node_prompt,
    build_repair_prompt,
    prompt_sha256,
)
from rcodex.repl import ReplError, ReplExecution, ReplSession, ReplTimeoutError
from rcodex.repl_context import ContextReader
from rcodex.run_inputs import (
    InvocationError,
    resolve_run_paths,
    validate_task,
    warn_ambient_mcp_risk,
)
from rcodex.storage import (
    RunStore,
    StorageError,
    atomic_write_json,
    ensure_private_directory,
    read_session,
    session_path,
)
from rcodex.tools import ToolError, ToolInputError, ToolRegistry

AdapterFactory = Callable[[], RecursiveCodexAdapter]
SubcallStart = Callable[[int, str, str], None]
SubcallComplete = Callable[[int, str, float, str | None], None]
IterationStart = Callable[[int, int], None]
IterationComplete = Callable[[int, int, float], None]
_REPL_BLOCK_PATTERN = re.compile(
    r"```(?:repl|python)[ \t]*\r?\n(?P<code>.*?)```",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class _BoundaryError(Exception):
    code: RunErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _NodeFailure(Exception):
    error: RunError
    best_partial: str | None = None


@dataclass(frozen=True, slots=True)
class _RunLimit(Exception):
    error: RunError
    best_partial: str | None = None


@dataclass(frozen=True, slots=True)
class _WaveInterrupted(BaseException):
    cause: BaseException
    results: list[CallResult]


class _ContextLimitError(ValueError):
    """Admission would exceed the run-wide derived-context budget."""


@dataclass(slots=True)
class _SessionLease:
    handle: Any
    unlock: Callable[[], None]

    @classmethod
    def acquire(cls, path: Path) -> _SessionLease:
        descriptor: int | None = None
        handle: Any | None = None
        try:
            ensure_private_directory(path.parent)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            handle = os.fdopen(descriptor, "a+b")
            descriptor = None
            if os.name != "nt":
                os.chmod(path, 0o600)
        except OSError as exc:
            if handle is not None:
                handle.close()
            elif descriptor is not None:
                os.close(descriptor)
            raise StorageError("could not open persistent session lease") from exc
        assert handle is not None
        try:
            if os.name == "nt":
                locking = importlib.import_module("msvcrt")
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                locking.locking(handle.fileno(), locking.LK_NBLCK, 1)

                def unlock() -> None:
                    handle.seek(0)
                    locking.locking(handle.fileno(), locking.LK_UNLCK, 1)

            else:
                locking = importlib.import_module("fcntl")
                locking.flock(handle.fileno(), locking.LOCK_EX | locking.LOCK_NB)

                def unlock() -> None:
                    locking.flock(handle.fileno(), locking.LOCK_UN)

        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ValueError("persistent session is already active") from exc
            raise StorageError("could not acquire persistent session lease") from exc
        return cls(handle=handle, unlock=unlock)

    def release(self) -> None:
        try:
            self.unlock()
        except OSError:
            pass
        finally:
            try:
                self.handle.close()
            except OSError:
                pass


@dataclass(slots=True)
class _MutableNode:
    node_id: str
    parent_node_id: str | None
    parent_call_id: str | None
    depth: int
    requested_mode: CallMode
    executed_mode: CallMode
    task: str
    model: str | None
    reasoning_effort: str
    started_at: datetime
    started_monotonic: float
    deadline: float
    manifest: ContextManifest
    manifest_path: Path
    manifest_hash: str
    status: NodeStatus = NodeStatus.running
    thread_id: str | None = None
    initial_prompt_sha256: str | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    iterations: int = 0
    calls: int = 0
    payload: FinalPayload | None = None
    best_partial: str | None = None
    error: RunError | None = None


def _unavailable_usage() -> RunUsage:
    return RunUsage(available=False)


def _zero_breakdown() -> TokenUsageBreakdown:
    return TokenUsageBreakdown(
        input_tokens=0,
        cached_input_tokens=0,
        cache_write_input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        total_tokens=0,
    )


def _subtract_usage(
    current: TokenUsageBreakdown, previous: TokenUsageBreakdown | None
) -> TokenUsageBreakdown:
    old = previous or _zero_breakdown()

    def delta(name: str) -> int:
        return max(0, int(getattr(current, name) or 0) - int(getattr(old, name) or 0))

    cache_write = current.cache_write_input_tokens
    old_cache_write = old.cache_write_input_tokens
    return TokenUsageBreakdown(
        input_tokens=delta("input_tokens"),
        cached_input_tokens=delta("cached_input_tokens"),
        cache_write_input_tokens=(
            max(0, cache_write - (old_cache_write or 0)) if cache_write is not None else None
        ),
        output_tokens=delta("output_tokens"),
        reasoning_output_tokens=delta("reasoning_output_tokens"),
        total_tokens=delta("total_tokens"),
    )


def _sum_breakdowns(values: Sequence[TokenUsageBreakdown]) -> TokenUsageBreakdown:
    return TokenUsageBreakdown(
        input_tokens=sum(item.input_tokens for item in values),
        cached_input_tokens=sum(item.cached_input_tokens for item in values),
        cache_write_input_tokens=(
            sum(item.cache_write_input_tokens or 0 for item in values)
            if any(item.cache_write_input_tokens is not None for item in values)
            else None
        ),
        output_tokens=sum(item.output_tokens for item in values),
        reasoning_output_tokens=sum(item.reasoning_output_tokens for item in values),
        total_tokens=sum(item.total_tokens for item in values),
    )


@dataclass(slots=True)
class _UsageLedger:
    durable_baselines: dict[str, TokenUsageBreakdown]
    durable_threads: set[str] = field(default_factory=set)
    run_baselines: dict[str, TokenUsageBreakdown | None] = field(default_factory=dict)
    latest: dict[str, RunUsage] = field(default_factory=dict)
    missing_threads: set[str] = field(default_factory=set)
    last: TokenUsageBreakdown | None = None
    last_window: int | None = None

    def mark_durable(self, thread_id: str) -> None:
        self.durable_threads.add(thread_id)

    def record(self, turn: DirectTurn) -> None:
        usage = turn.usage
        if not usage.available or usage.total is None or usage.last is None:
            self.missing_threads.add(turn.thread_id)
            return
        if turn.thread_id not in self.run_baselines:
            self.run_baselines[turn.thread_id] = (
                self.durable_baselines.get(turn.thread_id)
                if turn.thread_id in self.durable_threads
                else None
            )
        self.missing_threads.discard(turn.thread_id)
        self.latest[turn.thread_id] = usage
        self.last = usage.last
        self.last_window = usage.model_context_window
        if turn.thread_id in self.durable_threads:
            self.durable_baselines[turn.thread_id] = usage.total

    def for_thread(self, thread_id: str | None) -> RunUsage:
        if thread_id is None or thread_id in self.missing_threads:
            return _unavailable_usage()
        usage = self.latest.get(thread_id)
        if usage is None or usage.total is None or usage.last is None:
            return _unavailable_usage()
        total = _subtract_usage(usage.total, self.run_baselines.get(thread_id))
        return RunUsage(
            available=True,
            last=usage.last,
            total=total,
            model_context_window=usage.model_context_window,
        )

    def aggregate(self) -> RunUsage:
        if self.missing_threads:
            return _unavailable_usage()
        return self.known_aggregate()

    def known_aggregate(self) -> RunUsage:
        totals: list[TokenUsageBreakdown] = []
        for thread_id, usage in self.latest.items():
            if usage.total is not None:
                totals.append(_subtract_usage(usage.total, self.run_baselines.get(thread_id)))
        if not totals or self.last is None:
            return _unavailable_usage()
        return RunUsage(
            available=True,
            last=self.last,
            total=_sum_breakdowns(totals),
            model_context_window=self.last_window,
        )


@dataclass(slots=True)
class _RunState:
    run_id: str
    task: str
    root_prompt: str | None
    strategy: RunStrategy
    context_root: Path
    state_root: Path
    output_path: Path | None
    config: RunConfig
    store: RunStore
    adapter: RecursiveCodexAdapter
    tools: ToolRegistry
    sub_tools: ToolRegistry
    started_at: datetime
    started_monotonic: float
    deadline: float
    runtime: RuntimeMetadata
    usage: _UsageLedger
    semaphore: asyncio.Semaphore
    tool_semaphore: asyncio.Semaphore
    session_semaphores: list[asyncio.Semaphore]
    reservation_lock: asyncio.Lock
    manifest: ContextManifest | None = None
    child_context_bytes: int = 0
    nodes: dict[str, _MutableNode] = field(default_factory=dict)
    node_sequence: int = 0
    persistent_session_safe: bool = True
    codex_runtime_version: str | None = None
    root_thread_id: str | None = None
    manifest_written: bool = False
    nodes_written: bool = False
    iterations_written: bool = False
    prompt_hashes: dict[str, str] = field(default_factory=dict)
    session: SessionRecord | None = None
    session_file: Path | None = None

    def remaining(self, *, deadline: float | None = None) -> float:
        boundary = min(self.deadline, deadline) if deadline is not None else self.deadline
        remaining = boundary - time.monotonic()
        if remaining <= 0:
            raise _RunLimit(
                RunError(code=RunErrorCode.timeout, message="run deadline expired"),
                self.best_partial(),
            )
        return remaining

    def best_partial(self) -> str | None:
        root = self.nodes.get("node_000001")
        return root.best_partial if root is not None else None

    def record_prompt(self, phase: str, prompt: str) -> None:
        self.prompt_hashes[phase] = prompt_sha256(prompt)

    def event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        node: _MutableNode | None = None,
        iteration: int | None = None,
        call_id: str | None = None,
    ) -> None:
        self.store.event(
            self.run_id,
            event_type,
            payload,
            node_id=node.node_id if node is not None else None,
            parent_node_id=node.parent_node_id if node is not None else None,
            iteration=iteration,
            call_id=call_id,
        )
        if self.config.verbose:
            location = f" {node.node_id}" if node is not None else ""
            print(f"[rcodex]{location} {event_type}", file=sys.stderr, flush=True)


@asynccontextmanager
async def _deadline_permit(
    state: _RunState,
    semaphore: asyncio.Semaphore,
    deadline: float,
) -> AsyncIterator[None]:
    try:
        await asyncio.wait_for(
            semaphore.acquire(),
            timeout=state.remaining(deadline=deadline),
        )
    except TimeoutError as exc:
        raise _RunLimit(
            RunError(
                code=RunErrorCode.timeout,
                message="controller deadline expired while waiting for execution capacity",
            ),
            state.best_partial(),
        ) from exc
    try:
        yield
    finally:
        semaphore.release()


class RecursiveRunner:
    """Run bounded direct or iterative recursive Codex inference."""

    def __init__(
        self,
        adapter_factory: AdapterFactory = SdkCodexAdapter,
        *,
        custom_tools: Mapping[str, Any] | None = None,
        custom_sub_tools: Mapping[str, Any] | None = None,
        on_subcall_start: SubcallStart | None = None,
        on_subcall_complete: SubcallComplete | None = None,
        on_iteration_start: IterationStart | None = None,
        on_iteration_complete: IterationComplete | None = None,
    ) -> None:
        self._adapter_factory = adapter_factory
        self._tools = ToolRegistry.from_custom_tools(custom_tools)
        self._sub_tools = (
            self._tools
            if custom_sub_tools is None
            else ToolRegistry.from_custom_tools(custom_sub_tools)
        )
        self._on_subcall_start = on_subcall_start
        self._on_subcall_complete = on_subcall_complete
        self._on_iteration_start = on_iteration_start
        self._on_iteration_complete = on_iteration_complete
        self._usage_baselines: dict[str, TokenUsageBreakdown] = {}

    async def run(
        self,
        *,
        task: str,
        context: Path,
        state_directory: Path,
        config: RunConfig,
        strategy: RunStrategy = RunStrategy.recursive,
        root_prompt: str | None = None,
        session_id: str | None = None,
        create_session: bool = False,
        output: Path | None = None,
    ) -> tuple[RunResult, Path | None]:
        (
            normalized_task,
            normalized_root_prompt,
            context_root,
            state_root,
            output_path,
        ) = self._validate_invocation(
            task=task,
            root_prompt=root_prompt,
            context=context,
            state_directory=state_directory,
            output=output,
            config=config,
            strategy=strategy,
            session_id=session_id,
        )
        warn_ambient_mcp_risk()
        lease: _SessionLease | None = None
        if config.persistent:
            if session_id is None:
                session_id = f"session_{uuid.uuid4().hex}"
                create_session = True
            ensure_private_directory(state_root)
            lease = _SessionLease.acquire(state_root / "sessions" / f"{session_id}.lock")
        try:
            state = self._prepare(
                task=normalized_task,
                context_root=context_root,
                state_root=state_root,
                output_path=output_path,
                config=config,
                strategy=strategy,
                root_prompt=normalized_root_prompt,
                session_id=session_id,
                create_session=create_session,
            )
            return await self._run_state(state)
        finally:
            if lease is not None:
                lease.release()

    async def _run_state(self, state: _RunState) -> tuple[RunResult, Path | None]:
        payload: FinalPayload | None = None
        error: RunError | None = None
        status = RunStatus.failed
        best_partial: str | None = None
        try:
            payload = await self._execute(state)
            status = RunStatus.succeeded
        except asyncio.CancelledError:
            error = RunError(code=RunErrorCode.cancelled, message="run cancelled by the user")
            status = RunStatus.cancelled
            best_partial = state.best_partial()
            self._complete_run(state, status, payload, best_partial, error)
            raise
        except _RunLimit as exc:
            error = exc.error
            best_partial = exc.best_partial or state.best_partial()
            status = (
                RunStatus.timed_out
                if error.code == RunErrorCode.timeout
                else RunStatus.partial
                if best_partial is not None
                else RunStatus.failed
            )
        except _NodeFailure as exc:
            error = exc.error
            best_partial = exc.best_partial or state.best_partial()
            status = (
                RunStatus.timed_out
                if error.code == RunErrorCode.timeout
                else RunStatus.partial
                if best_partial is not None
                else RunStatus.failed
            )
        except Exception as exc:
            status, error = self._reduce_failure(exc)
            best_partial = state.best_partial()
        result = self._complete_run(state, status, payload, best_partial, error)
        return result, state.output_path

    async def run_batched(
        self,
        tasks: Sequence[str],
        *,
        context: Path,
        state_directory: Path,
        config: RunConfig,
        strategy: RunStrategy = RunStrategy.recursive,
    ) -> list[RunResult | BatchFailure]:
        if config.persistent:
            raise ValueError("top-level batched runs cannot share one persistent session")
        if isinstance(tasks, (str, bytes)):
            raise ValueError("batched tasks must be a sequence of task strings, not one string")
        if len(tasks) > config.max_batch_size:
            raise ValueError("top-level batch exceeds max_batch_size")
        resolve_run_paths(context, state_directory, None)
        semaphore = asyncio.Semaphore(config.max_concurrency)

        async def one(task: str) -> RunResult | BatchFailure:
            started = time.monotonic()
            async with semaphore:
                try:
                    result, _ = await self.run(
                        task=task,
                        context=context,
                        state_directory=state_directory,
                        config=config,
                        strategy=strategy,
                    )
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return BatchFailure(
                        task_preview=task[:2000],
                        task_sha256=prompt_sha256(task),
                        error=self._error_from_exception(exc),
                        duration_ms=round((time.monotonic() - started) * 1000),
                    )

        scheduled = [asyncio.create_task(one(task)) for task in tasks]
        try:
            return list(await asyncio.gather(*scheduled))
        except BaseException:
            for task in scheduled:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*scheduled, return_exceptions=True)
            raise

    @staticmethod
    def _validate_invocation(
        *,
        task: str,
        root_prompt: str | None,
        context: Path,
        state_directory: Path,
        output: Path | None,
        config: RunConfig,
        strategy: RunStrategy,
        session_id: str | None,
    ) -> tuple[str, str | None, Path, Path, Path | None]:
        normalized = validate_task(task)
        if root_prompt is not None:
            root_prompt = validate_task(root_prompt)
        context_root, state_root, output_path = resolve_run_paths(context, state_directory, output)
        if session_id is not None and not config.persistent:
            raise ValueError("session_id requires persistent=True")
        if session_id is not None and re.fullmatch(r"session_[0-9a-f]{32}", session_id) is None:
            raise ValueError("session_id must be session_ followed by 32 lowercase hex characters")
        if strategy == RunStrategy.direct and config.persistent:
            raise ValueError("persistent sessions require strategy=recursive")
        if config.persistent and config.max_depth == 0:
            raise ValueError("persistent sessions require max_depth of at least 1")
        if root_prompt is not None and (strategy == RunStrategy.direct or config.max_depth == 0):
            raise ValueError("root_prompt requires a retained recursive root")
        return normalized, root_prompt, context_root, state_root, output_path

    def _prepare(
        self,
        *,
        task: str,
        context_root: Path,
        state_root: Path,
        output_path: Path | None,
        config: RunConfig,
        strategy: RunStrategy,
        root_prompt: str | None,
        session_id: str | None,
        create_session: bool,
    ) -> _RunState:
        run_id = f"run_{uuid.uuid4().hex}"
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        deadline = started_monotonic + config.run_timeout_seconds
        limits = config.limits()
        adapter = self._adapter_factory()
        pinned_adapter = type(adapter) is SdkCodexAdapter
        session, persistent_path = self._prepare_session(
            state_root,
            context_root,
            config.persistent,
            session_id,
            create_session,
        )
        if (
            session is not None
            and session.root_thread_id is not None
            and session.root_usage_total is not None
        ):
            self._usage_baselines[session.root_thread_id] = session.root_usage_total
        store = RunStore(state_root, run_id)
        request = RunRequest(
            run_id=run_id,
            task=task,
            root_prompt=root_prompt,
            context_root=str(context_root),
            state_directory=str(state_root),
            strategy=strategy,
            model=config.model,
            sub_model=config.resolved_sub_model,
            allowed_models=list(config.allowed_models),
            provider=config.provider,
            reasoning_effort=config.reasoning_effort,
            sub_reasoning_effort=config.resolved_sub_reasoning_effort,
            persistent=config.persistent,
            session_id=session.session_id if session is not None else None,
            compaction=config.compaction,
            compaction_threshold=config.compaction_threshold,
            orchestrator=config.orchestrator,
            custom_system_prompt_sha256=(
                prompt_sha256(config.custom_system_prompt)
                if config.custom_system_prompt is not None
                else None
            ),
            user_prologue_sha256=(
                prompt_sha256(config.user_prologue) if config.user_prologue is not None else None
            ),
            root_tools_sha256=self._tools.definitions_sha256(),
            sub_tools_sha256=self._sub_tools.definitions_sha256(),
            include=list(config.include),
            exclude=list(config.exclude),
            limits=limits,
        )
        state = _RunState(
            run_id=run_id,
            task=task,
            root_prompt=root_prompt,
            strategy=strategy,
            context_root=context_root,
            state_root=state_root,
            output_path=output_path,
            config=config,
            store=store,
            adapter=adapter,
            tools=self._tools,
            sub_tools=self._sub_tools,
            started_at=started_at,
            started_monotonic=started_monotonic,
            deadline=deadline,
            runtime=RuntimeMetadata(
                rcodex_version=RCODEX_VERSION,
                python_version=platform.python_version(),
                platform=platform.platform(),
                openai_codex_version=OPENAI_CODEX_VERSION,
                root_model=config.model,
                sub_model=config.resolved_sub_model,
                provider=config.provider,
                reasoning_effort=config.reasoning_effort,
                sub_reasoning_effort=config.resolved_sub_reasoning_effort,
                prompt_template_version=PROMPT_VERSION,
                session_id=session.session_id if session is not None else None,
                adapter_kind="pinned-sdk" if pinned_adapter else "custom",
                limits=limits,
                limit_report=config.limit_report(),
                requested_capabilities=RequestedCapabilities() if pinned_adapter else None,
            ),
            usage=_UsageLedger(self._usage_baselines),
            semaphore=asyncio.Semaphore(config.max_concurrency),
            tool_semaphore=asyncio.Semaphore(config.max_concurrency),
            session_semaphores=[
                asyncio.Semaphore(1 if depth == 0 else config.max_concurrency)
                for depth in range(max(1, config.max_depth))
            ],
            reservation_lock=asyncio.Lock(),
            session=session,
            session_file=persistent_path,
        )
        if session is not None and session.root_thread_id is not None:
            state.usage.mark_durable(session.root_thread_id)
        store.write(store.request_path, request)
        state.event("run.started", {"strategy": strategy.value})
        return state

    @staticmethod
    def _prepare_session(
        state_root: Path,
        context_root: Path,
        persistent: bool,
        requested_id: str | None,
        create_requested: bool,
    ) -> tuple[SessionRecord | None, Path | None]:
        if not persistent:
            return None, None
        identifier = requested_id or f"session_{uuid.uuid4().hex}"
        path = session_path(state_root, identifier)
        if path.exists():
            session = read_session(path)
            if session.session_id != identifier:
                raise ValueError("persistent session record ID does not match its filename")
            if Path(session.context_root) != context_root:
                raise ValueError("persistent session context does not match this invocation")
            return session, path
        if requested_id is not None and not create_requested:
            raise ValueError("persistent session does not exist")
        now = datetime.now(UTC)
        session = SessionRecord(
            session_id=identifier,
            context_root=str(context_root),
            context_count=0,
            created_at=now,
            updated_at=now,
        )
        atomic_write_json(path, session)
        return session, path

    async def _execute(self, state: _RunState) -> FinalPayload:
        manifest = await self._scan_manifest(state)
        state.manifest = manifest
        manifest_hash = manifest_sha256(manifest)
        state.store.write(state.store.manifest_path, manifest)
        state.manifest_written = True
        state.runtime = state.runtime.model_copy(update={"manifest_sha256": manifest_hash})
        state.event(
            "manifest.created",
            {"entries": len(manifest.entries), "sha256": manifest_hash},
        )
        root_mode = CallMode.leaf if state.strategy == RunStrategy.direct else CallMode.recursive
        if state.strategy == RunStrategy.recursive and state.config.max_depth == 0:
            root_mode = CallMode.leaf
        root = await self._reserve_node(
            state,
            parent=None,
            canonical_call_id=None,
            depth=0,
            requested_mode=(
                CallMode.recursive if state.strategy == RunStrategy.recursive else CallMode.leaf
            ),
            executed_mode=root_mode,
            task=state.task,
            model=state.config.model,
            effort=state.config.reasoning_effort,
        )
        assert root is not None
        if root_mode == CallMode.recursive:
            payload = await self._run_repl_session_node(
                state,
                root,
                root_persistent=state.config.persistent,
            )
        else:
            payload = await self._run_leaf_node(state, root)
        await self._verify_integrity(state, manifest)
        return payload

    @staticmethod
    async def _scan_manifest(
        state: _RunState, *, context_root: Path | None = None, config: RunConfig | None = None
    ) -> ContextManifest:
        cancel_event = Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                build_manifest,
                context_root if context_root is not None else state.context_root,
                config if config is not None else state.config,
                deadline=state.deadline,
                cancel_event=cancel_event,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_event.set()

            def consume_result(completed: asyncio.Task[ContextManifest]) -> None:
                try:
                    completed.result()
                except BaseException:
                    pass

            worker.add_done_callback(consume_result)
            raise

    async def _reserve_node(
        self,
        state: _RunState,
        *,
        parent: _MutableNode | None,
        canonical_call_id: str | None,
        depth: int,
        requested_mode: CallMode,
        executed_mode: CallMode,
        task: str,
        model: str | None,
        effort: str,
        context: str | None = None,
    ) -> _MutableNode | None:
        async with state.reservation_lock:
            if state.node_sequence >= state.config.max_total_nodes:
                return None
            state.remaining(deadline=parent.deadline if parent is not None else None)
            node_id = f"node_{state.node_sequence + 1:06d}"
            assert state.manifest is not None
            manifest = parent.manifest if parent is not None else state.manifest
            manifest_path = (
                parent.manifest_path if parent is not None else state.store.manifest_path
            )
            manifest_hash = (
                parent.manifest_hash if parent is not None else manifest_sha256(manifest)
            )
            if context is not None:
                size = len(context.encode("utf-8"))
                if size > state.config.max_query_context_bytes:
                    raise _ContextLimitError("context exceeds max_query_context_bytes")
                if state.child_context_bytes + size > state.config.max_total_child_context_bytes:
                    raise _ContextLimitError("context exceeds max_total_child_context_bytes")
                manifest, manifest_path = state.store.write_child_context(node_id, context)
                manifest_hash = manifest_sha256(manifest)
                state.child_context_bytes += size
            state.node_sequence += 1
            timeout = (
                state.config.leaf_timeout_seconds
                if executed_mode == CallMode.leaf
                else state.config.node_timeout_seconds
            )
            node = _MutableNode(
                node_id=node_id,
                parent_node_id=parent.node_id if parent is not None else None,
                parent_call_id=canonical_call_id,
                depth=depth,
                requested_mode=requested_mode,
                executed_mode=executed_mode,
                task=task,
                model=model,
                reasoning_effort=effort,
                manifest=manifest,
                manifest_path=manifest_path,
                manifest_hash=manifest_hash,
                started_at=datetime.now(UTC),
                started_monotonic=time.monotonic(),
                deadline=min(
                    state.deadline,
                    parent.deadline if parent is not None else state.deadline,
                    time.monotonic() + timeout,
                ),
            )
            state.nodes[node_id] = node
        state.event(
            "node.admitted",
            {
                "depth": depth,
                "requested_mode": requested_mode.value,
                "executed_mode": executed_mode.value,
            },
            node=node,
            call_id=canonical_call_id,
        )
        self._write_node(state, node)
        return node

    async def _run_leaf_node(self, state: _RunState, node: _MutableNode) -> FinalPayload:
        context_root = Path(node.manifest.context_root)
        prompt = build_direct_prompt(
            node.task,
            context_root,
            node.manifest_path,
            user_prologue=state.config.user_prologue,
        )
        node.initial_prompt_sha256 = prompt_sha256(prompt)
        self._write_node(state, node)
        state.record_prompt("leaf", prompt)
        state.event("node.turn_started", {"phase": "leaf"}, node=node)
        try:
            async with _deadline_permit(state, state.semaphore, node.deadline):
                turn = await state.adapter.run_leaf(
                    DirectTurnRequest(
                        prompt=prompt,
                        context_root=context_root,
                        model=node.model,
                        reasoning_effort=node.reasoning_effort,
                        output_schema=FinalPayload.model_json_schema(),
                        timeout_seconds=state.remaining(deadline=node.deadline),
                        cleanup_timeout_seconds=state.config.cleanup_timeout_seconds,
                        node_id=node.node_id,
                        custom_system_prompt=state.config.custom_system_prompt,
                        provider_base_url=state.config.provider_base_url,
                    )
                )
            self._record_turn(state, node, turn)
            payload = self._parse_final(
                turn.final_response, node.manifest, state.config.max_final_result_bytes
            )
            self._complete_node(state, node, NodeStatus.succeeded, payload=payload)
            return payload
        except asyncio.CancelledError:
            self._complete_node(
                state,
                node,
                NodeStatus.cancelled,
                error=RunError(code=RunErrorCode.cancelled, message="node cancelled"),
            )
            raise
        except _RunLimit as exc:
            status = (
                NodeStatus.timed_out
                if exc.error.code == RunErrorCode.timeout
                else NodeStatus.partial
                if node.best_partial is not None
                else NodeStatus.failed
            )
            self._complete_node(state, node, status, error=exc.error)
            raise
        except Exception as exc:
            error = self._error_from_exception(exc)
            status = (
                NodeStatus.timed_out if error.code == RunErrorCode.timeout else NodeStatus.failed
            )
            self._complete_node(state, node, status, error=error)
            raise _NodeFailure(error) from exc

    async def _run_repl_session_node(
        self, state: _RunState, node: _MutableNode, *, root_persistent: bool
    ) -> FinalPayload:
        """Run one retained Codex thread with one persistent Python namespace."""

        context_root = Path(node.manifest.context_root)
        context_reader = ContextReader(node.manifest)
        session_permit = state.session_semaphores[node.depth]
        permit_acquired = False
        cleanup_succeeded = True
        successful_payload: FinalPayload | None = None
        session: RootSession | None = None
        repl: ReplSession | None = None
        resume_id = (
            state.session.root_thread_id if root_persistent and state.session is not None else None
        )
        try:
            try:
                await asyncio.wait_for(
                    session_permit.acquire(),
                    timeout=state.remaining(deadline=node.deadline),
                )
                permit_acquired = True
            except TimeoutError as exc:
                raise _RunLimit(
                    RunError(
                        code=RunErrorCode.timeout,
                        message=(
                            "controller deadline expired while waiting for retained-session "
                            "capacity"
                        ),
                    ),
                    node.best_partial,
                ) from exc

            async with _deadline_permit(state, state.semaphore, node.deadline):
                session = await state.adapter.start_root(
                    RootSessionRequest(
                        context_root=context_root,
                        model=node.model,
                        reasoning_effort=node.reasoning_effort,
                        timeout_seconds=state.remaining(deadline=node.deadline),
                        cleanup_timeout_seconds=state.config.cleanup_timeout_seconds,
                        persistent=root_persistent,
                        thread_id=resume_id,
                        custom_system_prompt=state.config.custom_system_prompt,
                        provider_base_url=state.config.provider_base_url,
                    )
                )
            node.thread_id = session.thread_id
            state.codex_runtime_version = session.codex_runtime_version
            if node.depth == 0:
                state.root_thread_id = session.thread_id
                if root_persistent:
                    state.usage.mark_durable(session.thread_id)
                self._persist_session_thread(state, session.thread_id)

            tools = state.tools if node.depth == 0 else state.sub_tools

            async def read_context(arguments: dict[str, Any]) -> dict[str, Any]:
                cancel_event = Event()
                try:
                    async with _deadline_permit(state, state.tool_semaphore, node.deadline):
                        value = await asyncio.to_thread(
                            context_reader.request,
                            arguments,
                            deadline=min(state.deadline, node.deadline),
                            cancel_event=cancel_event,
                        )
                    state.remaining(deadline=node.deadline)
                    metadata = (
                        {key: value[key] for key in ("context_entry_id", "offset", "next_offset")}
                        if "context_entry_id" in value
                        else {
                            "entry_count": len(value["entries"]),
                            "next_offset": value["next_offset"],
                        }
                    )
                    state.event("context.accessed", metadata, node=node)
                    return value
                finally:
                    cancel_event.set()

            repl = await ReplSession.start(
                context_handler=read_context,
                tool_names=[definition["name"] for definition in tools.definitions()],
                max_output_bytes=state.config.max_repl_output_bytes,
                max_query_context_bytes=state.config.max_query_context_bytes,
                max_message_bytes=2
                * (
                    state.config.max_repl_code_bytes
                    + state.config.max_repl_output_bytes * 2
                    + state.config.max_final_result_bytes
                ),
                memory_bytes=state.config.repl_memory_bytes,
                cpu_seconds=state.config.repl_cpu_seconds,
                timeout_seconds=state.remaining(deadline=node.deadline),
            )
            context_version = (
                state.session.context_count
                if root_persistent and node.depth == 0 and state.session is not None
                else None
            )
            prompt = build_node_prompt(
                node.task,
                context_root,
                node.manifest_path,
                node_id=node.node_id,
                depth=node.depth,
                max_depth=state.config.max_depth,
                max_iterations=state.config.max_iterations,
                max_calls_per_iteration=state.config.max_calls_per_iteration,
                max_query_context_bytes=state.config.max_query_context_bytes,
                max_total_child_context_bytes=state.config.max_total_child_context_bytes,
                tools=tools.definitions(),
                user_prologue=state.config.user_prologue,
                orchestrator=state.config.orchestrator,
                root_prompt=state.root_prompt if node.depth == 0 else None,
                context_version=context_version,
            )
            node.initial_prompt_sha256 = prompt_sha256(prompt)
            self._write_node(state, node)
            state.record_prompt("node_initial", prompt)
            consecutive_errors = 0

            for iteration in range(state.config.max_iterations):
                self._callback(self._on_iteration_start, node.depth, iteration)
                started_at = datetime.now(UTC)
                started = time.monotonic()
                state.event("iteration.started", node=node, iteration=iteration)
                turn: DirectTurn | None = None
                records: list[ReplExecutionRecord] = []
                results: list[CallResult] = []
                try:
                    async with _deadline_permit(state, state.semaphore, node.deadline):
                        turn = await session.turn(
                            RootTurnRequest(
                                prompt=prompt,
                                output_schema=None,
                                timeout_seconds=state.remaining(deadline=node.deadline),
                                cleanup_timeout_seconds=state.config.cleanup_timeout_seconds,
                                phase=f"node_{node.node_id}_{iteration}",
                            )
                        )
                    self._record_turn(state, node, turn)
                    blocks = self._parse_repl_blocks(
                        turn.final_response,
                        state.config.max_repl_code_bytes,
                    )
                    records, results, final_payload = await self._execute_repl_blocks(
                        state,
                        node,
                        repl,
                        blocks,
                        allow_calls=True,
                    )
                except _BoundaryError as exc:
                    consecutive_errors += 1
                    error = RunError(code=exc.code, message=exc.message, details=exc.details)
                    self._write_repl_iteration(
                        state,
                        node,
                        iteration,
                        "repl",
                        started_at,
                        started,
                        turn,
                        prompt=prompt,
                        executions=records,
                        results=results,
                        error=error,
                    )
                    node.iterations += 1
                    self._callback(
                        self._on_iteration_complete,
                        node.depth,
                        iteration,
                        time.monotonic() - started,
                    )
                    if (
                        state.config.max_errors is not None
                        and consecutive_errors >= state.config.max_errors
                    ):
                        raise _NodeFailure(
                            RunError(
                                code=RunErrorCode.error_limit,
                                message="node exceeded consecutive error limit",
                            ),
                            node.best_partial,
                        ) from exc
                    remaining_errors = (
                        None
                        if state.config.max_errors is None
                        else state.config.max_errors - consecutive_errors
                    )
                    prompt = build_repair_prompt(
                        {"code": exc.code.value, "message": exc.message, **exc.details},
                        errors_remaining=remaining_errors,
                    )
                    state.record_prompt("repair", prompt)
                    continue
                except _WaveInterrupted as interrupted:
                    results = interrupted.results
                    cause = interrupted.cause
                    if isinstance(cause, asyncio.CancelledError):
                        wave_error = RunError(
                            code=RunErrorCode.cancelled,
                            message="controller-backed REPL calls were cancelled",
                        )
                    elif isinstance(cause, _RunLimit):
                        wave_error = cause.error
                    elif isinstance(cause, Exception):
                        wave_error = self._error_from_exception(cause)
                    else:
                        wave_error = RunError(
                            code=RunErrorCode.unexpected,
                            message="controller-backed REPL calls were interrupted",
                        )
                    self._write_repl_iteration(
                        state,
                        node,
                        iteration,
                        "repl",
                        started_at,
                        started,
                        turn,
                        prompt=prompt,
                        executions=records,
                        results=results,
                        error=wave_error,
                    )
                    node.iterations += 1
                    self._callback(
                        self._on_iteration_complete,
                        node.depth,
                        iteration,
                        time.monotonic() - started,
                    )
                    raise cause from interrupted
                except asyncio.CancelledError:
                    error = RunError(
                        code=RunErrorCode.cancelled,
                        message="recursive REPL turn cancelled by the user",
                    )
                    self._write_repl_iteration(
                        state,
                        node,
                        iteration,
                        "repl",
                        started_at,
                        started,
                        turn,
                        prompt=prompt,
                        executions=records,
                        results=results,
                        error=error,
                    )
                    node.iterations += 1
                    self._callback(
                        self._on_iteration_complete,
                        node.depth,
                        iteration,
                        time.monotonic() - started,
                    )
                    raise
                except _RunLimit as exc:
                    self._write_repl_iteration(
                        state,
                        node,
                        iteration,
                        "repl",
                        started_at,
                        started,
                        turn,
                        prompt=prompt,
                        executions=records,
                        results=results,
                        error=exc.error,
                    )
                    node.iterations += 1
                    self._callback(
                        self._on_iteration_complete,
                        node.depth,
                        iteration,
                        time.monotonic() - started,
                    )
                    raise
                except Exception as exc:
                    error = self._error_from_exception(exc)
                    self._write_repl_iteration(
                        state,
                        node,
                        iteration,
                        "repl",
                        started_at,
                        started,
                        turn,
                        prompt=prompt,
                        executions=records,
                        results=results,
                        error=error,
                    )
                    node.iterations += 1
                    self._callback(
                        self._on_iteration_complete,
                        node.depth,
                        iteration,
                        time.monotonic() - started,
                    )
                    raise _NodeFailure(error, node.best_partial) from exc

                output = "\n".join(record.stdout for record in records if record.stdout).strip()
                if output:
                    node.best_partial = output[-12_000:]
                failed = any(record.stderr for record in records) or any(
                    result.status != CallStatus.succeeded for result in results
                )
                consecutive_errors = consecutive_errors + 1 if failed else 0
                compacted = False
                compaction_error: RunError | None = None
                try:
                    compacted = await self._maybe_compact(state, node, session, turn)
                except _RunLimit as exc:
                    self._write_repl_iteration(
                        state,
                        node,
                        iteration,
                        "repl",
                        started_at,
                        started,
                        turn,
                        prompt=prompt,
                        executions=records,
                        results=results,
                        error=exc.error,
                    )
                    node.iterations += 1
                    self._callback(
                        self._on_iteration_complete,
                        node.depth,
                        iteration,
                        time.monotonic() - started,
                    )
                    raise
                except Exception as exc:
                    compaction_error = self._error_from_exception(exc)
                    self._best_effort_event(
                        state,
                        "compaction.failed",
                        {"node_id": node.node_id, "error_code": compaction_error.code.value},
                    )
                self._write_repl_iteration(
                    state,
                    node,
                    iteration,
                    "repl",
                    started_at,
                    started,
                    turn,
                    prompt=prompt,
                    executions=records,
                    results=results,
                    error=compaction_error,
                    compaction_completed=compacted,
                )
                node.iterations += 1
                self._callback(
                    self._on_iteration_complete,
                    node.depth,
                    iteration,
                    time.monotonic() - started,
                )
                if final_payload is not None:
                    successful_payload = final_payload
                    return final_payload
                if (
                    state.config.max_errors is not None
                    and consecutive_errors >= state.config.max_errors
                ):
                    raise _NodeFailure(
                        RunError(
                            code=RunErrorCode.error_limit,
                            message="node exceeded consecutive error limit",
                        ),
                        node.best_partial,
                    )
                prompt = self._build_repl_feedback(state, node, iteration, records, results)
                state.record_prompt("feedback", prompt)

            payload = await self._finalize_repl_node(
                state,
                node,
                session,
                repl,
                compact_final=root_persistent and node.depth == 0,
            )
            successful_payload = payload
            return payload
        except asyncio.CancelledError:
            self._complete_node(
                state,
                node,
                NodeStatus.cancelled,
                error=RunError(code=RunErrorCode.cancelled, message="node cancelled"),
            )
            raise
        except _RunLimit as exc:
            status = (
                NodeStatus.timed_out
                if exc.error.code == RunErrorCode.timeout
                else NodeStatus.partial
                if node.best_partial is not None
                else NodeStatus.failed
            )
            self._complete_node(state, node, status, error=exc.error)
            raise
        except _NodeFailure as exc:
            self._complete_node(
                state,
                node,
                NodeStatus.partial if exc.best_partial is not None else NodeStatus.failed,
                best_partial=exc.best_partial,
                error=exc.error,
            )
            raise
        except Exception as exc:
            error = self._error_from_exception(exc)
            status = (
                NodeStatus.timed_out if error.code == RunErrorCode.timeout else NodeStatus.failed
            )
            self._complete_node(state, node, status, error=error)
            raise _NodeFailure(error, node.best_partial) from exc
        finally:
            primary_error = sys.exception()
            cleanup_error: RunError | None = None
            for resource_name, resource in (("REPL worker", repl), ("Codex session", session)):
                if resource is None:
                    continue
                try:
                    await resource.close(state.config.cleanup_timeout_seconds)
                except Exception as exc:
                    cleanup_succeeded = False
                    state.persistent_session_safe = False
                    if cleanup_error is None:
                        cleanup_error = RunError(
                            code=RunErrorCode.cleanup,
                            message=f"{resource_name} cleanup failed",
                            details={"error_type": type(exc).__name__},
                        )
                    self._best_effort_event(
                        state,
                        "node.cleanup_failed",
                        {
                            "node_id": node.node_id,
                            "resource": resource_name,
                            "error_type": type(exc).__name__,
                        },
                    )
            if permit_acquired and cleanup_succeeded:
                session_permit.release()
            if cleanup_error is not None:
                if node.completed_at is None:
                    self._complete_node(
                        state,
                        node,
                        NodeStatus.partial if node.best_partial is not None else NodeStatus.failed,
                        best_partial=node.best_partial,
                        error=cleanup_error,
                    )
                else:
                    node.status = (
                        NodeStatus.partial if node.best_partial is not None else NodeStatus.failed
                    )
                    node.payload = None
                    node.error = cleanup_error
                    self._write_node(state, node)
                raise _RunLimit(cleanup_error, node.best_partial) from primary_error
            if primary_error is None and successful_payload is not None:
                self._complete_node(state, node, NodeStatus.succeeded, payload=successful_payload)

    async def _finalize_repl_node(
        self,
        state: _RunState,
        node: _MutableNode,
        session: RootSession,
        repl: ReplSession,
        *,
        compact_final: bool,
    ) -> FinalPayload:
        iteration = state.config.max_iterations
        prompt = build_finalize_prompt(
            best_partial_answer=node.best_partial,
            reason=f"max_iterations={state.config.max_iterations} was reached",
        )
        state.record_prompt("finalization", prompt)
        self._callback(self._on_iteration_start, node.depth, iteration)
        started_at = datetime.now(UTC)
        started = time.monotonic()
        state.event("iteration.finalization_started", node=node, iteration=iteration)
        turn: DirectTurn | None = None
        records: list[ReplExecutionRecord] = []
        results: list[CallResult] = []
        try:
            async with _deadline_permit(state, state.semaphore, node.deadline):
                turn = await session.turn(
                    RootTurnRequest(
                        prompt=prompt,
                        output_schema=None,
                        timeout_seconds=state.remaining(deadline=node.deadline),
                        cleanup_timeout_seconds=state.config.cleanup_timeout_seconds,
                        phase=f"node_{node.node_id}_finalization",
                    )
                )
            self._record_turn(state, node, turn)
            blocks = self._parse_repl_blocks(
                turn.final_response,
                state.config.max_repl_code_bytes,
            )
            records, results, payload = await self._execute_repl_blocks(
                state,
                node,
                repl,
                blocks,
                allow_calls=False,
            )
            if payload is None:
                raise _BoundaryError(
                    RunErrorCode.invalid_model_output,
                    "forced finalization did not call submit_answer or mark answer ready",
                )
            compacted = False
            compaction_error: RunError | None = None
            try:
                compacted = (
                    await self._maybe_compact(state, node, session, turn)
                    if compact_final
                    else False
                )
            except _RunLimit:
                raise
            except Exception as exc:
                compaction_error = self._error_from_exception(exc)
                self._best_effort_event(
                    state,
                    "compaction.failed",
                    {"node_id": node.node_id, "error_code": compaction_error.code.value},
                )
            self._write_repl_iteration(
                state,
                node,
                iteration,
                "finalization",
                started_at,
                started,
                turn,
                prompt=prompt,
                executions=records,
                results=results,
                error=compaction_error,
                compaction_completed=compacted,
            )
            node.iterations += 1
            self._callback(
                self._on_iteration_complete,
                node.depth,
                iteration,
                time.monotonic() - started,
            )
            return payload
        except asyncio.CancelledError:
            error = RunError(code=RunErrorCode.cancelled, message="finalization cancelled")
            self._write_repl_iteration(
                state,
                node,
                iteration,
                "finalization",
                started_at,
                started,
                turn,
                prompt=prompt,
                executions=records,
                results=results,
                error=error,
            )
            node.iterations += 1
            self._callback(
                self._on_iteration_complete,
                node.depth,
                iteration,
                time.monotonic() - started,
            )
            raise
        except _RunLimit as exc:
            self._write_repl_iteration(
                state,
                node,
                iteration,
                "finalization",
                started_at,
                started,
                turn,
                prompt=prompt,
                executions=records,
                results=results,
                error=exc.error,
            )
            node.iterations += 1
            self._callback(
                self._on_iteration_complete,
                node.depth,
                iteration,
                time.monotonic() - started,
            )
            raise
        except Exception as exc:
            error = self._error_from_exception(exc)
            self._write_repl_iteration(
                state,
                node,
                iteration,
                "finalization",
                started_at,
                started,
                turn,
                prompt=prompt,
                executions=records,
                results=results,
                error=error,
            )
            node.iterations += 1
            self._callback(
                self._on_iteration_complete,
                node.depth,
                iteration,
                time.monotonic() - started,
            )
            raise _NodeFailure(error, node.best_partial) from exc

    async def _execute_repl_blocks(
        self,
        state: _RunState,
        node: _MutableNode,
        repl: ReplSession,
        blocks: list[str],
        *,
        allow_calls: bool,
    ) -> tuple[list[ReplExecutionRecord], list[CallResult], FinalPayload | None]:
        iteration_capacity = state.config.max_calls_per_iteration if allow_calls else 0
        records: list[ReplExecutionRecord] = []
        all_results: list[CallResult] = []
        final_payload: FinalPayload | None = None

        async def query_handler(
            mode: CallMode, prompts: list[str], model: str | None, contexts: list[str | None]
        ) -> tuple[list[str], list[CallResult]]:
            nonlocal iteration_capacity
            if mode not in {CallMode.leaf, CallMode.recursive}:
                raise ValueError("REPL queries support only leaf or recursive modes")
            delegate_mode = cast(Literal[CallMode.leaf, CallMode.recursive], mode)
            calls = [
                DelegateRequest(
                    kind="delegate",
                    call_id=f"q_{uuid.uuid4().hex}",
                    mode=delegate_mode,
                    task=prompt,
                    context=context,
                    model=model,
                )
                for prompt, context in zip(prompts, contexts, strict=True)
            ]
            before = node.calls
            capacity = iteration_capacity if state.config.orchestrator else 0
            call_results = await self._execute_repl_calls(state, node, calls, capacity)
            iteration_capacity = max(0, iteration_capacity - (node.calls - before))
            values = [self._query_value(result) for result in call_results]
            return values, call_results

        async def tool_handler(name: str, arguments: dict[str, Any]) -> tuple[Any, CallResult]:
            nonlocal iteration_capacity
            call = ToolRequest(
                kind="tool",
                call_id=f"t_{uuid.uuid4().hex}",
                name=name,
                arguments=arguments,
            )
            before = node.calls
            call_results = await self._execute_repl_calls(
                state,
                node,
                [call],
                iteration_capacity,
            )
            iteration_capacity = max(0, iteration_capacity - (node.calls - before))
            result = call_results[0]
            return result.value, result

        for code in blocks:
            execution = await repl.execute(
                code,
                timeout_seconds=state.remaining(deadline=node.deadline),
                query_handler=query_handler,
                tool_handler=tool_handler,
            )
            all_results.extend(execution.calls)
            parsed_payload = (
                self._parse_repl_final(
                    execution.final_payload,
                    node.manifest,
                    state.config.max_final_result_bytes,
                )
                if execution.final_payload is not None
                else None
            )
            records.append(self._repl_execution_record(execution, parsed_payload))
            if parsed_payload is not None:
                final_payload = parsed_payload
                break
        return records, all_results, final_payload

    async def _execute_repl_calls(
        self,
        state: _RunState,
        node: _MutableNode,
        calls: Sequence[DelegateRequest | ToolRequest],
        iteration_capacity: int,
    ) -> list[CallResult]:
        admitted = list(calls[: max(0, iteration_capacity)])
        rejected = list(calls[max(0, iteration_capacity) :])
        remaining = max(0, state.config.max_calls_per_node - node.calls)
        overflow = admitted[remaining:]
        admitted = admitted[:remaining]
        node.calls += len(admitted)
        scheduled = [(call, f"call_{uuid.uuid4().hex}") for call in admitted]
        tasks = [
            asyncio.create_task(self._execute_call(state, node, call, canonical_id=canonical_id))
            for call, canonical_id in scheduled
        ]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        completed: list[CallResult] = []
        for (call, canonical_id), task, outcome in zip(scheduled, tasks, outcomes, strict=True):
            if isinstance(outcome, CallResult):
                completed.append(outcome)
            else:
                completed.append(
                    self._result_from_interrupted_task(
                        state,
                        node,
                        call,
                        canonical_id,
                        task,
                    )
                )
        for call in [*overflow, *rejected]:
            mode = CallMode.tool if isinstance(call, ToolRequest) else CallMode(call.mode)
            error = RunError(
                code=RunErrorCode.call_limit,
                message="call rejected by node or iteration limit",
            )
            result = self._failed_call(
                call.call_id,
                mode,
                mode,
                CallStatus.rejected,
                error,
                0,
            )
            completed.append(result)
            state.event(
                "call.rejected",
                {"error_code": error.code.value},
                node=node,
                call_id=result.canonical_id,
            )
        interruption = next(
            (outcome for outcome in outcomes if isinstance(outcome, BaseException)),
            None,
        )
        if interruption is not None:
            raise _WaveInterrupted(interruption, completed)
        return completed

    @staticmethod
    def _query_value(result: CallResult) -> str:
        if result.status == CallStatus.succeeded and result.payload is not None:
            return result.payload.answer
        if result.error is not None:
            return f"Error ({result.error.code.value}): {result.error.message}"
        return f"Error: call ended with status {result.status.value}"

    @staticmethod
    def _repl_execution_record(
        execution: ReplExecution, payload: FinalPayload | None
    ) -> ReplExecutionRecord:
        return ReplExecutionRecord(
            code=execution.code,
            stdout=execution.stdout,
            stderr=execution.stderr,
            stdout_truncated=execution.stdout_truncated,
            stderr_truncated=execution.stderr_truncated,
            variable_types=execution.variable_types,
            final_payload=payload,
            duration_ms=execution.duration_ms,
        )

    @staticmethod
    def _parse_repl_blocks(raw: str, max_bytes: int) -> list[str]:
        raw_bytes = raw.encode("utf-8")
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if len(raw_bytes) > max_bytes:
            raise _BoundaryError(
                RunErrorCode.oversized_model_output,
                "recursive node response exceeds max_repl_code_bytes",
                {
                    "raw_sha256": raw_sha256,
                    "observed_bytes": len(raw_bytes),
                    "max_bytes": max_bytes,
                },
            )
        blocks = [match.group("code").strip() for match in _REPL_BLOCK_PATTERN.finditer(raw)]
        blocks = [block for block in blocks if block]
        if not blocks:
            raise _BoundaryError(
                RunErrorCode.invalid_model_output,
                "recursive node response contains no non-empty fenced REPL block",
                {"raw_sha256": raw_sha256},
            )
        if len(blocks) > 32:
            raise _BoundaryError(
                RunErrorCode.invalid_model_output,
                "recursive node response contains more than 32 REPL blocks",
                {"raw_sha256": raw_sha256, "observed_blocks": len(blocks)},
            )
        return blocks

    @staticmethod
    def _parse_repl_final(
        value: Any,
        manifest: ContextManifest,
        max_bytes: int,
    ) -> FinalPayload:
        normalized: Any = (
            {
                "schema_version": "1.0",
                "answer": value,
                "evidence": [],
                "uncertainties": [],
            }
            if isinstance(value, str)
            else value
        )
        try:
            raw = canonical_json_bytes(normalized)
        except (TypeError, ValueError) as exc:
            raise _BoundaryError(
                RunErrorCode.invalid_model_output,
                "REPL final answer is not strict JSON data",
            ) from exc
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        if len(raw) > max_bytes:
            raise _BoundaryError(
                RunErrorCode.oversized_model_output,
                "REPL final payload exceeds max_final_result_bytes",
                {
                    "raw_sha256": raw_sha256,
                    "observed_bytes": len(raw),
                    "max_bytes": max_bytes,
                },
            )
        try:
            payload = FinalPayload.model_validate(normalized, strict=True)
        except ValidationError as exc:
            raise _BoundaryError(
                RunErrorCode.invalid_model_output,
                "REPL final payload failed strict schema validation",
                {
                    "raw_sha256": raw_sha256,
                    "validation_errors": len(exc.errors()),
                },
            ) from exc
        RecursiveRunner._validate_final_payload(
            payload,
            manifest,
            max_bytes,
            raw_sha256=raw_sha256,
        )
        return payload

    @staticmethod
    def _build_repl_feedback(
        state: _RunState,
        node: _MutableNode,
        iteration: int,
        executions: list[ReplExecutionRecord],
        results: list[CallResult],
    ) -> str:
        artifact = str(state.store.iteration_path(node.node_id, iteration))
        calls_remaining = max(0, state.config.max_calls_per_node - node.calls)
        iterations_remaining = max(0, state.config.max_iterations - iteration - 1)

        def projected(preview: int) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for index, execution in enumerate(executions):
                items.append(
                    {
                        "block": index,
                        "stdout": execution.stdout[:preview],
                        "stdout_truncated": execution.stdout_truncated
                        or len(execution.stdout) > preview,
                        "stderr": execution.stderr[:preview],
                        "stderr_truncated": execution.stderr_truncated
                        or len(execution.stderr) > preview,
                        "variable_types": execution.variable_types,
                    }
                )
            if results:
                items.append(
                    {
                        "calls": [
                            RecursiveRunner._project_call_result(result, preview)
                            for result in results
                        ]
                    }
                )
            return items

        for preview in (8192, 4096, 2048, 1024, 512, 256, 128, 64, 0):
            feedback = build_feedback_prompt(
                iteration=iteration,
                results=projected(preview),
                calls_remaining=calls_remaining,
                iterations_remaining=iterations_remaining,
                artifact_path=artifact if preview else None,
            )
            if len(feedback.encode("utf-8")) <= state.config.max_repl_output_bytes:
                return feedback
        raise _NodeFailure(
            RunError(
                code=RunErrorCode.oversized_model_output,
                message="REPL feedback exceeds max_repl_output_bytes after projection",
            ),
            node.best_partial,
        )

    def _write_repl_iteration(
        self,
        state: _RunState,
        node: _MutableNode,
        iteration: int,
        phase: Literal["repl", "finalization"],
        started_at: datetime,
        started: float,
        turn: DirectTurn | None,
        *,
        prompt: str,
        executions: list[ReplExecutionRecord] | None = None,
        results: list[CallResult] | None = None,
        error: RunError | None = None,
        compaction_completed: bool = False,
    ) -> None:
        record = IterationRecord(
            node_id=node.node_id,
            iteration=iteration,
            phase=phase,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            duration_ms=round((time.monotonic() - started) * 1000),
            prompt_sha256=prompt_sha256(prompt),
            executions=executions or [],
            results=results or [],
            error=error,
            usage=turn.usage if turn is not None else _unavailable_usage(),
            compaction_completed=compaction_completed,
        )
        state.store.write(state.store.iteration_path(node.node_id, iteration), record)
        state.iterations_written = True
        state.event(
            "iteration.completed",
            {
                "phase": phase,
                "duration_ms": record.duration_ms,
                "error_code": error.code.value if error is not None else None,
            },
            node=node,
            iteration=iteration,
        )

    def _result_from_interrupted_task(
        self,
        state: _RunState,
        parent: _MutableNode,
        call: DelegateRequest | ToolRequest,
        canonical_id: str,
        task: asyncio.Task[CallResult],
    ) -> CallResult:
        child = next(
            (node for node in state.nodes.values() if node.parent_call_id == canonical_id),
            None,
        )
        requested = CallMode.tool if isinstance(call, ToolRequest) else CallMode(call.mode)
        executed = requested
        if requested == CallMode.recursive and parent.depth + 1 >= state.config.max_depth:
            executed = CallMode.leaf
        if task.cancelled():
            error = RunError(
                code=RunErrorCode.cancelled,
                message="call cancelled because its wave was interrupted",
            )
            status = CallStatus.cancelled
        else:
            task_error = task.exception()
            if task_error is None:
                return task.result()
            if isinstance(task_error, (_RunLimit, _NodeFailure)):
                error = task_error.error
            elif isinstance(task_error, Exception):
                error = self._error_from_exception(task_error)
            else:
                error = RunError(
                    code=RunErrorCode.unexpected,
                    message="call ended without a controller result",
                )
            status = (
                CallStatus.timed_out if error.code == RunErrorCode.timeout else CallStatus.failed
            )
        result = self._failed_call(
            call.call_id,
            requested,
            executed,
            status,
            error,
            0,
            canonical_id=canonical_id,
            child_node_id=child.node_id if child is not None else None,
            usage=(self._usage_for_subtree(state, child.node_id) if child is not None else None),
        )
        state.event(
            "call.cancelled" if status == CallStatus.cancelled else "call.failed",
            {"error_code": error.code.value},
            node=parent,
            call_id=canonical_id,
        )
        return result

    @staticmethod
    def _project_call_result(result: CallResult, preview_chars: int) -> dict[str, Any]:
        projected: dict[str, Any] = {
            "call_id": result.call_id,
            "canonical_id": result.canonical_id,
            "requested_mode": result.requested_mode.value,
            "executed_mode": result.executed_mode.value,
            "status": result.status.value,
            "child_node_id": result.child_node_id,
        }
        if result.error is not None:
            projected["error"] = {
                "code": result.error.code.value,
                "message": result.error.message[:preview_chars],
                "truncated": len(result.error.message) > preview_chars,
            }
        if result.payload is not None:
            answer = result.payload.answer
            projected["payload"] = {
                "answer_preview": answer[:preview_chars],
                "answer_truncated": len(answer) > preview_chars,
                "evidence": (
                    [item.model_dump(mode="json") for item in result.payload.evidence[:4]]
                    if preview_chars >= 512
                    else []
                ),
                "uncertainties": (result.payload.uncertainties[:4] if preview_chars >= 512 else []),
            }
        if result.executed_mode == CallMode.tool and result.status == CallStatus.succeeded:
            encoded = json.dumps(
                result.value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            projected["value_preview"] = encoded[:preview_chars]
            projected["value_truncated"] = len(encoded) > preview_chars
        return projected

    async def _execute_call(
        self,
        state: _RunState,
        parent: _MutableNode,
        call: DelegateRequest | ToolRequest,
        *,
        canonical_id: str | None = None,
    ) -> CallResult:
        canonical_id = canonical_id or f"call_{uuid.uuid4().hex}"
        started = time.monotonic()
        if isinstance(call, ToolRequest):
            return await self._execute_tool(state, parent, call, canonical_id, started)
        requested = CallMode(call.mode)
        executed = requested
        if requested == CallMode.recursive and parent.depth + 1 >= state.config.max_depth:
            executed = CallMode.leaf
        model_name = state.config.resolved_sub_model or "sdk-default"
        self._callback(
            self._on_subcall_start,
            parent.depth + 1,
            call.model or model_name,
            call.task[:200],
        )
        state.event(
            "call.started",
            {"requested_mode": requested.value, "executed_mode": executed.value},
            node=parent,
            call_id=canonical_id,
        )
        error_text: str | None = None
        try:
            model = state.config.resolve_requested_model(call.model)
            effort = state.config.resolve_requested_effort(call.reasoning_effort)
            child = await self._reserve_node(
                state,
                parent=parent,
                canonical_call_id=canonical_id,
                depth=parent.depth + 1,
                requested_mode=requested,
                executed_mode=executed,
                task=call.task.strip(),
                model=model,
                effort=effort,
                context=call.context,
            )
            if child is None:
                error = RunError(
                    code=RunErrorCode.node_limit,
                    message="call rejected because max_total_nodes was reached",
                )
                error_text = error.message
                result = self._failed_call(
                    call.call_id,
                    requested,
                    executed,
                    CallStatus.rejected,
                    error,
                    started,
                    canonical_id=canonical_id,
                )
                state.event(
                    "call.rejected",
                    {"error_code": error.code.value},
                    node=parent,
                    call_id=canonical_id,
                )
                return result
            try:
                payload = (
                    await self._run_leaf_node(state, child)
                    if executed == CallMode.leaf
                    else await self._run_repl_session_node(state, child, root_persistent=False)
                )
            except (_NodeFailure, _RunLimit) as exc:
                if exc.error.code == RunErrorCode.cleanup:
                    raise _RunLimit(exc.error, exc.best_partial) from exc
                if isinstance(exc, _RunLimit):
                    if exc.error.code != RunErrorCode.timeout or child.deadline >= parent.deadline:
                        raise
                error_text = exc.error.message
                result = self._failed_call(
                    call.call_id,
                    requested,
                    executed,
                    (
                        CallStatus.timed_out
                        if exc.error.code == RunErrorCode.timeout
                        else CallStatus.failed
                    ),
                    exc.error,
                    started,
                    canonical_id=canonical_id,
                    child_node_id=child.node_id,
                    usage=self._usage_for_subtree(state, child.node_id),
                )
                state.event(
                    "call.failed",
                    {"error_code": exc.error.code.value},
                    node=parent,
                    call_id=canonical_id,
                )
                return result
            result = CallResult(
                call_id=call.call_id,
                canonical_id=canonical_id,
                requested_mode=requested,
                executed_mode=executed,
                status=CallStatus.succeeded,
                child_node_id=child.node_id,
                payload=payload,
                usage=self._usage_for_subtree(state, child.node_id),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            state.event(
                "call.completed",
                {"status": result.status.value},
                node=parent,
                call_id=canonical_id,
            )
            return result
        except asyncio.CancelledError:
            error_text = "call cancelled"
            raise
        except _RunLimit as exc:
            error_text = exc.error.message
            raise
        except ValueError as exc:
            error = RunError(
                code=(
                    RunErrorCode.context_limit
                    if isinstance(exc, _ContextLimitError)
                    else RunErrorCode.unsupported
                ),
                message=str(exc),
            )
            error_text = error.message
            result = self._failed_call(
                call.call_id,
                requested,
                executed,
                CallStatus.rejected,
                error,
                started,
                canonical_id=canonical_id,
            )
            state.event(
                "call.rejected",
                {"error_code": error.code.value},
                node=parent,
                call_id=canonical_id,
            )
            return result
        except Exception:
            error_text = "unexpected controller failure"
            raise
        finally:
            self._callback(
                self._on_subcall_complete,
                parent.depth + 1,
                call.model or model_name,
                time.monotonic() - started,
                error_text,
            )

    async def _execute_tool(
        self,
        state: _RunState,
        parent: _MutableNode,
        call: ToolRequest,
        canonical_id: str,
        started: float,
    ) -> CallResult:
        registry = state.tools if parent.depth == 0 else state.sub_tools
        state.event(
            "tool.started",
            {"name": call.name},
            node=parent,
            call_id=canonical_id,
        )
        try:
            async with _deadline_permit(state, state.tool_semaphore, parent.deadline):
                value = await registry.invoke(
                    call.name,
                    call.arguments,
                    timeout_seconds=min(
                        state.config.tool_timeout_seconds,
                        state.remaining(deadline=parent.deadline),
                    ),
                )
            serialized = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            if len(serialized) > state.config.max_tool_result_bytes:
                raise ToolError("tool result exceeds max_tool_result_bytes")
            result = CallResult(
                call_id=call.call_id,
                canonical_id=canonical_id,
                requested_mode=CallMode.tool,
                executed_mode=CallMode.tool,
                status=CallStatus.succeeded,
                value=value,
                usage=_unavailable_usage(),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            state.event(
                "tool.completed",
                {"name": call.name},
                node=parent,
                call_id=canonical_id,
            )
            return result
        except ToolInputError as exc:
            code = RunErrorCode.invalid_tool_input
            error = RunError(code=code, message=str(exc))
        except ToolError as exc:
            code = (
                RunErrorCode.unknown_tool
                if str(exc).startswith("unknown controller tool")
                else RunErrorCode.tool_runtime
            )
            error = RunError(code=code, message=str(exc))
        result = self._failed_call(
            call.call_id,
            CallMode.tool,
            CallMode.tool,
            CallStatus.failed,
            error,
            started,
            canonical_id=canonical_id,
        )
        state.event(
            "tool.failed",
            {"name": call.name, "error_code": error.code.value},
            node=parent,
            call_id=canonical_id,
        )
        return result

    @staticmethod
    def _failed_call(
        call_id: str,
        requested: CallMode,
        executed: CallMode,
        status: CallStatus,
        error: RunError,
        started: float | int,
        *,
        canonical_id: str | None = None,
        child_node_id: str | None = None,
        usage: RunUsage | None = None,
    ) -> CallResult:
        duration = 0 if started == 0 else round((time.monotonic() - float(started)) * 1000)
        return CallResult(
            call_id=call_id,
            canonical_id=canonical_id or f"call_{uuid.uuid4().hex}",
            requested_mode=requested,
            executed_mode=executed,
            status=status,
            child_node_id=child_node_id,
            error=error,
            usage=usage or _unavailable_usage(),
            duration_ms=duration,
        )

    @staticmethod
    def _usage_for_subtree(state: _RunState, root_node_id: str) -> RunUsage:
        descendants = {root_node_id}
        changed = True
        while changed:
            changed = False
            for node in state.nodes.values():
                if node.parent_node_id in descendants and node.node_id not in descendants:
                    descendants.add(node.node_id)
                    changed = True
        usages = [state.usage.for_thread(state.nodes[node_id].thread_id) for node_id in descendants]
        if not usages or any(
            not usage.available or usage.total is None or usage.last is None for usage in usages
        ):
            return _unavailable_usage()
        available = usages
        root_usage = state.usage.for_thread(state.nodes[root_node_id].thread_id)
        representative = root_usage if root_usage.available else available[-1]
        assert representative.last is not None
        totals = [usage.total for usage in available if usage.total is not None]
        return RunUsage(
            available=True,
            last=representative.last,
            total=_sum_breakdowns(totals),
            model_context_window=representative.model_context_window,
        )

    def _record_turn(self, state: _RunState, node: _MutableNode, turn: DirectTurn) -> None:
        state.usage.record(turn)
        node.thread_id = turn.thread_id
        if node.depth == 0:
            state.root_thread_id = turn.thread_id
        state.codex_runtime_version = turn.codex_runtime_version
        usage = state.usage.known_aggregate()
        if (
            state.config.max_tokens is not None
            and usage.available
            and usage.total is not None
            and usage.total.total_tokens > state.config.max_tokens
        ):
            raise _RunLimit(
                RunError(
                    code=RunErrorCode.token_limit,
                    message="observed token usage exceeded max_tokens",
                    details={
                        "observed": usage.total.total_tokens,
                        "configured": state.config.max_tokens,
                    },
                ),
                state.best_partial(),
            )

    async def _maybe_compact(
        self,
        state: _RunState,
        node: _MutableNode,
        session: RootSession,
        turn: DirectTurn,
    ) -> bool:
        usage = turn.usage
        if (
            not state.config.compaction
            or not usage.available
            or usage.last is None
            or not usage.model_context_window
        ):
            return False
        ratio = usage.last.input_tokens / usage.model_context_window
        if ratio < state.config.compaction_threshold:
            return False
        state.event("compaction.requested", {"observed_ratio": ratio}, node=node)
        async with _deadline_permit(state, state.semaphore, node.deadline):
            await session.compact(
                min(
                    state.config.cleanup_timeout_seconds,
                    state.remaining(deadline=node.deadline),
                )
            )
        state.event("compaction.completed", {"observed_ratio": ratio}, node=node)
        return True

    def _complete_node(
        self,
        state: _RunState,
        node: _MutableNode,
        status: NodeStatus,
        *,
        payload: FinalPayload | None = None,
        best_partial: str | None = None,
        error: RunError | None = None,
    ) -> None:
        if node.completed_at is not None:
            return
        node.status = status
        node.payload = payload
        node.best_partial = best_partial or node.best_partial
        node.error = error
        node.completed_at = datetime.now(UTC)
        node.duration_ms = round((time.monotonic() - node.started_monotonic) * 1000)
        self._write_node(state, node)
        state.event(
            "node.completed",
            {
                "status": status.value,
                "duration_ms": node.duration_ms,
                "error_code": error.code.value if error is not None else None,
            },
            node=node,
        )

    def _write_node(self, state: _RunState, node: _MutableNode) -> None:
        record = self._node_record(state, node)
        state.store.write(state.store.node_path(node.node_id), record)
        state.nodes_written = True

    @staticmethod
    def _node_record(state: _RunState, node: _MutableNode) -> NodeRecord:
        return NodeRecord(
            node_id=node.node_id,
            parent_node_id=node.parent_node_id,
            parent_call_id=node.parent_call_id,
            depth=node.depth,
            requested_mode=node.requested_mode,
            executed_mode=node.executed_mode,
            task=node.task,
            context_manifest=str(node.manifest_path),
            context_manifest_sha256=node.manifest_hash,
            model=node.model,
            reasoning_effort=node.reasoning_effort,
            status=node.status,
            thread_id=node.thread_id,
            initial_prompt_sha256=node.initial_prompt_sha256,
            started_at=node.started_at,
            completed_at=node.completed_at,
            duration_ms=node.duration_ms,
            iterations=node.iterations,
            calls=node.calls,
            payload=node.payload,
            best_partial_answer=node.best_partial,
            error=node.error,
            usage=state.usage.for_thread(node.thread_id),
        )

    @staticmethod
    def _parse_final(raw: str, manifest: ContextManifest, max_bytes: int) -> FinalPayload:
        raw_bytes = raw.encode("utf-8")
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if len(raw_bytes) > max_bytes:
            raise _BoundaryError(
                RunErrorCode.oversized_model_output,
                "final payload exceeds max_final_result_bytes",
                {
                    "raw_sha256": raw_sha256,
                    "observed_bytes": len(raw_bytes),
                    "max_bytes": max_bytes,
                },
            )
        try:
            payload = FinalPayload.model_validate_json(raw_bytes, strict=True)
        except ValidationError as exc:
            raise _BoundaryError(
                RunErrorCode.invalid_model_output,
                "final payload failed strict schema validation",
                {
                    "raw_sha256": raw_sha256,
                    "validation_errors": len(exc.errors()),
                },
            ) from exc
        RecursiveRunner._validate_final_payload(
            payload,
            manifest,
            max_bytes,
            raw_sha256=raw_sha256,
        )
        return payload

    @staticmethod
    def _validate_final_payload(
        payload: FinalPayload,
        manifest: ContextManifest,
        max_bytes: int,
        *,
        raw_sha256: str,
    ) -> None:
        canonical_size = len(canonical_json_bytes(payload.model_dump(mode="json")))
        if canonical_size > max_bytes:
            raise _BoundaryError(
                RunErrorCode.oversized_model_output,
                "validated final payload exceeds max_final_result_bytes",
                {
                    "raw_sha256": raw_sha256,
                    "observed_bytes": canonical_size,
                    "max_bytes": max_bytes,
                },
            )
        try:
            validate_evidence(payload, manifest)
        except EvidenceValidationError as exc:
            raise _BoundaryError(
                RunErrorCode.invalid_model_output,
                str(exc),
                {"raw_sha256": raw_sha256},
            ) from exc

    async def _verify_integrity(self, state: _RunState, manifest: ContextManifest) -> None:
        current = await self._scan_manifest(state)
        changes = integrity_changes(manifest, current)
        if changes:
            raise _BoundaryError(
                RunErrorCode.context_integrity,
                "context changed during the run",
                {"changes": changes[:64], "change_count": len(changes)},
            )
        # Supplied contexts are durable inputs too. Inherited manifests need only one scan.
        checked = {state.store.manifest_path}
        child_config = replace(
            state.config,
            include=(),
            exclude=(),
            max_context_bytes=state.config.max_query_context_bytes,
        )
        for node in state.nodes.values():
            if node.manifest_path in checked:
                continue
            checked.add(node.manifest_path)
            current = await self._scan_manifest(
                state, context_root=Path(node.manifest.context_root), config=child_config
            )
            changes = integrity_changes(node.manifest, current)
            if changes:
                raise _BoundaryError(
                    RunErrorCode.context_integrity,
                    "child context changed during the run",
                    {"node_id": node.node_id, "changes": changes[:64]},
                )
        state.remaining()
        state.event("integrity.validated")

    def _persist_session_thread(self, state: _RunState, thread_id: str) -> None:
        if state.session is None or state.session_file is None:
            return
        state.session = state.session.model_copy(
            update={"root_thread_id": thread_id, "updated_at": datetime.now(UTC)}
        )
        atomic_write_json(state.session_file, state.session)

    @staticmethod
    def _callback(callback: Callable[..., None] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            return

    @staticmethod
    def _error_from_exception(exc: Exception) -> RunError:
        if isinstance(exc, _BoundaryError):
            return RunError(code=exc.code, message=exc.message, details=exc.details)
        if isinstance(exc, AdapterTimeoutError):
            return RunError(code=RunErrorCode.timeout, message=str(exc))
        if isinstance(exc, ReplTimeoutError):
            return RunError(code=RunErrorCode.timeout, message=str(exc))
        if isinstance(exc, ReplError):
            return RunError(code=RunErrorCode.repl_runtime, message=str(exc))
        if isinstance(exc, AdapterAuthenticationError):
            return RunError(code=RunErrorCode.authentication, message=str(exc))
        if isinstance(exc, AdapterCleanupError):
            return RunError(code=RunErrorCode.cleanup, message=str(exc))
        if isinstance(exc, AdapterRuntimeError):
            return RunError(
                code=adapter_error_code(exc),
                message=str(exc),
                retry=RetryClassification.transient,
                details={"phase": exc.phase, "error_type": exc.error_type},
            )
        if isinstance(exc, AdapterError):
            return RunError(
                code=adapter_error_code(exc),
                message=str(exc),
                retry=RetryClassification.transient,
            )
        if isinstance(exc, ManifestDeadlineError):
            return RunError(code=RunErrorCode.timeout, message=str(exc))
        if isinstance(exc, ManifestError):
            return RunError(code=RunErrorCode.manifest, message=str(exc))
        if isinstance(exc, StorageError):
            return RunError(code=RunErrorCode.artifact_write, message=str(exc))
        if isinstance(exc, InvocationError):
            return RunError(code=RunErrorCode.unsupported, message=str(exc))
        return RunError(
            code=RunErrorCode.unexpected,
            message="unexpected controller failure",
            details={"error_type": type(exc).__name__},
        )

    def _reduce_failure(self, exc: Exception) -> tuple[RunStatus, RunError]:
        error = self._error_from_exception(exc)
        status = RunStatus.timed_out if error.code == RunErrorCode.timeout else RunStatus.failed
        return status, error

    def _finalize(
        self,
        state: _RunState,
        status: RunStatus,
        payload: FinalPayload | None,
        best_partial: str | None,
        error: RunError | None,
    ) -> RunResult:
        usage = state.usage.aggregate()
        observed = usage.total.total_tokens if usage.available and usage.total is not None else None
        runtime = state.runtime.model_copy(
            update={
                "codex_runtime_version": state.codex_runtime_version,
                "root_thread_id": state.root_thread_id,
                "prompt_sha256s": state.prompt_hashes,
                "limit_report": state.config.limit_report(observed_tokens=observed),
            }
        )
        nodes = [self._node_record(state, node) for node in state.nodes.values()]
        result = RunResult(
            run_id=state.run_id,
            strategy=state.strategy,
            status=status,
            started_at=state.started_at,
            completed_at=datetime.now(UTC),
            duration_ms=round((time.monotonic() - state.started_monotonic) * 1000),
            payload=payload,
            best_partial_answer=best_partial,
            error=error,
            usage=usage,
            runtime=runtime,
            artifacts=state.store.artifacts(
                manifest=state.manifest_written,
                nodes=state.nodes_written,
                iterations=state.iterations_written,
            ),
            nodes=nodes,
        )
        return result

    def _complete_run(
        self,
        state: _RunState,
        status: RunStatus,
        payload: FinalPayload | None,
        best_partial: str | None,
        error: RunError | None,
    ) -> RunResult:
        result = self._finalize(state, status, payload, best_partial, error)
        result, persisted = self._persist_result(state, result)
        session_invalidated = self._invalidate_unsafe_session(state, result)
        if persisted:
            try:
                self._update_session(state, result)
            except StorageError as exc:
                if result.status == RunStatus.succeeded:
                    partial = result.model_copy(
                        update={
                            "status": RunStatus.partial,
                            "error": self._error_from_exception(exc),
                        }
                    )
                    result = partial
                    try:
                        state.store.write(state.store.result_path, partial)
                    except StorageError:
                        pass
                self._best_effort_event(
                    state,
                    "session.persistence_failed",
                    {"error_code": RunErrorCode.artifact_write.value},
                )
        elif session_invalidated and state.session is not None and state.session_file is not None:
            try:
                atomic_write_json(state.session_file, state.session)
            except StorageError:
                self._best_effort_event(
                    state,
                    "session.invalidation_failed",
                    {"error_code": RunErrorCode.artifact_write.value},
                )
        terminal = {
            RunStatus.succeeded: "run.completed",
            RunStatus.partial: "run.partial",
            RunStatus.failed: "run.failed",
            RunStatus.cancelled: "run.cancelled",
            RunStatus.timed_out: "run.timed_out",
        }[result.status]
        self._best_effort_event(
            state,
            terminal,
            {
                "status": result.status.value,
                "duration_ms": result.duration_ms,
                "nodes": len(result.nodes),
            },
        )
        return result

    def _invalidate_unsafe_session(self, state: _RunState, result: RunResult) -> bool:
        if state.session is None:
            return False
        unsafe_error = result.error is not None and result.error.code in {
            RunErrorCode.context_integrity,
            RunErrorCode.cleanup,
        }
        if state.persistent_session_safe and not unsafe_error:
            return False
        old_thread_id = state.root_thread_id or state.session.root_thread_id
        if old_thread_id is not None:
            self._usage_baselines.pop(old_thread_id, None)
        state.root_thread_id = None
        state.session = state.session.model_copy(
            update={
                "root_thread_id": None,
                "root_usage_total": None,
                "updated_at": datetime.now(UTC),
            }
        )
        self._best_effort_event(
            state,
            "session.invalidated",
            {"reason": result.error.code.value if result.error is not None else "unsafe-cleanup"},
        )
        return True

    def _persist_result(self, state: _RunState, result: RunResult) -> tuple[RunResult, bool]:
        try:
            state.store.write(state.store.result_path, result)
            return result, True
        except StorageError as exc:
            if result.status == RunStatus.succeeded:
                result = result.model_copy(
                    update={
                        "status": RunStatus.partial,
                        "error": self._error_from_exception(exc),
                    }
                )
                try:
                    state.store.write(state.store.result_path, result)
                    return result, True
                except StorageError:
                    pass
            self._best_effort_event(
                state,
                "result.persistence_failed",
                {"error_code": RunErrorCode.artifact_write.value},
            )
            return result, False

    @staticmethod
    def _update_session(state: _RunState, result: RunResult) -> None:
        if state.session is None or state.session_file is None:
            return
        answer = result.payload.answer if result.payload is not None else result.best_partial_answer
        entry = SessionHistoryEntry(
            run_id=result.run_id,
            context_version=state.session.context_count,
            task_preview=state.task[:2000],
            task_sha256=prompt_sha256(state.task),
            status=result.status,
            answer_preview=answer[:4000] if answer is not None else None,
            answer_sha256=prompt_sha256(answer) if answer is not None else None,
        )
        root_thread_id = state.root_thread_id or state.session.root_thread_id
        root_usage = state.usage.latest.get(root_thread_id) if root_thread_id is not None else None
        history = [*state.session.history[-999:], entry]
        state.session = SessionRecord.model_validate(
            {
                **state.session.model_dump(mode="python"),
                "context_count": state.session.context_count + 1,
                "history": history,
                "root_thread_id": root_thread_id,
                "root_usage_total": (
                    root_usage.total
                    if root_usage is not None and root_usage.total is not None
                    else state.session.root_usage_total
                ),
                "updated_at": datetime.now(UTC),
            },
            strict=True,
        )
        atomic_write_json(state.session_file, state.session)

    @staticmethod
    def _best_effort_event(
        state: _RunState,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            state.event(event_type, payload)
        except StorageError:
            return
