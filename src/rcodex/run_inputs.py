"""Shared invocation validation for direct and recursive runs."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


class InvocationError(ValueError):
    """Invalid CLI/Python input detected before the run starts."""


AMBIENT_MCP_WARNING = (
    "WARNING: rcodex does not support MCP and cannot guarantee that Codex starts without MCP "
    "servers inherited from your Codex configuration. Ambient MCP tools may receive task or "
    "context data or perform external actions with configured credentials. Review or isolate "
    "your Codex MCP configuration before running. By running rcodex, the operator accepts this "
    "risk."
)


def warn_ambient_mcp_risk() -> None:
    """Disclose the residual ambient-MCP risk before a prompt-driven run starts."""

    print(AMBIENT_MCP_WARNING, file=sys.stderr)


def default_state_directory(context: Path) -> Path:
    """Choose a stable sibling state tree that does not overlap the context."""

    resolved = context.expanduser().resolve(strict=False)
    try:
        encoded = str(resolved).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvocationError("context path must be valid UTF-8") from exc
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    label = resolved.name or "root"
    return resolved.parent / ".rcodex-state" / f"{label}-{digest}"


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def resolve_run_paths(
    context: Path, state_directory: Path, output: Path | None
) -> tuple[Path, Path, Path | None]:
    try:
        context_root = context.expanduser().resolve(strict=True)
    except OSError as exc:
        raise InvocationError("context does not exist or cannot be resolved") from exc
    if not context_root.is_dir():
        raise InvocationError("context must be a directory")

    state_root = state_directory.expanduser().resolve(strict=False)
    if _paths_overlap(context_root, state_root):
        raise InvocationError(
            "state directory overlaps the context; pass --state-dir outside the context root"
        )

    output_path = output.expanduser().resolve(strict=False) if output is not None else None
    for label, resolved in (
        ("context", context_root),
        ("state directory", state_root),
        ("output", output_path),
    ):
        if resolved is None:
            continue
        rendered = str(resolved)
        try:
            rendered.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InvocationError(f"{label} path must be valid UTF-8") from exc
        if len(rendered) > 4096:
            raise InvocationError(f"{label} path exceeds 4096 characters")
    if output_path is not None:
        if output_path == context_root or output_path.is_relative_to(context_root):
            raise InvocationError("output path must be outside the context root")
        if output_path.exists() and output_path.is_dir():
            raise InvocationError("output path must be a file")
        if _paths_overlap(output_path, state_root):
            raise InvocationError("output path must be outside the state directory")
    return context_root, state_root, output_path


def validate_task(task: str) -> str:
    normalized_task = task.strip()
    if not normalized_task:
        raise InvocationError("task must not be blank")
    if len(normalized_task) > 16_384:
        raise InvocationError("task exceeds 16384 characters")
    try:
        normalized_task.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvocationError("task must be valid UTF-8") from exc
    return normalized_task
