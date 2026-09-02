"""SDK-independent contracts used by the run controller."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rcodex.models import RunErrorCode, RunUsage


class AdapterError(RuntimeError):
    """Safe, controller-readable Codex adapter failure."""


class AdapterAuthenticationError(AdapterError):
    """No authenticated Codex account is available."""


class AdapterTimeoutError(AdapterError):
    """The controller deadline expired and the turn was interrupted."""


class AdapterCleanupError(AdapterError):
    """The SDK did not shut down cleanly within the cleanup boundary."""


class AdapterRuntimeError(AdapterError):
    """The SDK failed during a named lifecycle phase."""

    def __init__(self, phase: str, error_type: str) -> None:
        super().__init__(f"Codex SDK failure during {phase}: {error_type}")
        self.phase = phase
        self.error_type = error_type


_SDK_STARTUP_PHASES = frozenset(
    {"startup", "runtime_version", "account", "thread_start", "thread_resume"}
)


def adapter_error_code(error: AdapterError) -> RunErrorCode:
    """Map adapter lifecycle failures to the current controller error contract."""

    if isinstance(error, AdapterRuntimeError) and error.phase in _SDK_STARTUP_PHASES:
        return RunErrorCode.sdk_startup
    return RunErrorCode.sdk_runtime


@dataclass(frozen=True, slots=True)
class DirectTurnRequest:
    prompt: str
    context_root: Path
    model: str | None
    reasoning_effort: str
    output_schema: dict[str, Any]
    timeout_seconds: float
    cleanup_timeout_seconds: float
    node_id: str | None = None
    custom_system_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class DirectTurn:
    thread_id: str
    final_response: str
    usage: RunUsage
    codex_runtime_version: str | None


@dataclass(frozen=True, slots=True)
class RootSessionRequest:
    context_root: Path
    model: str | None
    reasoning_effort: str
    timeout_seconds: float
    cleanup_timeout_seconds: float
    persistent: bool = False
    thread_id: str | None = None
    custom_system_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class RootTurnRequest:
    prompt: str
    output_schema: dict[str, Any] | None
    timeout_seconds: float
    cleanup_timeout_seconds: float
    phase: str


class RootSession(Protocol):
    @property
    def thread_id(self) -> str: ...

    @property
    def codex_runtime_version(self) -> str | None: ...

    async def turn(self, request: RootTurnRequest) -> DirectTurn:
        """Continue the retained recursive-node thread with one turn."""

    async def compact(self, timeout_seconds: float) -> None:
        """Compact the retained root thread within a controller deadline."""

    async def close(self, timeout_seconds: float) -> None:
        """Close the root session within the controller cleanup boundary."""


class RecursiveCodexAdapter(Protocol):
    async def start_root(self, request: RootSessionRequest) -> RootSession:
        """Start or resume one iterative recursive-node thread."""

    async def run_leaf(self, request: DirectTurnRequest) -> DirectTurn:
        """Run one fresh, terminal leaf thread."""
