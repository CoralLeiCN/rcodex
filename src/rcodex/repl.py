"""Persistent restricted Python REPL with controller-backed query functions."""

from __future__ import annotations

import asyncio
import json
import os
import resource
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rcodex.models import CallMode, CallResult
from rcodex.repl_context import CONTEXT_MESSAGE_BYTES, ContextAccessError

QueryHandler = Callable[
    [CallMode, list[str], str | None, list[str | None]],
    Awaitable[tuple[list[str], list[CallResult]]],
]
ToolHandler = Callable[[str, dict[str, Any]], Awaitable[tuple[Any, CallResult]]]
ContextHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ReplError(RuntimeError):
    """The isolated REPL failed or violated its protocol."""


class ReplTimeoutError(ReplError):
    """The isolated REPL exceeded the controller-supplied deadline."""


@dataclass(frozen=True, slots=True)
class ReplExecution:
    code: str
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    variable_types: dict[str, str]
    final_payload: Any | None
    duration_ms: int
    calls: list[CallResult] = field(default_factory=list)


def _worker_limits(memory_bytes: int, cpu_seconds: int) -> Callable[[], None]:
    def apply() -> None:
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))
        # macOS starts Python with virtual mappings larger than a practical REPL cap and rejects
        # lowering RLIMIT_AS in preexec_fn. Linux enforces this address-space ceiling normally.
        if sys.platform != "darwin" and hasattr(resource, "RLIMIT_AS"):
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))

    return apply


