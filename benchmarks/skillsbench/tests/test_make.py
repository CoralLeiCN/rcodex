"""Keep development selection bounded and use each agent's supported effort setting."""

import json
import shlex
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("agent", ["rcodex", "codex", "oracle"])
@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("timeout", [None, "1200"])
def test_official_cli_selection_and_effort(
    agent: str, full: bool, timeout: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TIMEOUT", raising=False)
    target = f"bench-{'all-' if full else ''}{agent}"
    output = subprocess.check_output(
        [
            "make",
            "--no-print-directory",
            "-f",
            "benchmarks/skillsbench/Makefile",
            "-n",
            target,
            "TASK=dialogue-parser",
            "REASONING_EFFORT=low",
            *([f"TIMEOUT={timeout}"] if timeout else []),
        ],
        cwd=Path(__file__).parents[3],
        text=True,
    )
    args = shlex.split(output.replace("\\\n", ""))
    assert args[args.index("eval") + 1] == "run"
    assert args[args.index("--agent") + 1] == agent
    assert args[args.index("--retry-attempts") + 1] == "0"
    assert ("--expected-tasks" in args) is not full
    if not full:
        assert args[args.index("--expected-tasks") + 1] == "1"
        assert args[args.index("--tasks-dir") + 1].endswith("/tasks/dialogue-parser")
    assert ("--reasoning-effort" in args) == (agent == "codex")
    assert ("RCODEX_BENCH_REASONING_EFFORT=low" in args) == (agent == "rcodex")
    assert args[args.index("--jobs-dir") + 1].endswith("/" + target)
    assert ("--config-override" in args) == (timeout is not None)
    if timeout is not None:
        override = json.loads(args[args.index("--config-override") + 1])
        assert override == {"agent": {"timeout_sec": int(timeout)}}
    assert not any(arg.startswith("RCODEX_BENCH_TIMEOUT=") for arg in args)


@pytest.mark.parametrize("full", [False, True])
def test_combined_run_keeps_skillsbench_makefile(full: bool) -> None:
    output = subprocess.check_output(
        [
            "make",
            "--no-print-directory",
            "-n",
            "-f",
            "benchmarks/skillsbench/Makefile",
            "bench-all" if full else "bench",
            "TASK=dialogue-parser",
            "TIMEOUT=900",
        ],
        cwd=Path(__file__).parents[3],
        text=True,
    )
    commands = [
        shlex.split(line)
        for line in output.replace("\\\n", "").splitlines()
        if " bench eval run " in line
    ]
    assert len(commands) == 2
    assert {args[args.index("--agent") + 1] for args in commands} == {"rcodex", "codex"}
    for args in commands:
        path = args[args.index("--tasks-dir") + 1]
        assert path.endswith("/tasks" if full else "/tasks/dialogue-parser")
        assert ("--expected-tasks" in args) is not full
        assert json.loads(args[args.index("--config-override") + 1]) == {
            "agent": {"timeout_sec": 900}
        }
