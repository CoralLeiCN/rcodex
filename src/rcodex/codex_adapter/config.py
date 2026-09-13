"""Codex configuration shared by runtime adapters."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from openai_codex import CodexConfig
from openai_codex.types import ReasoningEffort

REASONING_EFFORTS = tuple(effort.value for effort in ReasoningEffort)
PINNED_CODEX_RUNTIME_VERSION = "0.153.4"
CODEX_BIN_ENV = "RCODEX_CODEX_BIN"
PROVIDER_API_KEY_ENV = "RCODEX_PROVIDER_API_KEY"
OPENAI_COMPATIBLE_PROVIDER_ID = "rcodex"


def codex_runtime_path() -> Path:
    """Resolve the required external Codex runtime without using the SDK fallback."""

    configured = os.environ.get(CODEX_BIN_ENV)
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise FileNotFoundError(f"{CODEX_BIN_ENV} does not name an executable file")
        return path

    discovered = shutil.which("codex")
    if discovered is None:
        raise FileNotFoundError(
            f"Codex {PINNED_CODEX_RUNTIME_VERSION} is required on PATH; "
            f"alternatively set {CODEX_BIN_ENV}"
        )
    return Path(discovered).resolve()


def runtime_release(server_version: str | None) -> str | None:
    """Extract the release number from App Server's descriptive version string."""

    if server_version is None:
        return None
    release, _, _details = server_version.partition(" ")
    return release or None


def locked_down_config() -> dict[str, Any]:
    """Return the per-thread config requesting every unrelated tool class off."""

    return {
        "agents": {"enabled": False},
        "apps": {"_default": {"enabled": False}},
        "mcp_servers": {},
        "tool_suggest": {"discoverables": []},
        "tools": {"web_search": False},
        "web_search": "disabled",
    }


def process_config(
    *,
    codex_bin: Path | None = None,
    provider_base_url: str | None = None,
    codex_home: Path | None = None,
) -> CodexConfig:
    """Return the process-level equivalent of the per-thread lockdown request."""

    overrides = [
        "agents.enabled=false",
        "apps._default.enabled=false",
        "mcp_servers={}",
        "tool_suggest.discoverables=[]",
        "tools.web_search=false",
        'web_search="disabled"',
    ]
    if provider_base_url is not None:
        provider = f"model_providers.{OPENAI_COMPATIBLE_PROVIDER_ID}"
        overrides.extend(
            (
                f'model_provider="{OPENAI_COMPATIBLE_PROVIDER_ID}"',
                f'{provider}.name="rcodex OpenAI-compatible provider"',
                f"{provider}.base_url={json.dumps(provider_base_url, ensure_ascii=False)}",
                f'{provider}.wire_api="responses"',
                f"{provider}.requires_openai_auth=false",
            )
        )
        if os.environ.get(PROVIDER_API_KEY_ENV):
            overrides.append(
                f"{provider}.env_key={json.dumps(PROVIDER_API_KEY_ENV, ensure_ascii=False)}"
            )
            overrides.append(
                "shell_environment_policy.exclude="
                f"{json.dumps([PROVIDER_API_KEY_ENV], ensure_ascii=False)}"
            )

    return CodexConfig(
        codex_bin=str(codex_bin or codex_runtime_path()),
        config_overrides=tuple(overrides),
        env={"CODEX_HOME": str(codex_home)} if codex_home is not None else None,
    )
