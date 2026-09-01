from __future__ import annotations

import os
from pathlib import Path

import pytest

from rcodex import AsyncRLM, RunConfig
from rcodex.models import RunResult, RunStatus

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RCODEX_LIVE_TESTS") != "1",
        reason="set RCODEX_LIVE_TESTS=1 to run authenticated Codex smoke tests",
    ),
]


def _context(tmp_path: Path) -> tuple[Path, str]:
    marker = "rcodex-live-marker-7f36b1"
    root = tmp_path / "context"
    root.mkdir()
    (root / "marker.txt").write_text(marker + "\n", encoding="utf-8")
    return root, marker


def _live_config(*, persistent: bool = False) -> RunConfig:
    return RunConfig(
        persistent=persistent,
        run_timeout_seconds=180,
        node_timeout_seconds=120,
        leaf_timeout_seconds=120,
        cleanup_timeout_seconds=5,
        max_depth=1,
        max_iterations=3,
        max_concurrency=1,
    )


@pytest.mark.asyncio
async def test_live_direct_reads_caller_context(tmp_path: Path) -> None:
    context, marker = _context(tmp_path)
    async with AsyncRLM(
        context=context,
        state_directory=tmp_path / "state",
        config=_live_config(),
    ) as client:
        completion = await client.direct_completion(
            "Read marker.txt and include its exact complete marker in your answer."
        )

    assert marker in completion.response
    assert isinstance(completion.run, RunResult)
    assert completion.run.status == RunStatus.succeeded


@pytest.mark.asyncio
async def test_live_recursive_persistent_resume_reuses_root_thread(tmp_path: Path) -> None:
    context, marker = _context(tmp_path)
    async with AsyncRLM(
        context=context,
        state_directory=tmp_path / "state",
        config=_live_config(persistent=True),
    ) as client:
        first = await client.completion(
            "Read marker.txt and include its exact complete marker in your answer."
        )
        second = await client.completion(
            "Repeat the exact marker from the preceding persistent context."
        )

    assert marker in first.response
    assert marker in second.response
    assert isinstance(first.run, RunResult)
    assert isinstance(second.run, RunResult)
    assert first.run.runtime.session_id == second.run.runtime.session_id
    assert first.run.runtime.root_thread_id == second.run.runtime.root_thread_id
