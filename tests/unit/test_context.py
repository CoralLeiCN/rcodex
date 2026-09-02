from __future__ import annotations

from pathlib import Path

import pytest

from rcodex.config import RunConfig
from rcodex.context import ManifestError, build_manifest


def test_manifest_entry_limit_is_a_candidate_path_budget(tmp_path: Path) -> None:
    (tmp_path / "a-invalid.txt").write_bytes(b"\xff")
    (tmp_path / "b-valid.txt").write_text("valid\n", encoding="utf-8")

    with pytest.raises(ManifestError, match="candidate-path budget"):
        build_manifest(tmp_path, RunConfig(max_manifest_entries=1))


def test_invalid_utf8_bytes_still_consume_context_byte_budget(tmp_path: Path) -> None:
    (tmp_path / "a-invalid.txt").write_bytes(b"\xff" * 6)
    (tmp_path / "b-valid.txt").write_text("valid\n", encoding="utf-8")

    with pytest.raises(ManifestError, match="max_context_bytes"):
        build_manifest(
            tmp_path,
            RunConfig(max_manifest_entries=2, max_context_bytes=10),
        )
