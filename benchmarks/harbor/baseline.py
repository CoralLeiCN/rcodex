"""Native Codex baseline and optional Docker Desktop provider forwarding."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from rcodex.codex_adapter.config import (
    PINNED_CODEX_RUNTIME_VERSION,
    PROVIDER_API_KEY_ENV,
    locked_down_config,
)

from .agents import _local_model_settings
from .network import forward_http_provider


class CodexBaselineAgent(Codex):
    """Harbor's native Codex agent, configured for the same provider as rcodex.

    Installation, native prompts/tools, execution, and trajectory parsing belong to
    Harbor. This adapter sets the provider, optional forwarding, and runtime version.
    """

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        provider_base_url: str | None = None,
        provider_gateway_host: str | None = None,
        reasoning_effort: str = "low",
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        model_name, provider_base_url = _local_model_settings(model_name, provider_base_url)
        self.provider_base_url = provider_base_url
        self.provider_gateway_host = provider_gateway_host
        api_key = os.environ.get(PROVIDER_API_KEY_ENV, "")
        provider: dict[str, Any] = {
            "name": "Local Responses provider",
            "base_url": provider_base_url,
            "wire_api": "responses",
            "requires_openai_auth": False,
        }
        if api_key:
            provider["env_key"] = "OPENAI_API_KEY"
        config = {
            **locked_down_config(),
            "model_provider": "local",
            "model_providers": {"local": provider},
            "shell_environment_policy": {"exclude": ["OPENAI_API_KEY", PROVIDER_API_KEY_ENV]},
        }
        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            version=PINNED_CODEX_RUNTIME_VERSION,
            reasoning_effort=reasoning_effort,
            config=config,
            extra_env={
                **{
                    key: value
                    for key, value in (extra_env or {}).items()
                    if key not in {"CODEX_AUTH_JSON_PATH", "CODEX_FORCE_AUTH_JSON"}
                },
                "OPENAI_BASE_URL": provider_base_url,
                "OPENAI_API_KEY": api_key,
            },
            **kwargs,
        )

    def _resolve_auth_json_path(self) -> Path | None:
        # Use only the local provider key. Do not import ambient OpenAI logins or
        # set auth flag env vars: Harbor scrubs their values even when boolean.
        return None

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        if self.provider_gateway_host is None:
            await super().run(instruction, environment, context)
            return
        async with forward_http_provider(
            self.provider_base_url, self.provider_gateway_host
        ) as container_url:
            self._base_config["model_providers"]["local"]["base_url"] = container_url
            self._extra_env["OPENAI_BASE_URL"] = container_url
            try:
                await super().run(instruction, environment, context)
            finally:
                self._base_config["model_providers"]["local"]["base_url"] = self.provider_base_url
                self._extra_env["OPENAI_BASE_URL"] = self.provider_base_url

    def populate_context_post_run(self, context: AgentContext) -> None:
        # Harbor only invokes this hook if run() leaves the context empty.
        # Metadata must be added here, after its native token/trajectory parser.
        super().populate_context_post_run(context)
        context.metadata = {
            **(context.metadata or {}),
            "provider_base_url": self.provider_base_url,
            "provider_gateway_host": self.provider_gateway_host,
        }
