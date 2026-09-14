from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from rcodex.config import RunConfig
from rcodex.context import build_manifest, canonical_json_bytes
from rcodex.models import ContextManifest, ManifestEntry
from rcodex.repl_context import (
    CONTEXT_MESSAGE_BYTES,
    CONTEXT_PAGE_BYTES,
    CONTEXT_READ_BYTES,
    ContextAccessError,
    ContextReader,
)


def _request(reader: ContextReader, **arguments: Any) -> dict[str, Any]:
    return reader.request(arguments, deadline=time.monotonic() + 5, cancel_event=Event())


def _reader(root: Path, text: str, **config: Any) -> ContextReader:
    (root / "input.txt").write_text(text, encoding="utf-8")
    return ContextReader(build_manifest(root, RunConfig(**config)))


@pytest.mark.parametrize("text", ["", "a\n", "aé🙂\n漢字\r\n" * 10, "x" * 200_000])
def test_reads_reassemble_utf8_without_losing_boundaries(tmp_path: Path, text: str) -> None:
    reader = _reader(tmp_path, text)
    page = _request(reader, operation="files", offset=0)
    entry = page["entries"][0]
    assert entry["relative_path"] == "input.txt"
    assert entry["bytes"] == len(text.encode("utf-8"))
    pieces = []
    offset = 0
    chunk_size = 4 if len(text) < 1000 else CONTEXT_READ_BYTES
    while True:
        chunk = _request(
            reader, operation="read", entry_id=entry["id"], offset=offset, max_bytes=chunk_size
        )
        assert chunk["context_entry_id"] == entry["id"]
        assert chunk["path"] == "input.txt"
        assert chunk["offset"] == offset
        assert len(chunk["text"].encode("utf-8")) <= chunk_size
        assert chunk["next_offset"] == offset + len(chunk["text"].encode("utf-8"))
        pieces.append(chunk["text"])
        if chunk["eof"]:
            break
        assert chunk["next_offset"] > offset
        offset = chunk["next_offset"]
    assert "".join(pieces) == text


def test_read_is_bounded_and_does_not_load_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _reader(tmp_path, "x" * 1_000_000)
    actual_read = os.read
    requests = []

    def observed_read(descriptor: int, size: int) -> bytes:
        requests.append(size)
        return actual_read(descriptor, size)

    monkeypatch.setattr(os, "read", observed_read)
    chunk = _request(reader, operation="read", entry_id="file_000001", offset=50, max_bytes=32)
    assert chunk["text"] == "x" * 32
    assert requests == [32]


def test_json_escaping_fits_the_context_transport_limit(tmp_path: Path) -> None:
    reader = _reader(tmp_path, "\x01" * CONTEXT_READ_BYTES)
    chunk = _request(
        reader, operation="read", entry_id="file_000001", offset=0, max_bytes=CONTEXT_READ_BYTES
    )
    assert len(canonical_json_bytes(chunk)) < CONTEXT_MESSAGE_BYTES - 1024


def test_manifest_pages_are_bounded_and_complete() -> None:
    # Exercise long UTF-8 paths without relying on the host filesystem's path-length limit.
    manifest = ContextManifest(
        context_root="/unused",
        entries=[
            ManifestEntry(
                id=f"file_{index:06d}",
                relative_path="漢" * 4000 + f"{index}.txt",
                bytes=0,
                sha256="0" * 64,
                media_type="text/plain",
                line_count=0,
            )
            for index in range(1, 120)
        ],
    )
    reader = ContextReader(manifest)
    observed: list[str] = []
    offset = 0
    while True:
        page = _request(reader, operation="files", offset=offset)
        assert len(canonical_json_bytes(page["entries"])) <= CONTEXT_PAGE_BYTES
        assert page["entries"]
        observed.extend(entry["id"] for entry in page["entries"])
        if page["eof"]:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert observed == [entry.id for entry in manifest.entries]


@pytest.mark.parametrize(
    "updates",
    [
        {"entry_id": "../input.txt"},
        {"entry_id": "/etc/passwd"},
        {"entry_id": "file_999999"},
        {"offset": -1},
        {"offset": 1000},
        {"offset": True},
        {"max_bytes": 3},
        {"max_bytes": CONTEXT_READ_BYTES + 1},
        {"max_bytes": "10"},
        {"extra": "ignored?"},
    ],
)
def test_invalid_context_requests_are_rejected(tmp_path: Path, updates: dict[str, Any]) -> None:
    reader = _reader(tmp_path, "hello")
    arguments = {"operation": "read", "entry_id": "file_000001", "offset": 0, "max_bytes": 4}
    with pytest.raises(ContextAccessError):
        _request(reader, **(arguments | updates))


def test_read_rejects_offset_inside_utf8_codepoint(tmp_path: Path) -> None:
    reader = _reader(tmp_path, "🙂hello")
    with pytest.raises(ContextAccessError, match="UTF-8 boundary"):
        _request(reader, operation="read", entry_id="file_000001", offset=1, max_bytes=4)


def test_context_interface_only_exposes_included_manifest_files(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("excluded", encoding="utf-8")
    (tmp_path / "alias.txt").symlink_to(tmp_path / "secret.txt")
    reader = _reader(tmp_path, "included", include=("input.txt",))
    page = _request(reader, operation="files", offset=0)
    assert [entry["relative_path"] for entry in page["entries"]] == ["input.txt"]
    with pytest.raises(ContextAccessError, match="unknown manifest entry"):
        _request(reader, operation="read", entry_id="file_000002", offset=0, max_bytes=4)


@pytest.mark.parametrize("replacement", ["symlink", "directory_symlink", "fifo", "size_change"])
def test_replaced_manifest_paths_are_rejected(tmp_path: Path, replacement: str) -> None:
    root = tmp_path / "context"
    nested = root / "nested"
    nested.mkdir(parents=True)
    _reader(nested, "original")
    reader = ContextReader(build_manifest(root, RunConfig()))
    path = nested / "input.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("external", encoding="utf-8")
    if replacement == "directory_symlink":
        nested.rename(root / "moved")
        nested.symlink_to(tmp_path, target_is_directory=True)
        (tmp_path / "input.txt").write_text("external", encoding="utf-8")
    else:
        path.unlink()
        if replacement == "symlink":
            path.symlink_to(outside)
        elif replacement == "fifo":
            os.mkfifo(path)
        else:
            path.write_text("changed size", encoding="utf-8")
    with pytest.raises(ContextAccessError):
        _request(reader, operation="read", entry_id="file_000001", offset=0, max_bytes=4)


def test_cancelled_or_expired_context_read_never_opens_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _reader(tmp_path, "hello")

    def forbidden_open(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("a cancelled request must not open files")

    monkeypatch.setattr(os, "open", forbidden_open)
    arguments = {"operation": "read", "entry_id": "file_000001", "offset": 0, "max_bytes": 4}
    cancelled = Event()
    cancelled.set()
    for deadline, event in [(time.monotonic() + 5, cancelled), (time.monotonic() - 1, Event())]:
        with pytest.raises(ContextAccessError, match="deadline"):
            reader.request(arguments, deadline=deadline, cancel_event=event)
