from __future__ import annotations

import asyncio
from typing import cast

import pytest
from openai_codex import AsyncCodex, AsyncThread, TurnResult

from rcodex.codex_adapter import AdapterCleanupError
from rcodex.codex_adapter.sdk import _close_opened, _OpenedThread


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _opened(
    client: _FakeClient, collector: asyncio.Task[None]
) -> tuple[_OpenedThread, asyncio.Task[TurnResult]]:
    typed_collector = cast(asyncio.Task[TurnResult], collector)
    return (
        _OpenedThread(
            client=cast(AsyncCodex, client),
            thread=cast(AsyncThread, object()),
            codex_runtime_version=None,
            collectors=[typed_collector],
        ),
        typed_collector,
    )


@pytest.mark.asyncio
async def test_close_cancels_and_drains_pending_collector() -> None:
    client = _FakeClient()
    started = asyncio.Event()
    cancellation_observed = asyncio.Event()

    async def collect() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cancellation_observed.set()

    opened, collector = _opened(client, asyncio.create_task(collect()))
    await started.wait()

    await _close_opened(opened, timeout_seconds=0.1)

    assert client.closed
    assert cancellation_observed.is_set()
    assert collector.cancelled()
    assert opened.collectors == []


@pytest.mark.asyncio
async def test_close_retains_collector_that_survives_cancellation() -> None:
    client = _FakeClient()
    started = asyncio.Event()
    cancellation_observed = asyncio.Event()
    release = asyncio.Event()

    async def collect() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_observed.set()
            await release.wait()

    opened, collector = _opened(client, asyncio.create_task(collect()))
    await started.wait()

    with pytest.raises(AdapterCleanupError, match="collector survived"):
        await _close_opened(opened, timeout_seconds=0.01)

    assert client.closed
    assert cancellation_observed.is_set()
    assert opened.collectors == [collector]

    release.set()
    await collector
