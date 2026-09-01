from __future__ import annotations

from rcodex.cli import build_parser


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
            "--max-depth",
            "4",
            "--max-iterations",
            "7",
            "--max-total-nodes",
            "20",
            "--max-concurrency",
            "1",
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
    assert args.max_depth == 4
    assert args.max_iterations == 7
    assert args.max_total_nodes == 20
    assert args.max_concurrency == 1
    assert args.max_repl_code_bytes == 4096
    assert args.max_repl_output_bytes == 8192
    assert args.repl_memory_bytes == 67_108_864
    assert args.repl_cpu_seconds == 10
    assert args.persistent
    assert args.compaction
    assert args.compaction_threshold == 0.8
