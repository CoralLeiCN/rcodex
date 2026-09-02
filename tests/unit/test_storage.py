from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from rcodex.storage import RunStore, StorageError


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_existing_shared_state_directory_is_rejected_without_chmod(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir(mode=0o755)
    state_directory.chmod(0o755)

    with pytest.raises(StorageError, match="deny group and other access"):
        RunStore(state_directory, "run_00000000000000000000000000000000")

    assert stat.S_IMODE(state_directory.stat().st_mode) == 0o755