class ReplSession:
    """One persistent worker namespace owned by one recursive Codex node."""

    def __init__(
        self,
        *,
        process: asyncio.subprocess.Process,
        temporary_directory: tempfile.TemporaryDirectory[str],
        max_message_bytes: int,
        max_query_context_bytes: int,
        context_handler: ContextHandler,
    ) -> None:
        self._process = process
        self._temporary_directory = temporary_directory
        self._max_message_bytes = max_message_bytes
        self._max_query_context_bytes = max_query_context_bytes
        self._context_handler = context_handler
        self._closed = False
        self._lock = asyncio.Lock()

    @classmethod
    async def start(
        cls,
        *,
        tool_names: list[str],
        max_output_bytes: int,
        max_message_bytes: int,
        max_query_context_bytes: int,
        memory_bytes: int,
        cpu_seconds: int,
        timeout_seconds: float,
        context_handler: ContextHandler,
    ) -> ReplSession:
        temporary = tempfile.TemporaryDirectory(prefix="rcodex-repl-")
        worker_path = Path(__file__).with_name("_repl_worker.py")
        # JSON can escape each context byte sixfold. Keep ordinary protocol overhead separate.
        message_limit = max(CONTEXT_MESSAGE_BYTES, max_message_bytes) + 6 * max_query_context_bytes
        environment = {
            "LANG": "C.UTF-8",
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-u",
                str(worker_path),
                cwd=temporary.name,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=_worker_limits(memory_bytes, cpu_seconds),
                limit=message_limit,
            )
            session = cls(
                process=process,
                temporary_directory=temporary,
                max_message_bytes=message_limit,
                max_query_context_bytes=max_query_context_bytes,
                context_handler=context_handler,
            )
            await session._write(
                {
                    "type": "initialize",
                    "tool_names": tool_names,
                    "max_output_bytes": max_output_bytes,
                    "max_query_context_bytes": max_query_context_bytes,
                }
            )
            ready = await asyncio.wait_for(session._read(), timeout=timeout_seconds)
            if ready.get("type") != "ready":
                raise ReplError("REPL worker did not complete initialization")
            return session
        except BaseException:
            temporary.cleanup()
            if "process" in locals() and process.returncode is None:
                process.kill()
                await process.wait()
            raise

    async def _write(self, message: dict[str, Any]) -> None:
        if self._process.stdin is None:
            raise ReplError("REPL worker stdin is unavailable")
        try:
            encoded = (
                json.dumps(
                    message,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise ReplError("REPL protocol value is not strict JSON") from exc
        if len(encoded) > self._max_message_bytes:
            raise ReplError("REPL protocol message exceeds its byte limit")
        self._process.stdin.write(encoded)
        await self._process.stdin.drain()

    async def _read(self) -> dict[str, Any]:
        if self._process.stdout is None:
            raise ReplError("REPL worker stdout is unavailable")
        try:
            raw = await self._process.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise ReplError("REPL worker response exceeds its byte limit") from exc
        if not raw:
            detail = ""
            if self._process.stderr is not None:
                error = await self._process.stderr.read()
                detail = error[:1000].decode("utf-8", errors="replace")
            suffix = f": {detail}" if detail else ""
            raise ReplError(f"REPL worker exited unexpectedly{suffix}")
        if len(raw) > self._max_message_bytes:
            raise ReplError("REPL worker response exceeds its byte limit")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReplError("REPL worker returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ReplError("REPL worker response must be an object")
        return value

    async def execute(
        self,
        code: str,
        *,
        timeout_seconds: float,
        query_handler: QueryHandler,
        tool_handler: ToolHandler,
    ) -> ReplExecution:
        if self._closed:
            raise ReplError("REPL session is closed")
        calls: list[CallResult] = []
        async with self._lock:
            try:
                await self._write({"type": "execute", "code": code})

                async def run() -> ReplExecution:
                    while True:
                        message = await self._read()
                        message_type = message.get("type")
                        if message_type == "context":
                            request_id = message.get("request_id")
                            arguments = message.get("arguments")
                            if not isinstance(request_id, str) or not isinstance(arguments, dict):
                                raise ReplError("invalid context RPC request")
                            try:
                                value = await self._context_handler(arguments)
                            except ContextAccessError as exc:
                                await self._write(
                                    {
                                        "type": "rpc_result",
                                        "request_id": request_id,
                                        "error": str(exc),
                                    }
                                )
                            else:
                                await self._write(
                                    {"type": "rpc_result", "request_id": request_id, "value": value}
                                )
                            continue
                        if message_type == "query":
                            request_id = message.get("request_id")
                            prompt_count = 1
                            try:
                                raw_mode = message.get("mode")
                                if not isinstance(raw_mode, str):
                                    raise ValueError("query mode must be a string")
                                mode = CallMode(raw_mode)
                                prompts = message.get("prompts")
                                model = message.get("model")
                                contexts = message.get("contexts")
                                if not isinstance(request_id, str):
                                    raise ValueError("query request_id must be a string")
                                if not isinstance(prompts, list) or not all(
                                    isinstance(prompt, str) for prompt in prompts
                                ):
                                    raise ValueError("query prompts must be strings")
                                prompt_count = len(prompts)
                                if model is not None and not isinstance(model, str):
                                    raise ValueError("query model must be a string or None")
                                if (
                                    not isinstance(contexts, list)
                                    or len(contexts) != len(prompts)
                                    or not all(
                                        context is None or isinstance(context, str)
                                        for context in contexts
                                    )
                                ):
                                    raise ValueError(
                                        "contexts must contain text or None per prompt"
                                    )
                                if mode != CallMode.recursive and any(
                                    context is not None for context in contexts
                                ):
                                    raise ValueError(
                                        "only recursive queries accept supplied context"
                                    )
                                total = 0
                                for context in contexts:
                                    if context is not None:
                                        if "\x00" in context:
                                            raise ValueError(
                                                "context must not contain NUL characters"
                                            )
                                        try:
                                            total += len(context.encode("utf-8"))
                                        except UnicodeEncodeError:
                                            raise ValueError(
                                                "context must be valid UTF-8 text"
                                            ) from None
                                if total > self._max_query_context_bytes:
                                    raise ValueError("contexts exceed max_query_context_bytes")
                                values, results = await query_handler(
                                    mode, prompts, model, contexts
                                )
                                calls.extend(results)
                                await self._write(
                                    {
                                        "type": "rpc_result",
                                        "request_id": request_id,
                                        "values": values,
                                    }
                                )
                            except Exception as exc:
                                await self._write(
                                    {
                                        "type": "rpc_result",
                                        "request_id": request_id,
                                        "values": [f"Error: {type(exc).__name__}: {exc}"]
                                        * prompt_count,
                                    }
                                )
                            continue
                        if message_type == "tool":
                            request_id = message.get("request_id")
                            try:
                                name = message.get("name")
                                arguments = message.get("arguments")
                                if not isinstance(request_id, str) or not isinstance(name, str):
                                    raise ValueError("tool request identifiers must be strings")
                                if not isinstance(arguments, dict):
                                    raise ValueError("tool arguments must be an object")
                                value, result = await tool_handler(name, arguments)
                                calls.append(result)
                                await self._write(
                                    {
                                        "type": "rpc_result",
                                        "request_id": request_id,
                                        "value": value,
                                        "error": (
                                            result.error.message
                                            if result.error is not None
                                            else None
                                        ),
                                    }
                                )
                            except Exception as exc:
                                await self._write(
                                    {
                                        "type": "rpc_result",
                                        "request_id": request_id,
                                        "error": f"{type(exc).__name__}: {exc}",
                                    }
                                )
                            continue
                        if message_type == "execution_result":
                            variables = message.get("variable_types")
                            if not isinstance(variables, dict) or not all(
                                isinstance(key, str) and isinstance(value, str)
                                for key, value in variables.items()
                            ):
                                raise ReplError("REPL worker returned invalid variable metadata")
                            return ReplExecution(
                                code=code,
                                stdout=str(message.get("stdout", "")),
                                stderr=str(message.get("stderr", "")),
                                stdout_truncated=bool(message.get("stdout_truncated", False)),
                                stderr_truncated=bool(message.get("stderr_truncated", False)),
                                variable_types=variables,
                                final_payload=message.get("final_payload"),
                                duration_ms=int(message.get("duration_ms", 0)),
                                calls=calls,
                            )
                        if message_type in {"worker_error", "protocol_error"}:
                            raise ReplError(str(message.get("error", "REPL worker failure")))
                        raise ReplError("REPL worker returned an unknown message type")

                return await asyncio.wait_for(run(), timeout=timeout_seconds)
            except TimeoutError as exc:
                await self._terminate()
                raise ReplTimeoutError("REPL execution exceeded its deadline") from exc
            except BaseException:
                await self._terminate()
                raise

    async def _terminate(self) -> None:
        if self._process.returncode is None:
            self._process.kill()
            await self._process.wait()
        self._closed = True
        self._temporary_directory.cleanup()

    async def close(self, timeout_seconds: float) -> None:
        if self._closed:
            return
        try:
            await self._write({"type": "close"})
            response = await asyncio.wait_for(self._read(), timeout=timeout_seconds)
            if response.get("type") != "closed":
                raise ReplError("REPL worker did not acknowledge close")
            await asyncio.wait_for(self._process.wait(), timeout=timeout_seconds)
        except BaseException:
            await self._terminate()
            raise
        finally:
            self._closed = True
            self._temporary_directory.cleanup()
