from __future__ import annotations

import os
from pathlib import Path

import pytest

from rcodex.cli import _load_cli_environment, build_parser


def test_run_parser_exposes_current_recursive_controls() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--task",
            "solve",
            "--context",
            ".",
            "--strategy",
            "recursive",
            "--sub-model",
            "child-model",
            "--allow-model",
            "child-model",
            "--provider-base-url",
            "http://127.0.0.1:8000/v1",
            "--max-depth",
            "4",
            "--max-iterations",
            "7",
            "--max-total-nodes",
            "20",
            "--max-concurrency",
            "1",
            "--max-query-context-bytes",
            "20000",
            "--max-total-child-context-bytes",
            "60000",
            "--max-repl-code-bytes",
            "4096",
            "--max-repl-output-bytes",
            "8192",
            "--repl-memory-bytes",
            "67108864",
            "--repl-cpu-seconds",
            "10",
            "--persistent",
            "--compaction",
            "--compaction-threshold",
            "0.8",
        ]
    )

    assert args.strategy == "recursive"
    assert args.sub_model == "child-model"
    assert args.allow_model == ["child-model"]
    assert args.provider_base_url == "http://127.0.0.1:8000/v1"
    assert args.max_depth == 4
    assert args.max_iterations == 7
    assert args.max_total_nodes == 20
    assert args.max_concurrency == 1
    assert args.max_query_context_bytes == 20000
    assert args.max_total_child_context_bytes == 60000
    assert args.max_repl_code_bytes == 4096
    assert args.max_repl_output_bytes == 8192
    assert args.repl_memory_bytes == 67_108_864
    assert args.repl_cpu_seconds == 10
    assert args.persistent
    assert args.compaction
    assert args.compaction_threshold == 0.8


def test_dotenv_supplies_provider_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            (
                "RCODEX_MODEL=dotenv-model",
                "RCODEX_SUB_MODEL=dotenv-child-model",
                "RCODEX_PROVIDER_BASE_URL=https://provider.example.com/v1",
                "RCODEX_PROVIDER_API_KEY=dotenv-secret",
            )
        ),
        encoding="utf-8",
    )
    for name in (
        "RCODEX_MODEL",
        "RCODEX_SUB_MODEL",
        "RCODEX_PROVIDER_BASE_URL",
        "RCODEX_PROVIDER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    _load_cli_environment()
    args = build_parser().parse_args(["run", "--task", "solve", "--context", "."])

    assert args.model == "dotenv-model"
    assert args.sub_model == "dotenv-child-model"
    assert args.provider_base_url == "https://provider.example.com/v1"
    assert os.environ["RCODEX_PROVIDER_API_KEY"] == "dotenv-secret"


def test_cli_values_override_dotenv_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCODEX_MODEL", "dotenv-model")
    monkeypatch.setenv("RCODEX_PROVIDER_BASE_URL", "https://dotenv.example.com/v1")

    args = build_parser().parse_args(
        [
            "run",
            "--task",
            "solve",
            "--context",
            ".",
            "--model",
            "cli-model",
            "--provider-base-url",
            "https://cli.example.com/v1",
        ]
    )

    assert args.model == "cli-model"
    assert args.provider_base_url == "https://cli.example.com/v1"
