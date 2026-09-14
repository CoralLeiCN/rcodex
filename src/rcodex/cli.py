"""Command-line entry point for Recursive Codex."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from rcodex.codex_adapter.config import REASONING_EFFORTS
from rcodex.config import (
    DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_COMPACTION_THRESHOLD,
    DEFAULT_LEAF_TIMEOUT_SECONDS,
    DEFAULT_MAX_CALLS_PER_ITERATION,
    DEFAULT_MAX_CALLS_PER_NODE,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FINAL_RESULT_BYTES,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_MANIFEST_ENTRIES,
    DEFAULT_MAX_REPL_CODE_BYTES,
    DEFAULT_MAX_REPL_OUTPUT_BYTES,
    DEFAULT_MAX_TOOL_RESULT_BYTES,
    DEFAULT_MAX_TOTAL_NODES,
    DEFAULT_NODE_TIMEOUT_SECONDS,
    DEFAULT_REPL_CPU_SECONDS,
    DEFAULT_REPL_MEMORY_BYTES,
    DEFAULT_RUN_TIMEOUT_SECONDS,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    RunConfig,
)
from rcodex.models import RunStatus, RunStrategy
from rcodex.run_inputs import InvocationError, default_state_directory
from rcodex.runner import RecursiveRunner
from rcodex.storage import StorageError, atomic_write_bytes


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _optional_positive_int(value: str) -> int:
    return _positive_int(value)


def _duration(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([smh]?)", value.strip().lower())
    if match is None:
        raise argparse.ArgumentTypeError("use seconds or a duration such as 30s, 15m, or 1h")
    amount = float(match.group(1))
    multiplier = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0}[match.group(2)]
    seconds = amount * multiplier
    if seconds <= 0 or seconds > 86_400:
        raise argparse.ArgumentTypeError("duration must be greater than zero and at most 24h")
    return seconds


def _ratio(value: str) -> float:
    parsed = float(value)
    if not 0.5 <= parsed <= 0.95:
        raise argparse.ArgumentTypeError("ratio must be between 0.5 and 0.95")
    return parsed


def _load_cli_environment() -> None:
    """Load CLI defaults and provider credentials from the working directory."""

    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rcodex")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="run direct or recursive Codex inference")
    run.add_argument("--task", required=True, help="task for Codex")
    run.add_argument("--root-prompt", help="short prompt separately visible to the root node")
    run.add_argument("--context", required=True, type=Path, help="local context directory")
    run.add_argument(
        "--strategy",
        choices=tuple(strategy.value for strategy in RunStrategy),
        default=RunStrategy.recursive.value,
    )
    run.add_argument(
        "--model",
        default=os.environ.get("RCODEX_MODEL"),
        help="root Codex model; defaults to RCODEX_MODEL or the SDK default",
    )
    run.add_argument(
        "--sub-model",
        default=os.environ.get("RCODEX_SUB_MODEL"),
        help="default child model; defaults to RCODEX_SUB_MODEL or --model",
    )
    run.add_argument(
        "--allow-model",
        action="append",
        default=[],
        help="repeatable per-call model allowlist",
    )
    run.add_argument(
        "--provider-base-url",
        default=os.environ.get("RCODEX_PROVIDER_BASE_URL"),
        help=("OpenAI Responses-compatible API base URL; defaults to RCODEX_PROVIDER_BASE_URL"),
    )
    run.add_argument(
        "--reasoning-effort",
        choices=REASONING_EFFORTS,
        default="low",
    )
    run.add_argument("--sub-reasoning-effort", choices=REASONING_EFFORTS)
    run.add_argument("--timeout", type=_duration, default=DEFAULT_RUN_TIMEOUT_SECONDS)
    run.add_argument("--node-timeout", type=_duration, default=DEFAULT_NODE_TIMEOUT_SECONDS)
    run.add_argument("--leaf-timeout", type=_duration, default=DEFAULT_LEAF_TIMEOUT_SECONDS)
    run.add_argument("--tool-timeout", type=_duration, default=DEFAULT_TOOL_TIMEOUT_SECONDS)
    run.add_argument(
        "--cleanup-timeout",
        type=_duration,
        default=DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    )
    run.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    run.add_argument("--max-iterations", type=_positive_int, default=DEFAULT_MAX_ITERATIONS)
    run.add_argument(
        "--max-calls-per-iteration",
        type=_positive_int,
        default=DEFAULT_MAX_CALLS_PER_ITERATION,
    )
    run.add_argument(
        "--max-calls-per-node",
        type=_positive_int,
        default=DEFAULT_MAX_CALLS_PER_NODE,
    )
    run.add_argument("--max-total-nodes", type=_positive_int, default=DEFAULT_MAX_TOTAL_NODES)
    run.add_argument("--max-concurrency", type=_positive_int, default=DEFAULT_MAX_CONCURRENCY)
    run.add_argument("--max-errors", type=_optional_positive_int)
    run.add_argument("--max-tokens", type=_optional_positive_int)
    run.add_argument(
        "--max-manifest-entries",
        type=_positive_int,
        default=DEFAULT_MAX_MANIFEST_ENTRIES,
    )
    run.add_argument(
        "--max-context-bytes",
        type=_positive_int,
        default=DEFAULT_MAX_CONTEXT_BYTES,
    )
    run.add_argument(
        "--max-final-result-bytes",
        type=_positive_int,
        default=DEFAULT_MAX_FINAL_RESULT_BYTES,
    )
    run.add_argument(
        "--max-repl-code-bytes",
        type=_positive_int,
        default=DEFAULT_MAX_REPL_CODE_BYTES,
    )
    run.add_argument(
        "--max-repl-output-bytes",
        type=_positive_int,
        default=DEFAULT_MAX_REPL_OUTPUT_BYTES,
    )
    run.add_argument(
        "--repl-memory-bytes",
        type=_positive_int,
        default=DEFAULT_REPL_MEMORY_BYTES,
    )
    run.add_argument(
        "--repl-cpu-seconds",
        type=_positive_int,
        default=DEFAULT_REPL_CPU_SECONDS,
    )
    run.add_argument(
        "--max-tool-result-bytes",
        type=_positive_int,
        default=DEFAULT_MAX_TOOL_RESULT_BYTES,
    )
    run.add_argument("--persistent", action="store_true")
    run.add_argument(
        "--session",
        dest="session_id",
        help="resume a persistent session ID; requires --persistent",
    )
    run.add_argument("--compaction", action="store_true")
    run.add_argument(
        "--compaction-threshold",
        type=_ratio,
        default=DEFAULT_COMPACTION_THRESHOLD,
    )
    run.add_argument("--no-orchestrator", action="store_true")
    run.add_argument("--system-prompt", dest="custom_system_prompt")
    run.add_argument("--user-prologue")
    run.add_argument("--verbose", action="store_true")
    run.add_argument(
        "--state-dir",
        type=Path,
        help="artifact directory outside context (default: a context-keyed sibling directory)",
    )
    run.add_argument("--output", type=Path, help="write final result instead of stdout")
    run.add_argument("--include", action="append", default=[], metavar="GLOB")
    run.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    return parser


async def _run(args: argparse.Namespace) -> int:
    try:
        config = RunConfig(
            model=args.model,
            sub_model=args.sub_model,
            allowed_models=tuple(args.allow_model),
            provider_base_url=args.provider_base_url,
            reasoning_effort=args.reasoning_effort,
            sub_reasoning_effort=args.sub_reasoning_effort,
            run_timeout_seconds=args.timeout,
            cleanup_timeout_seconds=args.cleanup_timeout,
            node_timeout_seconds=args.node_timeout,
            leaf_timeout_seconds=args.leaf_timeout,
            tool_timeout_seconds=args.tool_timeout,
            max_manifest_entries=args.max_manifest_entries,
            max_context_bytes=args.max_context_bytes,
            max_final_result_bytes=args.max_final_result_bytes,
            max_repl_code_bytes=args.max_repl_code_bytes,
            max_repl_output_bytes=args.max_repl_output_bytes,
            repl_memory_bytes=args.repl_memory_bytes,
            repl_cpu_seconds=args.repl_cpu_seconds,
            max_tool_result_bytes=args.max_tool_result_bytes,
            max_depth=args.max_depth,
            max_iterations=args.max_iterations,
            max_calls_per_iteration=args.max_calls_per_iteration,
            max_calls_per_node=args.max_calls_per_node,
            max_total_nodes=args.max_total_nodes,
            max_concurrency=args.max_concurrency,
            max_errors=args.max_errors,
            max_tokens=args.max_tokens,
            persistent=args.persistent,
            compaction=args.compaction,
            compaction_threshold=args.compaction_threshold,
            custom_system_prompt=args.custom_system_prompt,
            orchestrator=not args.no_orchestrator,
            user_prologue=args.user_prologue,
            verbose=args.verbose,
            include=tuple(args.include),
            exclude=tuple(args.exclude),
        )
        result, output_path = await RecursiveRunner().run(
            task=args.task,
            root_prompt=args.root_prompt,
            context=args.context,
            state_directory=args.state_dir or default_state_directory(args.context),
            config=config,
            strategy=RunStrategy(args.strategy),
            session_id=args.session_id,
            output=args.output,
        )
    except (InvocationError, ValueError) as exc:
        print(f"rcodex: {exc}", file=sys.stderr)
        return 2
    except StorageError as exc:
        print(f"rcodex: {exc}", file=sys.stderr)
        return 1

    rendered = (
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    if result.runtime.session_id is not None:
        print(f"Persistent session: {result.runtime.session_id}", file=sys.stderr)
    try:
        if output_path is None:
            sys.stdout.write(rendered)
        else:
            atomic_write_bytes(
                output_path,
                rendered.encode("utf-8"),
                preserve_existing_mode=True,
                secure_parent=False,
            )
            print(f"Run result written to {output_path}", file=sys.stderr)
    except StorageError as exc:
        print(f"rcodex: {exc}", file=sys.stderr)
        return 1
    return {
        RunStatus.succeeded: 0,
        RunStatus.partial: 3,
        RunStatus.failed: 1,
        RunStatus.timed_out: 124,
        RunStatus.cancelled: 130,
    }[result.status]


def main(argv: Sequence[str] | None = None) -> int:
    _load_cli_environment()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
