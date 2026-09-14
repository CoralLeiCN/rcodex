"""Check the official plugin seam and the source-only container bundle."""

import io
import tarfile

from skillsbench_agents import source_archive


def test_bundle_has_core_and_adapter_without_task_data() -> None:
    with tarfile.open(fileobj=io.BytesIO(source_archive()), mode="r:gz") as archive:
        names = archive.getnames()
    assert "rcodex/runner.py" in names
    assert "skillsbench_agents/agent.py" in names
    assert "install.sh" in names
    assert all(name == "install.sh" or name.endswith(".py") for name in names)
    assert not any(
        part in name for name in names for part in ("verifier", "oracle", "tests/", ".env")
    )


def test_entry_point_keeps_builtin_codex() -> None:
    from benchflow.agents.registry import AGENT_ALIASES, AGENTS

    codex = AGENTS[AGENT_ALIASES["codex"]]
    assert codex.name == "codex-acp"
    assert "@agentclientprotocol/codex-acp@1.6.0" in codex.install_cmd
    assert AGENTS["rcodex"].protocol == "acp"
    assert "-m skillsbench_agents.agent" in AGENTS["rcodex"].launch_cmd
