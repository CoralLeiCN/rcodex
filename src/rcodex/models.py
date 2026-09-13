"""Strict contracts for Recursive Codex runs and model/controller boundaries."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rcodex.json_values import normalize_json_object


class StrictModel(BaseModel):
    """Base class for persisted and model-authored contracts."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RunStrategy(StrEnum):
    direct = "direct"
    recursive = "recursive"


class RunStatus(StrEnum):
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"


class NodeStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"


class CallMode(StrEnum):
    leaf = "leaf"
    recursive = "recursive"
    tool = "tool"


class CallStatus(StrEnum):
    succeeded = "succeeded"
    failed = "failed"
    rejected = "rejected"
    cancelled = "cancelled"
    timed_out = "timed_out"


class RunErrorCode(StrEnum):
    authentication = "authentication"
    sdk_startup = "sdk-startup"
    sdk_runtime = "sdk-runtime"
    timeout = "timeout"
    cancelled = "cancelled"
    invalid_model_output = "invalid-model-output"
    oversized_model_output = "oversized-model-output"
    context_integrity = "context-integrity"
    manifest = "manifest"
    artifact_write = "artifact-write"
    cleanup = "cleanup"
    depth_limit = "depth-limit"
    node_limit = "node-limit"
    call_limit = "call-limit"
    iteration_limit = "iteration-limit"
    token_limit = "token-limit"
    error_limit = "error-limit"
    unknown_tool = "unknown-tool"
    invalid_tool_input = "invalid-tool-input"
    tool_runtime = "tool-runtime"
    repl_runtime = "repl-runtime"
    unsupported = "unsupported"
    unexpected = "unexpected"


class RetryClassification(StrEnum):
    never = "never"
    transient = "transient"


class RunError(StrictModel):
    code: RunErrorCode
    message: str = Field(min_length=1, max_length=1000)
    retry: RetryClassification = RetryClassification.never
    details: dict[str, Any] = Field(default_factory=dict, max_length=64)


class EvidenceItem(StrictModel):
    context_entry_id: str = Field(pattern=r"^file_[0-9]{6}$")
    path: str = Field(min_length=1, max_length=4096)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        return self


class FinalPayload(StrictModel):
    """Task content authored by Codex; lifecycle state stays controller-owned."""

    schema_version: Literal["1.0"]
    answer: str = Field(min_length=1, max_length=100_000)
    evidence: list[EvidenceItem] = Field(max_length=256)
    uncertainties: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(max_length=64)


class DelegateRequest(StrictModel):
    kind: Literal["delegate"]
    call_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    mode: Literal[CallMode.leaf, CallMode.recursive]
    task: str = Field(min_length=1, max_length=16_384)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_task(self) -> Self:
        if not self.task.strip():
            raise ValueError("delegate task must not be blank")
        return self


class ToolRequest(StrictModel):
    kind: Literal["tool"]
    call_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=128)

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        return normalize_json_object(value)


class ManifestEntry(StrictModel):
    id: str = Field(pattern=r"^file_[0-9]{6}$")
    relative_path: str = Field(min_length=1, max_length=4096)
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1, max_length=255)
    line_count: int = Field(ge=0)


class ContextManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    context_root: str = Field(min_length=1, max_length=4096)
    entries: list[ManifestEntry] = Field(min_length=1, max_length=999_999)


class TokenUsageBreakdown(StrictModel):
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    cache_write_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class RunUsage(StrictModel):
    available: bool
    last: TokenUsageBreakdown | None = None
    total: TokenUsageBreakdown | None = None
    model_context_window: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        measured = (self.last, self.total, self.model_context_window)
        if self.available and (self.last is None or self.total is None):
            raise ValueError("available usage requires last and total breakdowns")
        if not self.available and any(value is not None for value in measured):
            raise ValueError("unavailable usage forbids measured fields")
        return self


class RunLimits(StrictModel):
    run_timeout_seconds: float = Field(gt=0, le=86_400)
    cleanup_timeout_seconds: float = Field(gt=0, le=60)
    node_timeout_seconds: float = Field(gt=0, le=86_400)
    leaf_timeout_seconds: float = Field(gt=0, le=86_400)
    tool_timeout_seconds: float = Field(gt=0, le=3600)
    max_manifest_entries: int = Field(ge=1, le=999_999)
    max_context_bytes: int = Field(ge=1, le=1_099_511_627_776)
    max_final_result_bytes: int = Field(ge=1024, le=1_048_576)
    max_repl_code_bytes: int = Field(ge=1024, le=1_048_576)
    max_repl_output_bytes: int = Field(ge=1024, le=16_777_216)
    repl_memory_bytes: int = Field(ge=67_108_864, le=17_179_869_184)
    repl_cpu_seconds: int = Field(ge=1, le=86_400)
    max_tool_result_bytes: int = Field(ge=256, le=1_048_576)
    max_depth: int = Field(ge=0, le=16)
    max_iterations: int = Field(ge=1, le=100)
    max_calls_per_iteration: int = Field(ge=1, le=32)
    max_calls_per_node: int = Field(ge=1, le=1024)
    max_total_nodes: int = Field(ge=1, le=4096)
    max_concurrency: int = Field(ge=1, le=64)
    max_batch_size: int = Field(ge=1, le=4096)
    max_errors: int | None = Field(default=None, ge=1, le=100)
    max_tokens: int | None = Field(default=None, ge=1)


