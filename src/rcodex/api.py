"""Python completion APIs for Recursive Codex."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rcodex.codex_adapter import SdkCodexAdapter
from rcodex.config import RunConfig
from rcodex.models import BatchFailure, RunError, RunErrorCode, RunResult, RunStrategy, RunUsage
from rcodex.prompts import prompt_sha256
from rcodex.run_inputs import default_state_directory
from rcodex.runner import (
    AdapterFactory,
    IterationComplete,
    IterationStart,
    RecursiveRunner,
    SubcallComplete,
    SubcallStart,
)

Prompt = str | Mapping[str, Any] | Sequence[Any]


class RLMError(RuntimeError):
    """A Recursive Codex completion did not produce a final answer."""

    def __init__(self, error: RunError, *, partial_answer: str | None = None) -> None:
        super().__init__(error.message)
        self.error = error
        self.partial_answer = partial_answer


class TimeoutExceededError(RLMError):
    """The completion exceeded its wall-clock deadline."""


class TokenLimitExceededError(RLMError):
    """Observed SDK token usage crossed the configured limit."""


class ErrorThresholdExceededError(RLMError):
    """A node reached its consecutive-error threshold."""


@dataclass(frozen=True, slots=True)
class RLMChatCompletion:
    """Small completion-facing projection with the full run trace attached."""

    prompt: Prompt
    response: str
    usage_summary: RunUsage
    execution_time: float
    run: RunResult | BatchFailure

    def to_dict(self) -> dict[str, Any]:
        try:
            prompt = json.loads(
                json.dumps(
                    self.prompt,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (TypeError, ValueError):
            prompt = {"unserializable_prompt_type": type(self.prompt).__name__}
        return {
            "prompt": prompt,
            "response": self.response,
            "usage_summary": self.usage_summary.model_dump(mode="json"),
            "execution_time": self.execution_time,
            "run": self.run.model_dump(mode="json"),
        }


def _normalize_prompt(prompt: Prompt) -> str:
    if isinstance(prompt, str):
        return prompt
    return json.dumps(prompt, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _raise_failure(result: RunResult) -> None:
    if result.error is None:
        raise AssertionError("failed result is missing its controller error")
    partial = result.payload.answer if result.payload is not None else result.best_partial_answer
    error_type: type[RLMError] = RLMError
    if result.error.code == RunErrorCode.timeout:
        error_type = TimeoutExceededError
    elif result.error.code == RunErrorCode.token_limit:
        error_type = TokenLimitExceededError
    elif result.error.code == RunErrorCode.error_limit:
        error_type = ErrorThresholdExceededError
    raise error_type(result.error, partial_answer=partial)


class AsyncRLM:
    """Reusable async Recursive Codex completion client."""

    def __init__(
        self,
        *,
        context: Path,
        state_directory: Path | None = None,
        config: RunConfig | None = None,
        session_id: str | None = None,
        custom_tools: Mapping[str, Any] | None = None,
        custom_sub_tools: Mapping[str, Any] | None = None,
        adapter_factory: AdapterFactory = SdkCodexAdapter,
        on_subcall_start: SubcallStart | None = None,
        on_subcall_complete: SubcallComplete | None = None,
        on_iteration_start: IterationStart | None = None,
        on_iteration_complete: IterationComplete | None = None,
    ) -> None:
        self.context = context
        self.state_directory = state_directory or default_state_directory(context)
        self.config = config or RunConfig()
        self.session_id = session_id
        self._create_session_if_missing = False
        if self.config.persistent and self.session_id is None:
            self.session_id = f"session_{uuid.uuid4().hex}"
            self._create_session_if_missing = True
        self._closed = False
        self._persistent_completion_guard = threading.Lock()
        self._runner = RecursiveRunner(
            adapter_factory,
            custom_tools=custom_tools,
            custom_sub_tools=custom_sub_tools,
            on_subcall_start=on_subcall_start,
            on_subcall_complete=on_subcall_complete,
            on_iteration_start=on_iteration_start,
            on_iteration_complete=on_iteration_complete,
        )

    async def completion(self, prompt: Prompt, root_prompt: str | None = None) -> RLMChatCompletion:
        if self._closed:
            raise RuntimeError("RLM is already closed")
        acquired = False
        if self.config.persistent:
            acquired = self._persistent_completion_guard.acquire(blocking=False)
            if not acquired:
                raise RuntimeError("a persistent completion is already active on this RLM instance")
        try:
            return await self._completion_unlocked(prompt, root_prompt)
        finally:
            if acquired:
                self._persistent_completion_guard.release()

    async def _completion_unlocked(
        self, prompt: Prompt, root_prompt: str | None
    ) -> RLMChatCompletion:
        task = _normalize_prompt(prompt)
        result, _ = await self._runner.run(
            task=task,
            root_prompt=root_prompt,
            context=self.context,
            state_directory=self.state_directory,
            config=self.config,
            strategy=RunStrategy.recursive,
            session_id=self.session_id,
            create_session=self._create_session_if_missing,
        )
        if result.runtime.session_id is not None:
            self.session_id = result.runtime.session_id
            self._create_session_if_missing = False
        if result.payload is None:
            _raise_failure(result)
        assert result.payload is not None
        return RLMChatCompletion(
            prompt=prompt,
            response=result.payload.answer,
            usage_summary=result.usage,
            execution_time=result.duration_ms / 1000,
            run=result,
        )

    async def direct_completion(self, prompt: Prompt) -> RLMChatCompletion:
        if self._closed:
            raise RuntimeError("RLM is already closed")
        if self.config.persistent:
            raise ValueError("direct_completion is unavailable for persistent sessions")
        result, _ = await self._runner.run(
            task=_normalize_prompt(prompt),
            context=self.context,
            state_directory=self.state_directory,
            config=self.config,
            strategy=RunStrategy.direct,
        )
        if result.payload is None:
            _raise_failure(result)
        assert result.payload is not None
        return RLMChatCompletion(
            prompt=prompt,
            response=result.payload.answer,
            usage_summary=result.usage,
            execution_time=result.duration_ms / 1000,
            run=result,
        )

    async def completion_batched(self, prompts: Sequence[Prompt]) -> list[RLMChatCompletion]:
        if self._closed:
            raise RuntimeError("RLM is already closed")
        if isinstance(prompts, (str, bytes)):
            raise ValueError("batched prompts must be a sequence of prompts, not one string")
        if len(prompts) > self.config.max_batch_size:
            raise ValueError("top-level batch exceeds max_batch_size")
        prepared: list[str | BatchFailure] = []
        normalized: list[str] = []
        for prompt in prompts:
            try:
                task = _normalize_prompt(prompt)
            except (TypeError, ValueError):
                description = (
                    prompt
                    if isinstance(prompt, str)
                    else f"<non-JSON prompt: {type(prompt).__name__}>"
                )
                prepared.append(
                    BatchFailure(
                        task_preview=description[:2000],
                        task_sha256=prompt_sha256(description),
                        error=RunError(
                            code=RunErrorCode.unsupported,
                            message="batch prompt is not JSON serializable",
                        ),
                        duration_ms=0,
                    )
                )
            else:
                prepared.append(task)
                normalized.append(task)
        results = await self._runner.run_batched(
            normalized,
            context=self.context,
            state_directory=self.state_directory,
            config=self.config,
            strategy=RunStrategy.recursive,
        )
        completions: list[RLMChatCompletion] = []
        result_iterator = iter(results)
        for prompt, prepared_slot in zip(prompts, prepared, strict=True):
            result = (
                prepared_slot if isinstance(prepared_slot, BatchFailure) else next(result_iterator)
            )
            if isinstance(result, BatchFailure):
                response = f"Error: {result.error.message}"
                usage = RunUsage(available=False)
                execution_time = result.duration_ms / 1000
            else:
                response = (
                    result.payload.answer
                    if result.payload is not None
                    else f"Error: {result.error.message if result.error is not None else 'unknown'}"
                )
                usage = result.usage
                execution_time = result.duration_ms / 1000
            completions.append(
                RLMChatCompletion(
                    prompt=prompt,
                    response=response,
                    usage_summary=usage,
                    execution_time=execution_time,
                    run=result,
                )
            )
        assert next(result_iterator, None) is None
        return completions

    async def close(self) -> None:
        self._closed = True

    async def __aenter__(self) -> AsyncRLM:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


class RLM:
    """Synchronous facade matching the reference RLM's completion style."""

    def __init__(self, **kwargs: Any) -> None:
        self._async = AsyncRLM(**kwargs)

    @property
    def session_id(self) -> str | None:
        return self._async.session_id

    def completion(self, prompt: Prompt, root_prompt: str | None = None) -> RLMChatCompletion:
        return asyncio.run(self._async.completion(prompt, root_prompt=root_prompt))

    def direct_completion(self, prompt: Prompt) -> RLMChatCompletion:
        return asyncio.run(self._async.direct_completion(prompt))

    def completion_batched(self, prompts: Sequence[Prompt]) -> list[RLMChatCompletion]:
        return asyncio.run(self._async.completion_batched(prompts))

    def close(self) -> None:
        asyncio.run(self._async.close())

    def __enter__(self) -> RLM:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
