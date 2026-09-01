from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, Sandbox
from openai_codex.types import ThreadItem

from rcodex.codex_adapter import (
    AdapterCleanupError,
    AdapterError,
    AdapterRuntimeError,
    AdapterTimeoutError,
)
from rcodex.codex_adapter.base import RootSessionRequest
from rcodex.codex_adapter.config import locked_down_config
from rcodex.codex_adapter.sdk import (
    SdkRootSession,
    _before_deadline,
    _developer_instructions,
    _open_thread,
    _OpenedThread,
)


class _FakeThread:
    def __init__(self, thread_id: str) -> None:
        self.id = thread_id

    async def compact(self) -> object:
        return object()


class _FakeCodex:
    last: ClassVar[_FakeCodex | None] = None

    def __init__(self, *, config: object) -> None:
        self.config = config
        self.metadata = SimpleNamespace(
            serverInfo=SimpleNamespace(version="0.151.0 (test; arm64) adapter")
        )
        self.started: list[dict[str, object]] = []
        self.resumed: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        type(self).last = self

    async def __aenter__(self) -> _FakeCodex:
        return self

    async def account(self) -> object:
        return SimpleNamespace(account=object())

    async def thread_start(self, **kwargs: object) -> _FakeThread:
        self.started.append(kwargs)
        return _FakeThread("thread-new")

    async def thread_resume(self, thread_id: str, **kwargs: object) -> _FakeThread:
        self.resumed.append((thread_id, kwargs))
        return _FakeThread(thread_id)

    async def close(self) -> None:
        self.closed = True


class _StartupAndCleanupFailureCodex(_FakeCodex):
    async def thread_start(self, **kwargs: object) -> _FakeThread:
        del kwargs
        raise RuntimeError("unsafe startup detail")

    async def close(self) -> None:
        self.closed = True
        raise RuntimeError("unsafe cleanup detail")


def _session_request(tmp_path: Path, **updates: Any) -> RootSessionRequest:
    values: dict[str, Any] = {
        "context_root": tmp_path,
        "model": "test-model",
        "reasoning_effort": "low",
        "timeout_seconds": 1.0,
        "cleanup_timeout_seconds": 1.0,
    }
    values.update(updates)
    return RootSessionRequest(**values)


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rcodex.codex_adapter.sdk.AsyncCodex", _FakeCodex)
    monkeypatch.setattr("rcodex.codex_adapter.sdk.process_config", lambda: object())


@pytest.mark.asyncio
@pytest.mark.parametrize(("persistent", "ephemeral"), [(False, True), (True, False)])
async def test_new_session_selects_ephemeral_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    persistent: bool,
    ephemeral: bool,
) -> None:
    _install_fake_sdk(monkeypatch)

    opened = await _open_thread(
        _session_request(tmp_path, persistent=persistent), "locked developer instructions"
    )

    fake = _FakeCodex.last
    assert fake is not None
    assert fake.resumed == []
    assert len(fake.started) == 1
    assert fake.started[0] == {
        "approval_mode": ApprovalMode.deny_all,
        "config": locked_down_config(),
        "cwd": str(tmp_path),
        "developer_instructions": "locked developer instructions",
        "ephemeral": ephemeral,
        "model": "test-model",
        "sandbox": Sandbox.read_only,
    }
    assert opened.thread.id == "thread-new"
    await fake.close()


@pytest.mark.asyncio
async def test_resume_reasserts_all_session_restrictions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_sdk(monkeypatch)

    opened = await _open_thread(
        _session_request(tmp_path, persistent=True, thread_id="thread-existing"),
        "locked developer instructions",
    )

    fake = _FakeCodex.last
    assert fake is not None
    assert fake.started == []
    assert fake.resumed == [
        (
            "thread-existing",
            {
                "approval_mode": ApprovalMode.deny_all,
                "config": locked_down_config(),
                "cwd": str(tmp_path),
                "developer_instructions": "locked developer instructions",
                "model": "test-model",
                "sandbox": Sandbox.read_only,
            },
        )
    ]
    assert opened.thread.id == "thread-existing"
    await fake.close()