class LimitEnforcement(StrEnum):
    hard = "hard"
    observed = "observed"
    unsupported = "unsupported"


class LimitReportItem(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    enforcement: LimitEnforcement
    configured: int | float | None
    observed: int | float | None = None
    note: str = Field(min_length=1, max_length=500)


class RequestedCapabilities(StrictModel):
    sandbox: Literal["read-only"] = "read-only"
    delegation_requested_disabled: Literal[True] = True
    web_search_requested_disabled: Literal[True] = True
    connectors_requested_disabled: Literal[True] = True
    mcp_requested_disabled: Literal[True] = True


class OpenAICompatibleProvider(StrictModel):
    base_url: str = Field(min_length=1, max_length=2048)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value.strip() or any(character.isspace() for character in value):
            raise ValueError("provider base URL must not be blank or contain whitespace")
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("provider base URL must be a valid HTTP(S) URL") from exc
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("provider base URL must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("provider base URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("provider base URL must not contain a query or fragment")
        return value


class RuntimeMetadata(StrictModel):
    rcodex_version: str = Field(min_length=1, max_length=255)
    python_version: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=1000)
    openai_codex_version: str = Field(min_length=1, max_length=255)
    codex_runtime_version: str | None = Field(default=None, min_length=1, max_length=255)
    root_model: str | None = Field(default=None, min_length=1, max_length=255)
    sub_model: str | None = Field(default=None, min_length=1, max_length=255)
    provider: OpenAICompatibleProvider | None = None
    reasoning_effort: str = Field(min_length=1, max_length=32)
    sub_reasoning_effort: str = Field(min_length=1, max_length=32)
    prompt_template_version: str = Field(min_length=1, max_length=255)
    prompt_sha256s: dict[
        Annotated[str, Field(min_length=1, max_length=100)],
        Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    ] = Field(default_factory=dict, max_length=256)
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    root_thread_id: str | None = Field(default=None, min_length=1, max_length=1000)
    session_id: str | None = Field(default=None, pattern=r"^session_[0-9a-f]{32}$")
    adapter_kind: Literal["pinned-sdk", "custom"]
    limits: RunLimits
    limit_report: list[LimitReportItem] = Field(max_length=64)
    requested_capabilities: RequestedCapabilities | None


class ArtifactPaths(StrictModel):
    run_directory: str = Field(min_length=1, max_length=4096)
    request: str = Field(min_length=1, max_length=4096)
    manifest: str | None = Field(default=None, min_length=1, max_length=4096)
    nodes_directory: str | None = Field(default=None, min_length=1, max_length=4096)
    iterations_directory: str | None = Field(default=None, min_length=1, max_length=4096)
    result: str = Field(min_length=1, max_length=4096)
    events: str = Field(min_length=1, max_length=4096)


class RunRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    task: str = Field(min_length=1, max_length=16_384)
    root_prompt: str | None = Field(default=None, max_length=16_384)
    context_root: str = Field(min_length=1, max_length=4096)
    state_directory: str = Field(min_length=1, max_length=4096)
    strategy: RunStrategy
    model: str | None = Field(default=None, min_length=1, max_length=255)
    sub_model: str | None = Field(default=None, min_length=1, max_length=255)
    allowed_models: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(
        default_factory=list, max_length=64
    )
    provider: OpenAICompatibleProvider | None = None
    reasoning_effort: str
    sub_reasoning_effort: str
    persistent: bool
    session_id: str | None = Field(default=None, pattern=r"^session_[0-9a-f]{32}$")
    compaction: bool
    compaction_threshold: float = Field(ge=0.5, le=0.95)
    orchestrator: bool
    custom_system_prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    user_prologue_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    root_tools_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sub_tools_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    include: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        default_factory=list, max_length=64
    )
    exclude: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        default_factory=list, max_length=64
    )
    limits: RunLimits


class CallResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    call_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    canonical_id: str = Field(pattern=r"^call_[0-9a-f]{32}$")
    requested_mode: CallMode
    executed_mode: CallMode
    status: CallStatus
    child_node_id: str | None = Field(default=None, pattern=r"^node_[0-9]{6}$")
    payload: FinalPayload | None = None
    value: Any | None = None
    error: RunError | None = None
    usage: RunUsage
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == CallStatus.succeeded:
            if self.error is not None:
                raise ValueError("successful call forbids error")
            if self.executed_mode == CallMode.tool and self.payload is not None:
                raise ValueError("successful tool call uses value, not payload")
            if self.executed_mode != CallMode.tool and self.payload is None:
                raise ValueError("successful delegation requires payload")
        elif self.error is None or self.payload is not None:
            raise ValueError("unsuccessful call requires error and forbids payload")
        return self


class ReplExecutionRecord(StrictModel):
    code: str = Field(min_length=1, max_length=1_048_576)
    stdout: str = Field(max_length=16_777_216)
    stderr: str = Field(max_length=16_777_216)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    variable_types: dict[
        Annotated[str, Field(min_length=1, max_length=128)],
        Annotated[str, Field(min_length=1, max_length=255)],
    ] = Field(default_factory=dict, max_length=4096)
    final_payload: FinalPayload | None = None
    duration_ms: int = Field(ge=0)


class IterationRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    node_id: str = Field(pattern=r"^node_[0-9]{6}$")
    iteration: int = Field(ge=0, le=101)
    phase: Literal["repl", "finalization"]
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executions: list[ReplExecutionRecord] = Field(default_factory=list, max_length=32)
    # Admission limits bound executed calls, but every rejected attempt is also recorded.
    results: list[CallResult] = Field(default_factory=list)
    error: RunError | None = None
    usage: RunUsage
    compaction_completed: bool = False


class NodeRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    node_id: str = Field(pattern=r"^node_[0-9]{6}$")
    parent_node_id: str | None = Field(default=None, pattern=r"^node_[0-9]{6}$")
    parent_call_id: str | None = Field(default=None, pattern=r"^call_[0-9a-f]{32}$")
    depth: int = Field(ge=0, le=16)
    requested_mode: CallMode
    executed_mode: CallMode
    task: str = Field(min_length=1, max_length=16_384)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    reasoning_effort: str = Field(min_length=1, max_length=32)
    status: NodeStatus
    thread_id: str | None = Field(default=None, min_length=1, max_length=1000)
    initial_prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    iterations: int = Field(ge=0, le=101)
    calls: int = Field(ge=0, le=1024)
    payload: FinalPayload | None = None
    best_partial_answer: str | None = Field(default=None, max_length=12_000)
    error: RunError | None = None
    usage: RunUsage


class SessionHistoryEntry(StrictModel):
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    context_version: int = Field(ge=0)
    task_preview: str = Field(min_length=1, max_length=2000)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RunStatus
    answer_preview: str | None = Field(default=None, max_length=4000)
    answer_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SessionRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    session_id: str = Field(pattern=r"^session_[0-9a-f]{32}$")
    context_root: str = Field(min_length=1, max_length=4096)
    root_thread_id: str | None = Field(default=None, min_length=1, max_length=1000)
    root_usage_total: TokenUsageBreakdown | None = None
    context_count: int = Field(ge=0)
    history: list[SessionHistoryEntry] = Field(default_factory=list, max_length=1000)
    created_at: datetime
    updated_at: datetime


class RunResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    strategy: RunStrategy
    status: RunStatus
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    payload: FinalPayload | None = None
    best_partial_answer: str | None = Field(default=None, max_length=12_000)
    error: RunError | None = None
    usage: RunUsage
    runtime: RuntimeMetadata
    artifacts: ArtifactPaths
    nodes: list[NodeRecord] = Field(default_factory=list, max_length=4096)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == RunStatus.succeeded:
            if self.payload is None or self.error is not None:
                raise ValueError("succeeded run requires payload and forbids error")
        elif self.status == RunStatus.partial:
            if self.error is None or (self.payload is None and self.best_partial_answer is None):
                raise ValueError("partial run requires an error and usable partial answer")
        elif self.payload is not None or self.error is None:
            raise ValueError("unsuccessful run requires error and forbids payload")
        return self


class BatchFailure(StrictModel):
    """Ordered top-level batch slot that failed before a RunResult existed."""

    schema_version: Literal["1.0"] = "1.0"
    task_preview: str = Field(max_length=2000)
    task_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    error: RunError
    duration_ms: int = Field(ge=0)


class RunEvent(StrictModel):
    event_schema_version: Literal["1.0"] = "1.0"
    sequence: int = Field(ge=1)
    event_id: str = Field(pattern=r"^evt_[0-9a-f]{32}$")
    timestamp: datetime
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    node_id: str | None = Field(default=None, pattern=r"^node_[0-9]{6}$")
    parent_node_id: str | None = Field(default=None, pattern=r"^node_[0-9]{6}$")
    iteration: int | None = Field(default=None, ge=0, le=101)
    call_id: str | None = Field(default=None, pattern=r"^call_[0-9a-f]{32}$")
    type: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict, max_length=64)
