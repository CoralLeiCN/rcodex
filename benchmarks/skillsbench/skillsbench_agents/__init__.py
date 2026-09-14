"""Register rcodex through BenchFlow's documented agent-package entry point."""

from __future__ import annotations

import base64
import io
import tarfile
from pathlib import Path


def source_archive() -> bytes:
    """Bundle only current agent/core source for a local, uncommitted development run."""
    here = Path(__file__).resolve().parent
    root = here.parents[2]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for directory in (root / "src/rcodex", here):
            for path in sorted(directory.glob("**/*.py")):
                archive.add(path, arcname=str(Path(directory.name) / path.relative_to(directory)))
        archive.add(here.parent / "install.sh", arcname="install.sh")
    return buffer.getvalue()


def register() -> None:
    from benchflow.agents.registry import register_agent

    payload = base64.b64encode(source_archive()).decode("ascii")
    register_agent(
        name="rcodex",
        protocol="acp",
        api_protocol="openai-responses",
        supports_acp_set_model=False,
        install_cmd="mkdir -p /opt/rcodex-benchmark && "
        f"printf '%s' '{payload}' | base64 -d | tar -xz -C /opt/rcodex-benchmark && "
        "sh /opt/rcodex-benchmark/install.sh",
        launch_cmd="env PYTHONPATH=/opt/rcodex-benchmark "
        "RCODEX_CODEX_BIN=/opt/rcodex-venv/bin/codex "
        "/opt/rcodex-venv/bin/python -m skillsbench_agents.agent",
        skill_paths=["$HOME/.agents/skills"],
        env_mapping={
            "BENCHFLOW_PROVIDER_BASE_URL": "RCODEX_PROVIDER_BASE_URL",
            "BENCHFLOW_PROVIDER_API_KEY": "RCODEX_PROVIDER_API_KEY",
            "BENCHFLOW_PROVIDER_MODEL": "RCODEX_MODEL",
        },
    )