@pytest.mark.asyncio
async def test_startup_cleanup_failure_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "rcodex.codex_adapter.sdk.AsyncCodex",
        _StartupAndCleanupFailureCodex,
    )
    monkeypatch.setattr("rcodex.codex_adapter.sdk.process_config", lambda: object())

    with pytest.raises(AdapterCleanupError) as captured:
        await _open_thread(_session_request(tmp_path), "locked developer instructions")

    assert str(captured.value) == "Codex SDK startup cleanup failed"
    assert "unsafe startup detail" not in str(captured.value)
    assert "unsafe cleanup detail" not in str(captured.value)


class _CompactClient:
    async def close(self) -> None:
        return


class _CompactThread:
    def __init__(self, action: str) -> None:
        self.id = "thread-compact"
        self.action = action
        self.called = False
        self.read_include_turns: list[bool] = []
        self.reads_after_compact = 0

    @staticmethod
    def _response(*identifiers: str) -> object:
        items = [
            ThreadItem.model_validate({"type": "contextCompaction", "id": identifier})
            for identifier in identifiers
        ]
        return SimpleNamespace(
            thread=SimpleNamespace(
                turns=[
                    SimpleNamespace(
                        status=SimpleNamespace(value="completed"),
                        items=items,
                    )
                ]
            )
        )

    async def read(self, *, include_turns: bool) -> object:
        self.read_include_turns.append(include_turns)
        if not self.called:
            return self._response("existing")
        self.reads_after_compact += 1
        if self.action == "complete" and self.reads_after_compact >= 2:
            return self._response("existing", "new")
        return self._response("existing")

    async def compact(self) -> object:
        self.called = True
        if self.action == "block":
            await asyncio.Event().wait()
        if self.action == "fail":
            raise ValueError("unsafe detail")
        return object()


def _root_session(thread: _CompactThread) -> SdkRootSession:
    return SdkRootSession(
        _OpenedThread(
            client=cast(AsyncCodex, _CompactClient()),
            thread=cast(AsyncThread, thread),
            codex_runtime_version="0.151.0",
        ),
        reasoning_effort="low",
    )


@pytest.mark.asyncio
async def test_session_compact_polls_public_read_until_new_compaction_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = _CompactThread("complete")
    session = _root_session(thread)
    deadlines: list[float] = []

    async def record_deadline(awaitable: Awaitable[Any], deadline: float) -> Any:
        deadlines.append(deadline)
        return await _before_deadline(awaitable, deadline)

    monkeypatch.setattr("rcodex.codex_adapter.sdk._before_deadline", record_deadline)

    await session.compact(timeout_seconds=0.2)

    assert thread.called
    assert thread.reads_after_compact == 2
    assert thread.read_include_turns == [True, True, True]
    assert len(deadlines) >= 5
    assert len(set(deadlines)) == 1


@pytest.mark.asyncio
async def test_session_compact_is_bounded() -> None:
    session = _root_session(_CompactThread("block"))

    with pytest.raises(AdapterTimeoutError, match="did not complete"):
        await session.compact(timeout_seconds=0.01)


@pytest.mark.asyncio
async def test_session_compact_sanitizes_sdk_failure() -> None:
    session = _root_session(_CompactThread("fail"))

    with pytest.raises(AdapterRuntimeError) as captured:
        await session.compact(timeout_seconds=0.1)

    assert captured.value.phase == "thread_compact"
    assert captured.value.error_type == "ValueError"
    assert "unsafe detail" not in str(captured.value)


@pytest.mark.asyncio
async def test_closed_session_cannot_be_compacted() -> None:
    session = _root_session(_CompactThread("complete"))
    await session.close(timeout_seconds=0.1)

    with pytest.raises(AdapterError, match="already closed"):
        await session.compact(timeout_seconds=0.1)


def test_custom_system_prompt_is_appended_as_developer_guidance() -> None:
    guidance = "Use the caller's domain vocabulary."
    instructions = _developer_instructions("root", guidance)

    assert instructions.endswith(guidance)
    assert "trusted task guidance" in instructions
    assert "cannot relax or override" in instructions
    assert instructions.index("Do not use web/connectors/MCP tools") < instructions.index(guidance)
