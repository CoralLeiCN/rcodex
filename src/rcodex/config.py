"""Validated configuration for direct and recursive Codex runs."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from pydantic import ValidationError

from rcodex.codex_adapter.config import REASONING_EFFORTS
from rcodex.models import (
    LimitEnforcement,
    LimitReportItem,
    OpenAICompatibleProvider,
    RunLimits,
)

DEFAULT_RUN_TIMEOUT_SECONDS = 900.0
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 5.0
DEFAULT_NODE_TIMEOUT_SECONDS = 600.0
DEFAULT_LEAF_TIMEOUT_SECONDS = 300.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_MANIFEST_ENTRIES = 100_000
DEFAULT_MAX_CONTEXT_BYTES = 10 * 1024**3
DEFAULT_MAX_FINAL_RESULT_BYTES = 128 * 1024
DEFAULT_MAX_REPL_CODE_BYTES = 128 * 1024
DEFAULT_MAX_REPL_OUTPUT_BYTES = 256 * 1024
DEFAULT_REPL_MEMORY_BYTES = 512 * 1024**2
DEFAULT_REPL_CPU_SECONDS = 120
DEFAULT_MAX_TOOL_RESULT_BYTES = 64 * 1024
DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_ITERATIONS = 30
DEFAULT_MAX_CALLS_PER_ITERATION = 8
DEFAULT_MAX_CALLS_PER_NODE = 64
DEFAULT_MAX_TOTAL_NODES = 128
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_MAX_BATCH_SIZE = 256
DEFAULT_COMPACTION_THRESHOLD = 0.85
MAX_GUIDANCE_CHARS = 16_384
MAX_GUIDANCE_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Controller policy shared by the Python API and CLI."""

    model: str | None = None
    sub_model: str | None = None
    allowed_models: tuple[str, ...] = ()
    provider_base_url: str | None = None
    reasoning_effort: str = "low"
    sub_reasoning_effort: str | None = None
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS
    cleanup_timeout_seconds: float = DEFAULT_CLEANUP_TIMEOUT_SECONDS
    node_timeout_seconds: float = DEFAULT_NODE_TIMEOUT_SECONDS
    leaf_timeout_seconds: float = DEFAULT_LEAF_TIMEOUT_SECONDS
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    max_manifest_entries: int = DEFAULT_MAX_MANIFEST_ENTRIES
    max_context_bytes: int = DEFAULT_MAX_CONTEXT_BYTES
    max_final_result_bytes: int = DEFAULT_MAX_FINAL_RESULT_BYTES
    max_repl_code_bytes: int = DEFAULT_MAX_REPL_CODE_BYTES
    max_repl_output_bytes: int = DEFAULT_MAX_REPL_OUTPUT_BYTES
    repl_memory_bytes: int = DEFAULT_REPL_MEMORY_BYTES
    repl_cpu_seconds: int = DEFAULT_REPL_CPU_SECONDS
    max_tool_result_bytes: int = DEFAULT_MAX_TOOL_RESULT_BYTES
    max_depth: int = DEFAULT_MAX_DEPTH
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_calls_per_iteration: int = DEFAULT_MAX_CALLS_PER_ITERATION
    max_calls_per_node: int = DEFAULT_MAX_CALLS_PER_NODE
    max_total_nodes: int = DEFAULT_MAX_TOTAL_NODES
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE
    max_errors: int | None = None
    max_tokens: int | None = None
    persistent: bool = False
    compaction: bool = False
    compaction_threshold: float = DEFAULT_COMPACTION_THRESHOLD
    custom_system_prompt: str | None = None
    orchestrator: bool = True
    user_prologue: str | None = None
    verbose: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        text_fields = (
            ("model", self.model),
            ("sub_model", self.sub_model),
            ("provider_base_url", self.provider_base_url),
            ("sub_reasoning_effort", self.sub_reasoning_effort),
            ("custom_system_prompt", self.custom_system_prompt),
            ("user_prologue", self.user_prologue),
        )
        for name, value in text_fields:
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be blank")
            if value is not None:
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ValueError(f"{name} must be valid UTF-8") from exc
            if value is not None and name in {"model", "sub_model"} and len(value) > 255:
                raise ValueError(f"{name} must not exceed 255 characters")
        if self.provider_base_url is not None:
            if self.model is None:
                raise ValueError("model is required when provider_base_url is configured")
        if self.provider_base_url is not None:
            OpenAICompatibleProvider(base_url=self.provider_base_url)
        for name, value in (
            ("custom_system_prompt", self.custom_system_prompt),
            ("user_prologue", self.user_prologue),
        ):
            if value is not None and (
                len(value) > MAX_GUIDANCE_CHARS or len(value.encode("utf-8")) > MAX_GUIDANCE_BYTES
            ):
                raise ValueError(
                    f"{name} must not exceed {MAX_GUIDANCE_CHARS} characters "
                    f"or {MAX_GUIDANCE_BYTES} UTF-8 bytes"
                )
        efforts = (self.reasoning_effort, self.resolved_sub_reasoning_effort)
        if any(effort not in REASONING_EFFORTS for effort in efforts):
            raise ValueError(f"reasoning effort must be one of: {', '.join(REASONING_EFFORTS)}")
        if len(self.allowed_models) > 64 or any(
            not item.strip() or len(item) > 255 for item in self.allowed_models
        ):
            raise ValueError("allowed_models accepts at most 64 non-blank model names")
        try:
            for item in self.allowed_models:
                item.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("allowed_models must contain valid UTF-8") from exc
        if len(set(self.allowed_models)) != len(self.allowed_models):
            raise ValueError("allowed_models must be unique")
        if self.allowed_models:
            configured = {item for item in (self.model, self.sub_model) if item is not None}
            if not configured.issubset(self.allowed_models):
                raise ValueError("model and sub_model must be present in allowed_models")
        if len(self.include) > 64 or len(self.exclude) > 64:
            raise ValueError("include and exclude accept at most 64 patterns each")
        if any(not pattern.strip() for pattern in (*self.include, *self.exclude)):
            raise ValueError("include and exclude patterns must not be blank")
        if any(len(pattern) > 4096 for pattern in (*self.include, *self.exclude)):
            raise ValueError("include and exclude patterns must not exceed 4096 characters")
        try:
            for pattern in (*self.include, *self.exclude):
                pattern.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("include and exclude patterns must be valid UTF-8") from exc
        if not 0.5 <= self.compaction_threshold <= 0.95:
            raise ValueError("compaction_threshold must be between 0.5 and 0.95")
        try:
            self.limits()
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

    @property
    def resolved_sub_model(self) -> str | None:
        return self.sub_model if self.sub_model is not None else self.model

    @property
    def resolved_sub_reasoning_effort(self) -> str:
        return (
            self.reasoning_effort
            if self.sub_reasoning_effort is None
            else self.sub_reasoning_effort
        )

    @property
    def provider(self) -> OpenAICompatibleProvider | None:
        if self.provider_base_url is None:
            return None
        return OpenAICompatibleProvider(base_url=self.provider_base_url)

    def resolve_requested_model(self, requested: str | None) -> str | None:
        if requested is None:
            return self.resolved_sub_model
        if self.allowed_models and requested not in self.allowed_models:
            raise ValueError("requested model is not in the controller allowlist")
        if not self.allowed_models and requested not in {self.model, self.resolved_sub_model}:
            raise ValueError("per-call model overrides require allowed_models")
        return requested

    def resolve_requested_effort(self, requested: str | None) -> str:
        if requested is None:
            return self.resolved_sub_reasoning_effort
        if requested not in REASONING_EFFORTS:
            raise ValueError("requested reasoning effort is not supported")
        return requested

    def limits(self) -> RunLimits:
        return RunLimits(
            run_timeout_seconds=self.run_timeout_seconds,
            cleanup_timeout_seconds=self.cleanup_timeout_seconds,
            node_timeout_seconds=self.node_timeout_seconds,
            leaf_timeout_seconds=self.leaf_timeout_seconds,
            tool_timeout_seconds=self.tool_timeout_seconds,
            max_manifest_entries=self.max_manifest_entries,
            max_context_bytes=self.max_context_bytes,
            max_final_result_bytes=self.max_final_result_bytes,
            max_repl_code_bytes=self.max_repl_code_bytes,
            max_repl_output_bytes=self.max_repl_output_bytes,
            repl_memory_bytes=self.repl_memory_bytes,
            repl_cpu_seconds=self.repl_cpu_seconds,
            max_tool_result_bytes=self.max_tool_result_bytes,
            max_depth=self.max_depth,
            max_iterations=self.max_iterations,
            max_calls_per_iteration=self.max_calls_per_iteration,
            max_calls_per_node=self.max_calls_per_node,
            max_total_nodes=self.max_total_nodes,
            max_concurrency=self.max_concurrency,
            max_batch_size=self.max_batch_size,
            max_errors=self.max_errors,
            max_tokens=self.max_tokens,
        )

    def limit_report(self, *, observed_tokens: int | None = None) -> list[LimitReportItem]:
        retained_session_cap = (
            0 if self.max_depth == 0 else 1 + self.max_concurrency * max(0, self.max_depth - 1)
        )
        live_process_upper_bound = min(
            self.max_total_nodes,
            retained_session_cap + self.max_concurrency,
        )
        hard = (
            ("run_timeout_seconds", self.run_timeout_seconds, "monotonic whole-run deadline"),
            ("node_timeout_seconds", self.node_timeout_seconds, "per recursive node deadline"),
            ("leaf_timeout_seconds", self.leaf_timeout_seconds, "per terminal leaf deadline"),
            ("max_depth", self.max_depth, "recursive requests become leaves at the boundary"),
            ("max_iterations", self.max_iterations, "REPL turns before one finalization turn"),
            (
                "max_calls_per_iteration",
                self.max_calls_per_iteration,
                "controller-backed calls admitted during one REPL turn",
            ),
            ("max_calls_per_node", self.max_calls_per_node, "admitted calls over one node"),
            ("max_total_nodes", self.max_total_nodes, "controller-reserved run-wide nodes"),
            ("max_concurrency", self.max_concurrency, "active Codex turns"),
            ("max_batch_size", self.max_batch_size, "top-level completion batch admission"),
            ("max_repl_code_bytes", self.max_repl_code_bytes, "raw root code response bound"),
            (
                "max_repl_output_bytes",
                self.max_repl_output_bytes,
                "captured stdout and stderr bound",
            ),
            ("repl_cpu_seconds", self.repl_cpu_seconds, "isolated worker CPU limit"),
            (
                "retained_session_cap",
                retained_session_cap,
                "one root plus max_concurrency queued per deeper retained depth",
            ),
            (
                "live_codex_process_upper_bound",
                live_process_upper_bound,
                "retained sessions plus active leaves, also bounded by max_total_nodes",
            ),
        )
        report = [
            LimitReportItem(
                name=name,
                enforcement=LimitEnforcement.hard,
                configured=value,
                note=note,
            )
            for name, value, note in hard
        ]
        report.append(
            LimitReportItem(
                name="repl_memory_bytes",
                enforcement=(
                    LimitEnforcement.unsupported
                    if sys.platform == "darwin"
                    else LimitEnforcement.hard
                ),
                configured=self.repl_memory_bytes,
                note=(
                    "macOS rejects a practical RLIMIT_AS below Python's initial virtual mappings"
                    if sys.platform == "darwin"
                    else "isolated worker address-space limit"
                ),
            )
        )
        report.append(
            LimitReportItem(
                name="max_tokens",
                enforcement=LimitEnforcement.observed,
                configured=self.max_tokens,
                observed=observed_tokens,
                note="checked from SDK usage after each completed turn; one turn may overshoot",
            )
        )
        report.append(
            LimitReportItem(
                name="max_budget_usd",
                enforcement=LimitEnforcement.unsupported,
                configured=None,
                note="the Codex SDK does not expose per-turn monetary cost",
            )
        )
        return report
